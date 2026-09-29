"""Where Kiro Crew places the git worktrees it creates.

Every producer (the task runner's isolated run, Spec Builder's per-spec branch,
an Issue Radar crew's per-issue worktree)
lays its worktrees out under ONE root, ``<root>/<repo>/<name>``, so the operator
finds every checkout of every repository in one place instead of beside each
repository under a producer-specific name. The root is ``dev_fleet.worktrees_root``
in ``config.json`` (``~`` expanded), defaulting to ``~/worktrees``.

The setting lives in the app-owned ``dev_fleet`` section because Dev Fleet is the
surface that lists and prunes these worktrees; this module reads it directly so
producers outside that app do not import it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT_ROOT = "~/worktrees"

#: One path segment: no separator, no leading dot (so never ``.`` or ``..``).
_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9._-]{0,127}\Z")


def _configured_root() -> str:
    """``dev_fleet.worktrees_root`` from the config files, or ``""``. Blocking."""
    try:
        from kiro_crew.config.loader import config_dir

        base = config_dir()
    except Exception:  # noqa: BLE001 - a missing data home means "use the default"
        return ""
    value = ""
    for fname in ("config.json", "config.local.json"):
        try:
            raw = json.loads((base / fname).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        section = raw.get("dev_fleet") if isinstance(raw, dict) else None
        if isinstance(section, dict) and isinstance(section.get("worktrees_root"), str):
            value = section["worktrees_root"].strip() or value
    return value


def worktrees_root() -> Path:
    """The absolute root every created worktree lives under. Blocking (config read).

    ``KIROCREW_WORKTREES_ROOT`` overrides the config. A relative value is refused
    in favour of the default: it would resolve against whatever directory the
    creating process happens to run in.
    """
    override = os.environ.get("KIROCREW_WORKTREES_ROOT", "").strip()
    root = Path(override or _configured_root() or DEFAULT_ROOT).expanduser()
    if not root.is_absolute():
        root = Path(DEFAULT_ROOT).expanduser()
    return root


def segment(value: str, what: str = "path segment") -> str:
    """*value* unchanged when it is one safe path segment; ``ValueError`` otherwise."""
    if not _SEGMENT_RE.match(value):
        raise ValueError(f"{what} {value!r} is not a single safe path segment")
    return value


def worktree_path(repo_root: str | Path, name: str) -> Path:
    """``<root>/<repo name>/<name>`` for a worktree of the checkout at *repo_root*.

    Blocking (config read). *repo_root* is the primary checkout; its directory name
    groups the worktrees of one repository. Raises ``ValueError`` when either
    segment could escape the root.
    """
    repo = segment(Path(repo_root).name, "repository name")
    return worktrees_root() / repo / segment(name, "worktree name")


__all__ = ("DEFAULT_ROOT", "segment", "worktree_path", "worktrees_root")
