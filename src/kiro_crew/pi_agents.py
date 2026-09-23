"""Pi user-agent profiles under ``~/.pi/agent/agents``.

Pi sessions do not run KiroCrew's ``~/.kiro/agents`` specs. The profiles a Pi
host can actually name are markdown files with YAML frontmatter, discovered
the same way Pi itself walks that directory (nested folders included,
``*.chain.md`` excluded). This module only reads those files. It does not
spawn Pi, and it does not turn on the experimental Crew ``droid`` backend.
"""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew.validation import _AGENT_NAME_RE


def pi_user_agents_dir() -> Path:
    """The directory Pi reads user agent profiles from.

    ``KIROCREW_PI_AGENTS_DIR`` overrides the location for tests and for an
    install that keeps the profiles somewhere other than the default home.
    """
    override = os.environ.get("KIROCREW_PI_AGENTS_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".pi" / "agent" / "agents"


def _frontmatter(text: str) -> dict[str, str]:
    """The ``key: value`` pairs between the opening ``---`` fences.

    Pi profiles are small. A full YAML parser is not required, and skipping
    it keeps this reader from depending on PyYAML. Nested blocks and list
    items are ignored; the fields the picker needs (``name``, ``description``,
    ``model``, ``thinking``) are scalars.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line or line[0] in " \t#":
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def _contained(root: Path, path: Path) -> bool:
    """True when *path* resolves inside *root* (symlink escape rejected)."""
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def list_pi_agent_profiles(root: Path | None = None) -> list[dict[str, str]]:
    """Profiles selectable as chat templates.

    Raises ``FileNotFoundError`` when the directory is absent and ``OSError``
    when it cannot be listed. An empty directory returns ``[]`` — that is a
    real catalog, not a failure, and it must not be filled with Kiro agents.
    """
    base = root if root is not None else pi_user_agents_dir()
    if not base.is_dir():
        raise FileNotFoundError(str(base))
    try:
        paths = sorted(p for p in base.rglob("*.md") if p.is_file())
    except OSError as exc:
        raise OSError(f"unreadable pi agents dir: {base}") from exc
    profiles: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in paths:
        if path.name.endswith(".chain.md"):
            continue
        if not _contained(base, path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _frontmatter(text)
        name = (meta.get("name") or path.stem).strip()
        if not _AGENT_NAME_RE.fullmatch(name) or name in seen:
            continue
        seen.add(name)
        profiles.append(
            {
                "name": name,
                "description": meta.get("description", ""),
                "model": meta.get("model", ""),
                "reasoning_effort": meta.get("thinking", ""),
            }
        )
    return profiles


def is_pi_user_profile(name: str, root: Path | None = None) -> bool:
    """True when *name* is a profile Pi would discover under *root*."""
    if not name or not _AGENT_NAME_RE.fullmatch(name):
        return False
    try:
        profiles = list_pi_agent_profiles(root)
    except OSError:
        return False
    return any(profile["name"] == name for profile in profiles)
