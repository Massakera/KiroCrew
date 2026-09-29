"""Tool projection for an explicitly Crew-managed pi process.

This is a subset of a Crew spec, not an importer for pi-subagents profiles.
Only pi built-ins and tools on Crew's existing sealed bridge can be selected.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any, Mapping

# Native Crew spellings and pi's equivalent read/write primitives. No command
# tool is implied by a read grant; a reviewer never needs a shell to search.
_BUILTINS: dict[str, tuple[str, ...]] = {
    "fs_read": ("read", "grep", "find", "ls"),
    "fs_write": ("edit", "write"),
    "execute_bash": ("bash",),
    "glob": ("find",),
    **{name: (name,) for name in ("read", "grep", "find", "ls", "edit", "write", "bash")},
}


def managed_pi_tools(spec: Any, bridge_tools: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Project an explicit spec; missing/malformed inputs never mean all tools.

    Unknown native names have no pi implementation and grant nothing. MCP refs
    are narrowed per tool (unlike a transport that can only mount whole servers).
    The bridge's inventory remains the ceiling; a spec cannot add another server.
    """
    if not isinstance(spec, dict) or not isinstance(spec.get("tools"), list):
        raise ValueError("managed pi requires an agent spec with an explicit tools list")
    if any(not isinstance(tool, str) for tool in spec["tools"]):
        raise ValueError("managed pi requires string tool names")
    refs = set(spec["tools"])
    selected: set[str] = set()
    for name, tools in _BUILTINS.items():
        if name in refs or "*" in refs or "@builtin" in refs:
            selected.update(tools)
    servers = spec.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("managed pi requires an object for mcpServers")
    for server, tools in bridge_tools.items():
        settings = servers.get(server, {})
        if not isinstance(settings, dict):
            raise ValueError("managed pi requires object MCP server settings")
        if settings.get("disabled") is True:
            continue
        disabled = settings.get("disabledTools", [])
        if not isinstance(disabled, list) or any(not isinstance(p, str) for p in disabled):
            raise ValueError("managed pi requires a string list for disabledTools")
        for tool in tools:
            granted = "*" in refs or f"@{server}" in refs or f"@{server}/{tool}" in refs
            if granted and not any(fnmatchcase(tool, pattern) for pattern in disabled):
                selected.add(f"mcp__{server}__{tool}")
    return tuple(sorted(selected))


def managed_pi_grants(spec: Any, managed_tools: tuple[str, ...]) -> frozenset[str]:
    """The ``@server/tool`` refs a spec's ``allowedTools`` pre-approves, among mounted tools.

    Only MCP refs grant. A native name (``fs_write``) or a bare glob (``*``) grants
    nothing here, so pi's own read/edit/write/bash keep asking; the grant exists for
    the bridged Crew tools a kiro-cli session would run unprompted. The result is
    concrete refs rather than patterns so the runtime check is an equality, and it
    can never name a tool the session was not given.
    """
    if not isinstance(spec, dict):
        return frozenset()
    allowed = spec.get("allowedTools")
    if not isinstance(allowed, list):
        return frozenset()
    mounted: list[str] = []
    for name in managed_tools:
        server, sep, tool = name.removeprefix("mcp__").partition("__")
        if name.startswith("mcp__") and sep and server and tool:
            mounted.append(f"@{server}/{tool}")
    granted: set[str] = set()
    for entry in allowed:
        if not isinstance(entry, str) or not entry.startswith("@"):
            continue
        server, _, tool = entry[1:].partition("/")
        if not server:
            continue
        pattern = f"@{server}/{tool or '*'}"
        granted.update(ref for ref in mounted if fnmatchcase(ref, pattern))
    return frozenset(granted)


def managed_profile_issue(commands: Any, extension_path: str) -> str:
    """Require the profile's probe to originate in the separately sealed copy."""
    if extension_path and isinstance(commands, list):
        for command in commands:
            if not isinstance(command, dict) or command.get("name") != "kiro-crew-managed":
                continue
            source = command.get("sourceInfo")
            if isinstance(source, dict) and source.get("path") == extension_path:
                return ""
    return "the managed pi profile is absent or was not loaded from Kiro Crew's sealed file"
