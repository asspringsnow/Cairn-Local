"""Cairn's built-in agent runtime.

This package is deliberately self-contained: it does not shell out to pi, claude
or codex. A dispatcher task starts one ``cairn.agent.worker`` child process, the
worker runs the model/tool loop, and the dispatcher keeps scheduling other
projects while that child works.
"""

from cairn.agent.settings import AgentSettings

__all__ = ["AgentSettings"]
