"""Tool implementations for Cairn's built-in agent.

The tool surface mirrors what a coding/pentest agent needs: bash, read, write,
edit, grep, find and ls. Large command output is written to
``<workspace>/.cairn/outputs`` and only a tail is returned to the model, so a
full port scan does not blow up the context window.
"""

from __future__ import annotations

import base64
import codecs
import difflib
import fnmatch
import io
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps, UnidentifiedImageError

from cairn.agent.graph import load_snapshot, query_graph


MAX_OUTPUT_BYTES = 50 * 1024
MAX_OUTPUT_LINES = 2000
MAX_READ_BYTES = 50 * 1024
MAX_READ_LINES = 2000
MAX_GREP_LINE = 500
MAX_WRITE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_INPUT_BYTES = 20 * 1024 * 1024
MAX_IMAGE_OUTPUT_BYTES = 3 * 1024 * 1024
MAX_IMAGE_DIMENSION = 2048
IMAGE_FORMATS = {"PNG", "JPEG", "GIF", "WEBP", "BMP"}
Image.MAX_IMAGE_PIXELS = 50_000_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".cairn", "venv", "dist", "build"}


@dataclass(slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    images: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolResult]
    parallel_safe: bool = False

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _error(message: str) -> ToolResult:
    return ToolResult(f"ERROR: {message}", is_error=True)


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _truncate_tail(text: str, max_bytes: int = MAX_OUTPUT_BYTES, max_lines: int = MAX_OUTPUT_LINES) -> tuple[str, bool]:
    lines = text.splitlines()
    truncated = len(lines) > max_lines or len(text.encode("utf-8", "replace")) > max_bytes
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    result = "\n".join(lines)
    encoded = result.encode("utf-8", "replace")
    if len(encoded) > max_bytes:
        result = encoded[-max_bytes:].decode("utf-8", "ignore")
        truncated = True
    return result, truncated


def _detect_image_mime(probe: bytes) -> str | None:
    if probe.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if probe.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if probe.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(probe) >= 12 and probe[:4] == b"RIFF" and probe[8:12] == b"WEBP":
        return "image/webp"
    if probe.startswith(b"BM"):
        return "image/bmp"
    return None


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA") or "transparency" in image.info


def _flatten_image(image: Image.Image) -> Image.Image:
    if _has_alpha(image):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return image.convert("RGB")


def _encode_image(image: Image.Image, format_name: str, quality: int = 88) -> bytes:
    buffer = io.BytesIO()
    if format_name == "JPEG":
        image.save(buffer, "JPEG", quality=quality, optimize=True)
    else:
        image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def _image_result(
    path: Path,
    mime: str,
    encoded: bytes,
    image: Image.Image,
    original_mime: str,
    original_width: int,
    original_height: int,
) -> dict[str, Any]:
    return {
        "mime": mime,
        "data": base64.b64encode(encoded).decode("ascii"),
        "path": str(path),
        "bytes": len(encoded),
        "width": image.width,
        "height": image.height,
        "original_mime": original_mime,
        "original_width": original_width,
        "original_height": original_height,
        "resized": image.width != original_width or image.height != original_height,
    }


def _process_image(data: bytes, path: Path, original_mime: str) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(data)) as opened:
            source_format = str(opened.format or "").upper()
            if source_format not in IMAGE_FORMATS:
                raise ValueError(f"unsupported image format: {source_format or 'unknown'}")
            image = ImageOps.exif_transpose(opened)
            if getattr(image, "is_animated", False):
                try:
                    image.seek(0)
                except EOFError:
                    pass
            original_width, original_height = image.size
            if max(original_width, original_height) > MAX_IMAGE_DIMENSION:
                image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS)
            preferred = "JPEG" if source_format == "JPEG" else "PNG"

            for scale in (1.0, 0.8, 0.6, 0.45):
                if scale == 1.0:
                    candidate = image
                else:
                    candidate = image.resize(
                        (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                        Image.Resampling.LANCZOS,
                    )
                if preferred == "PNG":
                    png = candidate if candidate.mode in ("RGB", "RGBA") else candidate.convert(
                        "RGBA" if _has_alpha(candidate) else "RGB"
                    )
                    encoded = _encode_image(png, "PNG")
                    if len(encoded) <= MAX_IMAGE_OUTPUT_BYTES:
                        return _image_result(path, "image/png", encoded, png, original_mime, original_width, original_height)
                flat = _flatten_image(candidate)
                for quality in (88, 75, 60):
                    encoded = _encode_image(flat, "JPEG", quality)
                    if len(encoded) <= MAX_IMAGE_OUTPUT_BYTES:
                        return _image_result(path, "image/jpeg", encoded, flat, original_mime, original_width, original_height)
            raise ValueError("image is still too large after resizing")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"cannot process image: {exc}") from exc


class Toolbox:
    def __init__(
        self,
        workspace: Path,
        *,
        cancel_event: threading.Event | None = None,
        include_graph: bool = False,
    ):
        self.workspace = workspace.resolve()
        self.output_dir = self.workspace / ".cairn" / "outputs"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cancel_event = cancel_event or threading.Event()
        self._specs: dict[str, ToolSpec] = {}
        specs = [
            self._bash_spec(),
            self._read_spec(),
            self._write_spec(),
            self._edit_spec(),
            self._grep_spec(),
            self._find_spec(),
            self._ls_spec(),
        ]
        if include_graph:
            specs.append(self._graph_spec())
        for spec in specs:
            self._specs[spec.name] = spec

    def specs(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._specs.values()]

    def is_parallel_safe(self, name: str) -> bool:
        spec = self._specs.get(name)
        return bool(spec and spec.parallel_safe)

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return _error(f"unknown tool: {name}")
        if not isinstance(arguments, dict):
            return _error("tool arguments must be a JSON object")
        try:
            return spec.handler(arguments)
        except Exception as exc:  # tools never crash the loop
            return _error(f"{name} failed: {exc}")

    # ------------------------------------------------------------------ paths

    def _resolve(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve()

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return path.as_posix()

    # ------------------------------------------------------------------ bash

    def _graph_spec(self) -> ToolSpec:
        return ToolSpec(
            name="graph",
            description=(
                "Read-only query over the current project graph. Call overview first, then use "
                "frontier/get/search/neighbors for details. This tool does not modify the graph."
            ),
            parameters=_obj(
                {
                    "query": {
                        "type": "string",
                        "enum": ["overview", "frontier", "get", "search", "neighbors"],
                        "description": "Graph query to run.",
                    },
                    "id": {
                        "type": "string",
                        "description": "Node id for get/neighbors (e.g. f001, i002, origin, goal).",
                    },
                    "text": {"type": "string", "description": "Search text for search."},
                    "depth": {
                        "type": "integer",
                        "description": "Neighbor depth, 1 or 2 (default 1).",
                        "minimum": 1,
                        "maximum": 2,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default 20, max 50).",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                ["query"],
            ),
            handler=self._graph,
            parallel_safe=True,
        )

    def _graph(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or "")
        try:
            snapshot = load_snapshot(self.workspace)
            result = query_graph(
                snapshot,
                query,
                node_id=str(args.get("id") or "") or None,
                text=str(args.get("text") or "") or None,
                depth=_as_int(args.get("depth"), 1, 1, 2),
                limit=_as_int(args.get("limit"), 20, 1, 50),
            )
        except ValueError as exc:
            return _error(str(exc))
        return ToolResult(json.dumps(result, ensure_ascii=False, separators=(",", ":")))

    def _bash_spec(self) -> ToolSpec:
        return ToolSpec(
            name="bash",
            description=(
                "Execute a shell command in the project working directory. Use it for recon, scans, "
                "exploitation, file inspection and anything else. stdout and stderr are merged. "
                f"Output is truncated to the last {MAX_OUTPUT_LINES} lines / {MAX_OUTPUT_BYTES // 1024}KB; "
                "the full output is saved to a file whose path is returned."
            ),
            parameters=_obj(
                {
                    "command": {"type": "string", "description": "Shell command to execute."},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 300, max 3600).",
                        "minimum": 1,
                        "maximum": 3600,
                    },
                },
                ["command"],
            ),
            handler=self._bash,
            parallel_safe=False,
        )

    def _bash(self, args: dict[str, Any]) -> ToolResult:
        command = str(args.get("command") or "")
        if not command.strip():
            return _error("command is required")
        timeout = _as_int(args.get("timeout"), 300, 1, 3600)
        if self.cancel_event.is_set():
            return _error("cancelled")

        run_id = f"bash-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        output_path = self.output_dir / f"{run_id}.log"
        env = dict(os.environ)
        for key in (
            "CAIRN_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "DEEPSEEK_API_KEY",
        ):
            env.pop(key, None)

        started = time.monotonic()
        if os.name == "nt":
            argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
            popen_kwargs: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            argv = ["/bin/bash", "-lc", command]
            popen_kwargs = {"start_new_session": True}
        with output_path.open("wb") as sink:
            process = subprocess.Popen(
                argv,
                cwd=str(self.workspace),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **popen_kwargs,
            )
            tail = bytearray()
            total_bytes = 0
            lock = threading.Lock()

            def drain() -> None:
                nonlocal total_bytes
                assert process.stdout is not None
                while True:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    sink.write(chunk)
                    sink.flush()
                    with lock:
                        total_bytes += len(chunk)
                        tail.extend(chunk)
                        if len(tail) > 300 * 1024:
                            del tail[: len(tail) - 200 * 1024]

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            timed_out = False
            cancelled = False
            deadline = started + timeout
            while True:
                try:
                    process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if self.cancel_event.is_set():
                        cancelled = True
                        self._kill_group(process)
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        self._kill_group(process)
                        break
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._kill_group(process, force=True)
                process.wait(timeout=3)
            reader.join(timeout=3)
            wal = time.monotonic() - started

        with lock:
            raw = bytes(tail).decode("utf-8", "replace")
        output, truncated = _truncate_tail(raw)
        status = "cancelled" if cancelled else "timed_out" if timed_out else ("success" if process.returncode == 0 else "failed")
        payload = {
            "status": status,
            "exit_code": process.returncode,
            "wall_time_seconds": round(wal, 2),
            "output": output,
            "truncated": truncated,
            "full_output": str(output_path),
            "output_bytes": total_bytes,
            "output_lines": raw.count("\n") + (1 if raw and not raw.endswith("\n") else 0),
        }
        return ToolResult(json.dumps(payload, ensure_ascii=False), is_error=status in ("failed", "timed_out", "cancelled"))

    @staticmethod
    def _kill_group(process: subprocess.Popen[bytes], *, force: bool = False) -> None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                except OSError:
                    pass
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass

    # ------------------------------------------------------------------ read

    def _read_spec(self) -> ToolSpec:
        return ToolSpec(
            name="read",
            description=(
                "Read a file. Text files return at most 2000 lines / 50KB per call; use offset "
                "(1-indexed) and limit for large files. Image files (png/jpg/jpeg/gif/webp/bmp) are "
                "MIME-detected from content, auto-resized/compressed, and returned as image attachments; "
                "original image max 20MB, output max 3MB. offset/limit are only for text files."
            ),
            parameters=_obj(
                {
                    "path": {"type": "string", "description": "File path, relative or absolute."},
                    "offset": {"type": "integer", "description": "1-indexed starting line.", "minimum": 1},
                    "limit": {"type": "integer", "description": "Maximum lines to read.", "minimum": 1},
                },
                ["path"],
            ),
            handler=self._read,
            parallel_safe=True,
        )

    def _read(self, args: dict[str, Any]) -> ToolResult:
        raw_path = str(args.get("path") or "")
        if not raw_path:
            return _error("path is required")
        path = self._resolve(raw_path)
        if not path.exists():
            return _error(f"file not found: {path}")
        if not path.is_file():
            return _error(f"not a file: {path}")

        with path.open("rb") as handle:
            probe = handle.read(8192)
        mime = _detect_image_mime(probe)
        if mime:
            if args.get("offset") is not None or args.get("limit") is not None:
                return _error("image read does not support offset/limit")
            size = path.stat().st_size
            if size > MAX_IMAGE_INPUT_BYTES:
                return _error(f"image is too large ({size} bytes; max {MAX_IMAGE_INPUT_BYTES})")
            try:
                image_data = _process_image(path.read_bytes(), path, mime)
            except ValueError as exc:
                return _error(str(exc))
            content = (
                f"Image: {path} ({image_data['mime']}, "
                f"{image_data['width']}x{image_data['height']}, {image_data['bytes']} bytes; "
                f"original {mime}, {image_data['original_width']}x{image_data['original_height']}"
                f"{' resized' if image_data['resized'] else ''}). Attached as an image content block."
            )
            return ToolResult(content, images=[image_data])

        offset = _as_int(args.get("offset"), 1, 1, 10_000_000)
        limit = args.get("limit")
        limit = _as_int(limit, MAX_READ_LINES, 1, MAX_READ_LINES) if limit is not None else None

        if b"\x00" in probe:
            return _error(f"binary file ({path.stat().st_size} bytes): use bash tools to inspect it")

        lines: list[str] = []
        total = 0
        selected_bytes = 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle, start=1):
                total = index
                if index < offset:
                    continue
                if limit is not None and len(lines) >= limit:
                    continue
                if len(lines) >= MAX_READ_LINES or selected_bytes >= MAX_READ_BYTES:
                    continue
                selected_bytes += len(line.encode("utf-8", "replace"))
                lines.append(line)
        if offset > total and total > 0:
            return _error(f"offset {offset} is beyond end of file ({total} lines)")

        content = "".join(lines)
        truncated = offset + len(lines) - 1 < total or selected_bytes > MAX_READ_BYTES
        if selected_bytes > MAX_READ_BYTES:
            encoded = content.encode("utf-8", "replace")[:MAX_READ_BYTES]
            content = encoded.decode("utf-8", "ignore")
        if truncated:
            end = offset + max(0, len(content.splitlines()) - 1)
            content += f"\n\n[Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.]"
        return ToolResult(content)

    # ------------------------------------------------------------------ write

    def _write_spec(self) -> ToolSpec:
        return ToolSpec(
            name="write",
            description="Create or overwrite a file. Parent directories are created automatically.",
            parameters=_obj(
                {
                    "path": {"type": "string", "description": "File path, relative or absolute."},
                    "content": {"type": "string", "description": "Complete file content."},
                },
                ["path", "content"],
            ),
            handler=self._write,
            parallel_safe=False,
        )

    def _write(self, args: dict[str, Any]) -> ToolResult:
        raw_path = str(args.get("path") or "")
        content = args.get("content")
        if not raw_path:
            return _error("path is required")
        if not isinstance(content, str):
            return _error("content must be a string")
        if len(content.encode("utf-8", "replace")) > MAX_WRITE_BYTES:
            return _error(f"content exceeds {MAX_WRITE_BYTES // 1024 // 1024}MB limit")
        path = self._resolve(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ToolResult(f"Successfully wrote {len(content)} characters to {path}")

    # ------------------------------------------------------------------ edit

    def _edit_spec(self) -> ToolSpec:
        return ToolSpec(
            name="edit",
            description=(
                "Edit one file with exact text replacements. Each oldText must match a unique, "
                "non-overlapping region of the original file. Batch nearby changes into one call."
            ),
            parameters=_obj(
                {
                    "path": {"type": "string", "description": "File path, relative or absolute."},
                    "edits": {
                        "type": "array",
                        "description": "Targeted replacements, all matched against the original file.",
                        "items": _obj(
                            {
                                "oldText": {"type": "string", "description": "Exact text to replace."},
                                "newText": {"type": "string", "description": "Replacement text."},
                            },
                            ["oldText", "newText"],
                        ),
                    },
                },
                ["path", "edits"],
            ),
            handler=self._edit,
            parallel_safe=False,
        )

    def _edit(self, args: dict[str, Any]) -> ToolResult:
        raw_path = str(args.get("path") or "")
        edits = args.get("edits")
        if not raw_path:
            return _error("path is required")
        if isinstance(edits, dict):
            edits = [edits]
        if not isinstance(edits, list) or not edits:
            return _error("edits must be a non-empty array")
        path = self._resolve(raw_path)
        if not path.exists():
            return _error(f"file not found: {path}")

        raw = path.read_bytes()
        bom = b""
        if raw.startswith(codecs.BOM_UTF8):
            bom, raw = codecs.BOM_UTF8, raw[len(codecs.BOM_UTF8) :]
        try:
            original = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return _error(f"file is not valid UTF-8: {exc}")
        crlf = "\r\n" in original
        content = original.replace("\r\n", "\n").replace("\r", "\n")

        normalized: list[tuple[str, str]] = []
        for index, edit in enumerate(edits):
            if not isinstance(edit, dict):
                return _error(f"edits[{index}] must be an object")
            old = edit.get("oldText", edit.get("old_text"))
            new = edit.get("newText", edit.get("new_text"))
            if not isinstance(old, str) or not isinstance(new, str):
                return _error(f"edits[{index}] requires oldText and newText strings")
            if not old:
                return _error(f"edits[{index}].oldText must not be empty")
            normalized.append((old, new))

        spans: list[tuple[int, int, str]] = []
        for old, new in normalized:
            count = content.count(old)
            if count == 0:
                return _error(f"oldText not found: {old[:120]!r}")
            if count > 1:
                return _error(f"oldText matches {count} times; make it unique: {old[:120]!r}")
            start = content.index(old)
            spans.append((start, start + len(old), new))
        spans.sort()
        for previous, current in zip(spans, spans[1:]):
            if current[0] < previous[1]:
                return _error("edits overlap; merge them into one edit")

        updated = content
        for start, end, new in reversed(spans):
            updated = updated[:start] + new + updated[end:]
        if crlf:
            updated = updated.replace("\n", "\r\n")
        path.write_bytes(bom + updated.encode("utf-8"))

        diff = list(
            difflib.unified_diff(
                content.splitlines(),
                updated.replace("\r\n", "\n").splitlines(),
                fromfile=str(path),
                tofile=str(path),
                lineterm="",
                n=2,
            )
        )
        diff_text = "\n".join(diff[:120])
        if len(diff) > 120:
            diff_text += "\n[... diff truncated ...]"
        return ToolResult(f"Successfully replaced {len(spans)} block(s) in {path}.\n\n{diff_text}")

    # ------------------------------------------------------------------ grep

    def _grep_spec(self) -> ToolSpec:
        return ToolSpec(
            name="grep",
            description=(
                "Search file contents with ripgrep semantics. Returns file:line matches, respects "
                ".gitignore, and truncates to 100 matches / 50KB."
            ),
            parameters=_obj(
                {
                    "pattern": {"type": "string", "description": "Regex or literal search pattern."},
                    "path": {"type": "string", "description": "Directory or file to search (default .)."},
                    "glob": {"type": "string", "description": "Glob filter, e.g. '*.py'."},
                    "ignoreCase": {"type": "boolean", "description": "Case-insensitive search."},
                    "literal": {"type": "boolean", "description": "Treat pattern as a literal string."},
                    "context": {"type": "integer", "description": "Lines of context around matches.", "minimum": 0, "maximum": 20},
                    "limit": {"type": "integer", "description": "Maximum matches (default 100).", "minimum": 1, "maximum": 1000},
                },
                ["pattern"],
            ),
            handler=self._grep,
            parallel_safe=True,
        )

    def _grep(self, args: dict[str, Any]) -> ToolResult:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return _error("pattern is required")
        search_path = self._resolve(str(args.get("path") or "."))
        if not search_path.exists():
            return _error(f"path not found: {search_path}")
        limit = _as_int(args.get("limit"), 100, 1, 1000)
        context = _as_int(args.get("context"), 0, 0, 20)

        rg = shutil.which("rg")
        if rg:
            command = [rg, "--no-config", "--line-number", "--no-heading", "--color", "never", "--hidden"]
            if args.get("ignoreCase"):
                command.append("--ignore-case")
            if args.get("literal"):
                command.append("--fixed-strings")
            if args.get("glob"):
                command += ["--glob", str(args["glob"])]
            if context:
                command += ["-C", str(context)]
            command += ["--", pattern, str(search_path)]
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(self.workspace),
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
            except subprocess.TimeoutExpired:
                return _error("grep timed out after 45s")
            if completed.returncode not in (0, 1):
                return _error(f"rg failed: {completed.stderr.strip()[:1000]}")
            lines = completed.stdout.splitlines()
        else:
            lines = self._python_grep(pattern, search_path, args, context)

        limited = lines[: limit * (1 + 2 * context) if context else limit]
        output = "\n".join(line[:MAX_GREP_LINE] for line in limited)
        if len(lines) > len(limited):
            output += f"\n\n[{len(lines) - len(limited)} more lines; narrow the pattern or path]"
        return ToolResult(output or "No matches found.")

    def _python_grep(self, pattern: str, root: Path, args: dict[str, Any], context: int) -> list[str]:
        flags = re.IGNORECASE if args.get("ignoreCase") else 0
        regex = re.escape(pattern) if args.get("literal") else pattern
        try:
            compiled = re.compile(regex, flags)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        glob = str(args.get("glob") or "*")
        results: list[str] = []
        files = [root] if root.is_file() else root.rglob("*")
        for file_path in files:
            if not file_path.is_file() or any(part in SKIP_DIRS for part in file_path.parts):
                continue
            if root.is_dir() and not fnmatch.fnmatch(file_path.name, glob):
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if not compiled.search(line):
                    continue
                start = max(0, index - context)
                end = min(len(lines), index + context + 1)
                for current in range(start, end):
                    prefix = ":" if current == index else "-"
                    results.append(f"{self._relative(file_path)}{prefix}{current + 1}{prefix}{lines[current]}")
        return results

    # ------------------------------------------------------------------ find

    def _find_spec(self) -> ToolSpec:
        return ToolSpec(
            name="find",
            description="Find files by glob pattern (ripgrep semantics, respects .gitignore).",
            parameters=_obj(
                {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '*.py' or '**/*.json'."},
                    "path": {"type": "string", "description": "Directory to search (default .)."},
                    "limit": {"type": "integer", "description": "Maximum results (default 1000).", "minimum": 1, "maximum": 5000},
                },
                ["pattern"],
            ),
            handler=self._find,
            parallel_safe=True,
        )

    def _find(self, args: dict[str, Any]) -> ToolResult:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return _error("pattern is required")
        search_path = self._resolve(str(args.get("path") or "."))
        if not search_path.exists():
            return _error(f"path not found: {search_path}")
        limit = _as_int(args.get("limit"), 1000, 1, 5000)
        rg = shutil.which("rg")
        paths: list[str] = []
        if rg:
            completed = subprocess.run(
                [rg, "--no-config", "--files", "--hidden", "--glob", pattern, str(search_path)],
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=45,
            )
            if completed.returncode not in (0, 1):
                return _error(f"rg failed: {completed.stderr.strip()[:1000]}")
            for line in completed.stdout.splitlines():
                path = Path(line)
                try:
                    paths.append(path.relative_to(search_path).as_posix())
                except ValueError:
                    paths.append(line.replace("\\", "/"))
        else:
            for path in search_path.rglob("*"):
                if path.is_file() and not any(part in SKIP_DIRS for part in path.parts) and fnmatch.fnmatch(path.name, pattern):
                    try:
                        paths.append(path.relative_to(search_path).as_posix())
                    except ValueError:
                        paths.append(path.as_posix())
        paths.sort()
        truncated = len(paths) > limit
        output = "\n".join(paths[:limit])
        if truncated:
            output += f"\n\n[{len(paths) - limit} more results; narrow the pattern or path]"
        return ToolResult(output or "No files found matching pattern")

    # ------------------------------------------------------------------ ls

    def _ls_spec(self) -> ToolSpec:
        return ToolSpec(
            name="ls",
            description="List a directory. Directories are shown with a trailing slash.",
            parameters=_obj(
                {
                    "path": {"type": "string", "description": "Directory to list (default .)."},
                    "limit": {"type": "integer", "description": "Maximum entries (default 500).", "minimum": 1, "maximum": 5000},
                }
            ),
            handler=self._ls,
            parallel_safe=True,
        )

    def _ls(self, args: dict[str, Any]) -> ToolResult:
        path = self._resolve(str(args.get("path") or "."))
        if not path.exists():
            return _error(f"path not found: {path}")
        if not path.is_dir():
            return _error(f"not a directory: {path}")
        limit = _as_int(args.get("limit"), 500, 1, 5000)
        entries: list[str] = []
        try:
            with os.scandir(path) as iterator:
                for entry in iterator:
                    suffix = "/" if entry.is_dir() else ""
                    entries.append(entry.name + suffix)
        except OSError as exc:
            return _error(f"cannot read directory: {exc}")
        entries.sort(key=str.lower)
        truncated = len(entries) > limit
        output = "\n".join(entries[:limit])
        if truncated:
            output += f"\n\n[{len(entries) - limit} more entries]"
        return ToolResult(output or "(empty directory)")
