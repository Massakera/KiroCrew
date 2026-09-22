"""The two on-disk forms of an agent spec, and the one parser for both.

kiro-cli reads an agent from ``~/.kiro/agents/<name>.json``. The v3 engine (KAS,
and Kiro IDE) also reads ``<name>.md``: YAML frontmatter carrying the same fields
as the JSON object, with the markdown body as the agent's system prompt. Both are
the same spec in a different serialization, so every scan of an agents directory
goes through this module rather than a bare ``glob("*.json")`` -- a scan that
sees only one form lists an agent kiro-cli would run, or projects onto KAS an
agent that is not there.

This is a leaf module on purpose: it imports nothing from ``kiro_crew``, so the
config loader, the MCP gateway rewriter and the discovery cache can all reach it
without an import cycle. It parses bytes it is handed and never opens a file --
the hardened read (size cap, sensitive-symlink refusal) stays with the caller.

An agents directory is a TREE, not a flat list: the v3 engine walks it and
names an agent by its path relative to the directory with the suffix removed and
``/`` as the separator, so ``~/.kiro/agents/team/planner.md`` is the agent
``team/planner``. :func:`iter_agent_spec_files` walks the same way and
:func:`spec_relname` derives the same id, so a spec in a subdirectory is one
agent everywhere rather than an agent the roster omits and a session cannot
select. A file directly in the directory keeps its plain stem, so nothing about
a flat install changes. The walk is bounded by :data:`MAX_AGENT_SPEC_DEPTH`,
which is also the bound ``validation.AGENT_ID_RE`` is built from so a spec can
never be found under a name the selection surfaces reject. A directory whose name
begins with ``.`` is not descended into: that is where this directory's tools keep
state (Kiro Crew's own skill-projection leases live in one, and every file in it
is JSON), never where an author puts an agent. A symlinked DIRECTORY is not
descended into either, because a spec inside one resolves outside the agents
directory and ``agent._spec_path_is_safe`` refuses it -- discovering an agent no
resolver will find is worse than not discovering it. A symlinked FILE is still
read, which is the case authors use and the hardened reader already covers.

Because an id can now carry ``/``, an id turned back into a filename is a path
join on caller-supplied text: :func:`is_safe_agent_relname` is the one rule for
what may be joined, and :func:`agent_spec_candidates` yields nothing for a name
that escapes the directory.

``<name>.json`` beside ``<name>.md`` is one agent authored twice -- the JSON twin
is the workaround users kept while only JSON was read -- and the JSON wins:
the iterator drops the shadowed markdown file, so every consumer sees one
agent, and the roster warns so the author knows which file is live. Twins are
per DIRECTORY, which is what a twin is -- one agent authored twice:
``team/planner.json`` and ``planner.md`` are the two agents ``team/planner`` and
``planner``, and neither shadows the other. Two
files of the SAME form declaring one name stay an ambiguity for the callers
that already refuse it.

A markdown spec is one file, so the body IS the prompt. When the body has
content it is the prompt even if the frontmatter also declares ``prompt``; a
frontmatter ``prompt`` is honoured only for a body-less file, so a spec that
points at a ``file://`` prompt still resolves. The frontmatter must be a
mapping; ``---`` opens it at byte 0 (after an optional UTF-8 BOM, which a
Windows editor adds and which would otherwise turn the whole document into
prose) and a line that is exactly ``---`` closes it, so a ``---junk`` line is
body text and configuration never leaks into the prompt.
"""

from __future__ import annotations

import json
import math
import ntpath
import os
import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

#: Suffixes an agents directory entry may carry, lower-case. ``.json`` is the
#: kiro-cli form; ``.md`` is the markdown form.
JSON_SUFFIX = ".json"
MARKDOWN_SUFFIX = ".md"
AGENT_SPEC_SUFFIXES: tuple[str, ...] = (JSON_SUFFIX, MARKDOWN_SUFFIX)
NATIVE_SKILL_ALIAS_PREFIX = "kirocrew-skill-view-"

#: How many directories below an agents directory the scan descends. The v3
#: engine applies no depth limit -- but every roster read, every per-turn model
#: resolution and every gateway fingerprint walks this tree, so the cost of a
#: pathological one is paid on a user's keystroke. Agent ids are typed by hand;
#: nesting is for grouping a handful of agents, not for mirroring a source tree.
#:
#: This is ALSO the bound the wire grammar is built from
#: (``validation.AGENT_ID_RE``), so the two cannot disagree. They must not: a
#: spec the walk finds one level below where the grammar stops would be listed
#: under a name every selection surface rejects, which is the original defect
#: (an agent that exists and cannot be run) one level deeper.
MAX_AGENT_SPEC_DEPTH = 4

#: Path segments an agent id may not contain. ``..`` is the one that matters --
#: an id is joined onto an agents directory -- and ``.``/empty are refused with
#: it so one rule covers every spelling that does not name a child.
_UNSAFE_ID_SEGMENTS = frozenset({"", ".", ".."})

_FRONTMATTER_CLOSE_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)


class _FrontmatterLoader(yaml.SafeLoader):  # type: ignore[misc]
    """``SafeLoader`` that yields a JSON-shaped TREE and nothing else.

    The parsed frontmatter is re-serialized as JSON on every consuming path
    (the KAS projection, the dashboard roster, the rewriter's overlay), so a
    document JSON cannot carry is not a quirk but a crash there. Two features of
    YAML have no JSON form and are removed at the loader rather than detected
    afterwards:

    * **Aliases** (``*name``) are refused at compose time; an ``&name`` anchor
      nothing references parses as its plain value, the same rule the other
      alias-refusing loaders in this package apply. An alias is a shared
      reference, and a shared reference is what makes a document a graph: a self-reference recurses forever, and a chain of
      shared containers expands exponentially when it is serialized -- which
      every consumer does. Without aliases the loaded value is a tree whose
      size is bounded by the file's, so the validation below is one linear
      walk with no identity tracking. An agent spec has no use for an alias
      that a repeated literal does not serve.
    * **Timestamps**: PyYAML's 1.1 resolver turns an unquoted ISO date
      (``args: [YYYY-MM-DD]``) into ``datetime.date``; kiro-cli's own YAML
      reader has no date type and reads the same scalar as the string the
      author typed, so the timestamp constructor is replaced to do the same.

    Anything else that is not JSON (an explicit ``!!binary`` / ``!!set``, a
    non-string key, an infinity) is rejected by :func:`_require_json_shape`
    after loading rather than mapped, since there is no faithful mapping.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.get_event()
            raise yaml.composer.ComposerError(
                None,
                None,
                f"aliases (*{event.anchor}) are not supported in agent frontmatter; "
                "repeat the value instead",
                event.start_mark,
            )
        return super().compose_node(parent, index)


def _construct_timestamp_as_text(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    return loader.construct_scalar(node)  # type: ignore[arg-type]


_FrontmatterLoader.add_constructor("tag:yaml.org,2002:timestamp", _construct_timestamp_as_text)


def _require_json_shape(value: Any, where: str) -> None:
    """Raise ``ValueError`` unless *value* is a finite JSON document.

    JSON values only: ``dict`` with ``str`` keys, ``list``, ``str``, ``bool``,
    ``int``, finite ``float``, ``None``. The loader admits no aliases, so *value*
    is a tree and one pass over it is linear in the file size; a depth the
    interpreter cannot recurse is caught by the caller. *where* names the
    offending key path in the error, since the author has to find it in a file
    whose other fields all parsed.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"frontmatter value at {where} is not a finite number")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"frontmatter key {key!r} at {where} is not a string; quote it")
            _require_json_shape(item, f"{where}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_shape(item, f"{where}[{index}]")
        return
    raise ValueError(
        f"frontmatter value at {where} is a YAML {type(value).__name__}, which has no JSON form"
    )


def spec_suffix(name: str | Path) -> str | None:
    """The spec suffix of *name* (``.json`` / ``.md``), or ``None`` for neither.

    Case-insensitive: a case-insensitive filesystem serves ``Foo.JSON`` to a
    ``glob("*.json")`` consumer, so the check here must match what a scan sees.
    """
    # Anything path-like with a ``.name`` is accepted, not only ``Path``: the
    # hardened reader is handed duck-typed paths on some platforms' probe gates.
    lowered = (name if isinstance(name, str) else name.name).lower()
    for suffix in AGENT_SPEC_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    return None


def is_agent_spec_name(name: str) -> bool:
    """Whether a directory entry name is an agent spec of either form."""
    return spec_suffix(name) is not None


def is_markdown_spec(path: str | Path) -> bool:
    """Whether *path* is the markdown form."""
    return spec_suffix(path) == MARKDOWN_SUFFIX


def spec_stem(name: str) -> str:
    """The filename with its spec suffix removed; *name* unchanged otherwise."""
    suffix = spec_suffix(name)
    return name[: -len(suffix)] if suffix else name


def _has_json_twin(directory: Path, md: Path, json_stems: set[str]) -> bool:
    """Whether ``<md.stem>.json`` names an existing entry beside *md*.

    The stem set answers the exact-case twin. The filesystem answers the rest:
    on a case-insensitive filesystem (Windows, macOS by default) ``Foo.json``
    and ``foo.md`` are twins too -- their ``<stem>.json`` overlays would be ONE
    file there, so the second overlay written would hand one agent the other's
    MCP servers -- and the only authority on which names collide is the
    filesystem itself, asked for the name this file's overlay would take. On a
    case-sensitive filesystem the probe misses and the two stay distinct
    agents, which is what their distinct overlay files are.
    """
    if md.stem in json_stems:
        return True
    return (directory / f"{md.stem}{JSON_SUFFIX}").exists()


def _scan_spec_dirs(root: Path) -> list[tuple[Path, list[Path]]]:
    """``(directory, its spec files)`` for *root* and every directory under it.

    Breadth-first from *root*, each directory's entries sorted by name, so the
    order is the same on every platform and every run. Bounded by
    :data:`MAX_AGENT_SPEC_DEPTH`, and a directory whose name begins with ``.``
    is skipped: that is where a tool keeps state, never where an author puts an
    agent.

    A symlinked DIRECTORY is not descended into, and neither is a Windows
    JUNCTION -- which needs no privilege to create, answers ``True`` to
    ``is_dir(follow_symlinks=False)`` and ``False`` to ``is_symlink()``, so the
    symlink rule alone would let the walk through it on Windows and nowhere else.
    A spec reached through either resolves outside the agents directory, and
    ``agent._spec_path_is_safe`` refuses exactly that -- so following the link would list an agent that the
    resolver every writer and the capability layer go through then declines to
    find. An agent that is discovered and unresolvable is worse than one that is
    neither, because the surfaces disagree about whether it exists. A symlinked
    FILE is still followed, which is the case authors actually use (a spec linked
    to a checked-in copy) and the case the hardened reader already covers by
    checking the resolved target.

    Every ``OSError`` is swallowed and the directory contributes nothing, which
    is what ``Path.glob`` does for an unreadable or missing directory on every
    supported version -- so a caller's existing ``except OSError`` stays correct
    and a caller without one is no more exposed than before. A broken symlink is
    neither a file nor a directory here and is skipped by the same rule. The
    resolved-directory set remains the guard against a repeat from any other
    cause (a bind mount inside the tree), so no directory is ever read twice.
    """
    found: list[tuple[Path, list[Path]]] = []
    visited: set[str] = set()
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        directory, depth = queue.pop(0)
        try:
            resolved = os.path.realpath(directory, strict=True)
        except OSError:
            continue
        if resolved in visited:
            continue
        visited.add(resolved)
        files: list[Path] = []
        subdirs: list[Path] = []
        try:
            with os.scandir(directory) as entries:
                for entry in sorted(entries, key=lambda e: e.name):
                    try:
                        # A real directory entry only: ``follow_symlinks=False``
                        # excludes a symlink and ``is_junction`` the Windows
                        # reparse point that answers True to it anyway. ``is_file``
                        # DOES follow the link, so a symlinked spec still loads,
                        # and a broken link answers False to both.
                        if entry.is_dir(follow_symlinks=False) and not entry.is_junction():
                            # A DOTTED directory holds state, not authored
                            # agents: Kiro Crew keeps the skill-projection
                            # leases in ``.kirocrew-skill-projection-leases``
                            # right here, and every one of those files is JSON --
                            # walked, they would each be listed as an agent. The
                            # v3 engine has no such directory to skip, so this is
                            # a divergence the shared directory forces, and the
                            # rule is the conventional one rather than a list of
                            # Kiro Crew's own names: an author grouping agents
                            # names the folder, and a tool hiding state dots it.
                            if not entry.name.startswith("."):
                                subdirs.append(directory / entry.name)
                        elif entry.is_file() and is_agent_spec_name(entry.name):
                            files.append(directory / entry.name)
                    except OSError:
                        continue
        except OSError:
            continue
        found.append((directory, files))
        if depth >= MAX_AGENT_SPEC_DEPTH:
            continue
        for subdir in subdirs:
            queue.append((subdir, depth + 1))
    return found


def spec_relname(directory: Path, path: Path) -> str:
    """The agent id *path* carries by where it sits under *directory*.

    The path relative to the scan root, spec suffix removed, ``/`` as the
    separator -- the id the v3 engine derives for the same file, so one spec is
    one agent on both hosts. ``<agents>/team/planner.md`` is ``team/planner``
    and ``<agents>/planner.md`` is ``planner``, so a flat install reads exactly
    as it did.

    Falls back to the plain stem when *path* is not under *directory*: a caller
    holding a path from elsewhere gets the name it got before rather than a
    ``ValueError`` raised in the middle of a roster scan.
    """
    try:
        relative = path.relative_to(directory)
    except ValueError:
        return spec_stem(path.name)
    return spec_stem(relative.as_posix())


def is_safe_agent_relname(name: str) -> bool:
    """Whether *name* may be joined onto an agents directory as a relative path.

    An id is a path relative to the scan root, so turning one back into a
    filename joins caller-supplied text onto a directory a caller then reads or
    writes. Refused: an empty name, a NUL, a backslash (ids spell the separator
    ``/``; a Windows separator would resolve as one there and not here, so the
    same id would name two different files), an absolute or drive-qualified
    path, and any segment in :data:`_UNSAFE_ID_SEGMENTS` -- which is what makes
    ``../../.aws/credentials`` unusable as an agent name.

    A name the walk derived always passes: it is built from real path segments
    under the root. The rule is for the other direction -- a name off the wire,
    a ``--agent`` argument, a URL path segment.
    """
    if not name or "\x00" in name or "\\" in name:
        return False
    if name.startswith("/") or ntpath.splitdrive(name)[0]:
        return False
    return not any(segment in _UNSAFE_ID_SEGMENTS for segment in name.split("/"))


def _split_spec_files(directory: Path) -> tuple[list[Path], list[Path]]:
    """``(live, shadowed)``: every spec file at or under *directory*, with
    ``<stem>.md`` beside ``<stem>.json`` set aside.

    The twin rule is applied per directory, because that is the pair it is
    about: ``<stem>.json`` and ``<stem>.md`` in ONE directory are one agent
    authored twice, while the same stem in two directories is two agents with
    two ids.
    """
    live: list[Path] = []
    shadowed: list[Path] = []
    for folder, files in _scan_spec_dirs(directory):
        json_files = [p for p in files if spec_suffix(p.name) == JSON_SUFFIX]
        json_stems = {p.stem for p in json_files}
        live.extend(json_files)
        for path in files:
            if spec_suffix(path.name) != MARKDOWN_SUFFIX:
                continue
            (shadowed if _has_json_twin(folder, path, json_stems) else live).append(path)
    return live, shadowed


def iter_agent_spec_files(directory: Path, *, ordered: bool = True) -> list[Path]:
    """Every live spec file at or under *directory*, both forms.

    The walk descends into subdirectories, so a spec grouped in a folder is
    returned too; :func:`spec_relname` turns each path into the agent's id. A
    ``<stem>.md`` whose ``<stem>.json`` twin sits in the SAME directory is not
    returned: the JSON wins (see the module docstring), and
    :func:`shadowed_markdown_specs` names the files this dropped. An unreadable,
    missing or looping directory contributes nothing rather than raising,
    exactly as ``Path.glob`` behaves, so a caller that handled the JSON-only
    glob handles this unchanged. *ordered* sorts by full path so the order is
    stable across platforms; ``ordered=False`` keeps the walk's own order --
    each directory's entries by name, JSON before markdown within a directory --
    for the first-match resolvers that stop at the first hit.
    """
    live, _shadowed = _split_spec_files(directory)
    live = [path for path in live if not path.stem.startswith(NATIVE_SKILL_ALIAS_PREFIX)]
    return sorted(live) if ordered else live


def shadowed_markdown_specs(directory: Path) -> list[Path]:
    """The ``<stem>.md`` files a same-directory ``<stem>.json`` twin hides, sorted."""
    try:
        _live, shadowed = _split_spec_files(directory)
    except OSError:
        return []
    return sorted(shadowed)


def agent_spec_candidates(directory: Path, name: str) -> list[Path]:
    """The paths ``<name>.json`` and ``<name>.md`` under *directory*, existing or not.

    JSON first, so a caller that takes the first existing candidate applies the
    JSON-wins rule for a twin without restating it.

    *name* may be a nested id: ``team/planner`` names
    ``<directory>/team/planner.json`` and ``.md``, which is what makes an agent
    the walk found in a subdirectory resolvable by the id the walk gave it.

    ``[]`` for a name :func:`is_safe_agent_relname` refuses. Every candidate is
    a path a caller goes on to read or write, so a name that escapes the agents
    directory yields no candidate at all rather than a path outside the tree
    that happens not to exist yet.
    """
    if not is_safe_agent_relname(name):
        return []
    return [directory / f"{name}{suffix}" for suffix in AGENT_SPEC_SUFFIXES]


def split_markdown_spec(text: str) -> tuple[str, str] | None:
    """Split a markdown spec into ``(frontmatter_yaml, body)``.

    ``None`` when the document does not open with a frontmatter fence -- a plain
    markdown file dropped into the agents directory is not a spec and must not
    be listed as one. A fence with no closing line is also ``None``: the whole
    file would otherwise parse as YAML and a prompt would be mistaken for config.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        return None
    first_newline = text.index("\n")
    close = _FRONTMATTER_CLOSE_RE.search(text, first_newline + 1)
    if close is None:
        return None
    frontmatter = text[first_newline + 1 : close.start()]
    body = text[close.end() :]
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return frontmatter, body


def _load_frontmatter(text: str) -> Any:
    """Parse ONE YAML document with :class:`_FrontmatterLoader`.

    Driving the loader instance is what ``yaml.load`` does with an explicit
    ``Loader=``, so the parse is identical -- but the SafeLoader subclass is the
    only construction path here, with no ``yaml.load`` call whose safety a
    reader (or a scanner keyed on the call name) has to infer from the
    ``Loader=`` argument. The repo's ``test_yaml_safe_loading`` guard pins that.
    """
    loader = _FrontmatterLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def parse_markdown_spec(text: str) -> dict[str, Any]:
    """Parse a markdown spec into the same dict shape a JSON spec loads to.

    Raises ``ValueError`` -- the same class ``json.loads`` raises for a bad
    JSON spec -- when the fence is missing or unclosed, the frontmatter is not
    valid YAML, is valid YAML that is not a mapping, or holds a value JSON
    cannot carry (see :class:`_FrontmatterLoader`). The safe loader only: the
    agents directory is user-writable and shared with other tools.
    """
    parts = split_markdown_spec(text)
    if parts is None:
        raise ValueError("markdown agent spec has no closed '---' frontmatter fence")
    frontmatter, body = parts
    try:
        loaded = _load_frontmatter(frontmatter) if frontmatter.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"markdown agent spec frontmatter is not valid YAML: {exc}") from exc
    except RecursionError as exc:
        # PyYAML composes and constructs nested collections recursively, so a
        # frontmatter nested hundreds of levels deep (well within the size cap)
        # exhausts the interpreter stack. That is bad content, not a crash the
        # caller should see: same class as any other unparseable frontmatter.
        raise ValueError("markdown agent spec frontmatter is nested too deeply") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError("markdown agent spec frontmatter is not a mapping")
    try:
        _require_json_shape(loaded, "frontmatter")
    except RecursionError as exc:
        raise ValueError("markdown agent spec frontmatter is nested too deeply") from exc
    except ValueError as exc:
        raise ValueError(f"markdown agent spec {exc}") from exc
    spec: dict[str, Any] = dict(loaded)
    tools = spec.get("tools")
    if isinstance(tools, str) and tools.strip() != "*":
        # The v3 loader accepts ``tools: read, write`` as a comma-separated
        # string as well as a YAML list; the JSON shape is the list, so the
        # string is normalized here and every consumer sees one shape. An
        # empty string means no tools, which the JSON form spells by omission.
        entries = [t.strip() for t in tools.split(",") if t.strip()]
        if entries:
            spec["tools"] = entries
        else:
            spec.pop("tools")
    if body.strip():
        # Leading blank lines are the gap authors leave after the fence, not
        # prompt text; trailing whitespace is kept as written.
        spec["prompt"] = body.lstrip("\r\n")
    return spec


def parse_agent_spec_text(text: str, path: str | Path) -> Any:
    """Parse *text* as the spec form *path*'s suffix names.

    Returns whatever the document holds -- callers reject a non-dict exactly as
    they did for JSON -- and raises ``ValueError`` for either form's syntax
    errors, so one ``except ValueError`` covers both. A path with neither
    suffix is parsed as JSON, the historical behaviour of every caller.
    """
    if is_markdown_spec(path):
        return parse_markdown_spec(text)
    return json.loads(text)


def parse_agent_spec_bytes(raw: bytes, path: str | Path) -> Any:
    """:func:`parse_agent_spec_text` over UTF-8 bytes; ``UnicodeDecodeError`` propagates."""
    return parse_agent_spec_text(raw.decode("utf-8"), path)
