"""Per-project workspace on the dispatcher host.

There are no containers anymore. Every project gets an isolated directory under
``runtime.workspace_root``; the agent worker's cwd and its session files live
there. Completed projects can be kept or removed according to config.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

LOG = logging.getLogger(__name__)


class WorkspaceManager:
    def __init__(self, root: Path, *, completed_action: str = "keep"):
        self.root = root.expanduser().resolve()
        self.completed_action = completed_action
        self.root.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        return None

    def project_dir(self, project_id: str) -> Path:
        return self.root / project_id.replace("/", "-")

    def session_path(self, project_id: str, task_type: str, run_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in run_id)
        return self.project_dir(project_id) / ".cairn" / "sessions" / f"{task_type}-{safe}.json"

    # Kept name for the scheduler's cleanup bookkeeping.
    def container_name(self, project_id: str) -> str:
        return str(self.project_dir(project_id))

    def ensure_running(self, project_id: str) -> str:
        path = self.project_dir(project_id)
        path.mkdir(parents=True, exist_ok=True)
        (path / ".cairn").mkdir(parents=True, exist_ok=True)
        return str(path)

    def needs_completed_cleanup(self, project_id: str) -> bool:
        return self.completed_action == "remove" and self.project_dir(project_id).exists()

    def cleanup_completed(self, project_id: str) -> bool:
        if self.completed_action != "remove":
            return True
        path = self.project_dir(project_id)
        if path.exists():
            LOG.info("removing completed project workspace project=%s dir=%s", project_id, path)
            shutil.rmtree(path, ignore_errors=True)
        return not path.exists()

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return False

    def cleanup_stopped(self, project_id: str) -> bool:
        return True
