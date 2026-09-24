"""Display-only launch context for managed subagents."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)
_WORKSPACE_KEYS = ("cwd", "worktree", "branch", "head")


def _redact(value: str) -> str:
    value, _ = redact_exfiltration_urls(value)
    value, _ = redact_credentials(value)
    return value


async def capture_launch_workspace(info: Any) -> None:
    """Observe the acquired session's directory before its spawn is broadcast.

    This is a launch snapshot, not a process-CWD tracker. Reuse the project
    browser's bounded Git metadata reader (including its sensitive-path gate).
    An unavailable observation must never stop a subagent from launching.
    """
    cwd = getattr(info, "_session_cwd", "")
    if not isinstance(cwd, str) or not cwd:
        return
    workspace = {"cwd": _redact(cwd)}
    info.launch_workspace = workspace
    try:
        from kiro_crew.dashboard.handlers.files import _project_git_branch

        git = await asyncio.to_thread(_project_git_branch, cwd)
        for source, target in (("repoRoot", "worktree"), ("branch", "branch"), ("head", "head")):
            value = git.get(source)
            if isinstance(value, str) and value:
                workspace[target] = _redact(value)
    except Exception:
        logger.debug("Subagent launch Git context unavailable", exc_info=True)


def workspace_event_fields(info: Any) -> dict[str, Any]:
    """Same optional fields on live lifecycle events and reconnect replay."""
    fields: dict[str, Any] = {}
    backend = getattr(info, "acp_backend", "")
    if isinstance(backend, str) and backend:
        fields["backend"] = _redact(backend)
    workspace = getattr(info, "launch_workspace", None)
    if isinstance(workspace, dict):
        fields["workspace"] = {
            key: _redact(value)
            for key in _WORKSPACE_KEYS
            if isinstance(value := workspace.get(key), str) and value
        }
    return fields
