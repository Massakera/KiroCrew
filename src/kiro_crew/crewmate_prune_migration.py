"""One-time startup migration: prune the crewmates an older agent sync generated.

An enrol-on-mount build of the dashboard called ``POST /api/agents/sync`` on
every chat mount, and that sync enrolled EVERY user-authored spec under
``~/.kiro/agents`` as a crewmate --
a ``config.agents`` row with no ``member_id``, on the shared ``default`` memory
store, bound to the spec by name. An existing install therefore carries one
crewmate per custom agent, most of them never opened. This module runs once at
gateway startup and:

* **removes** each such crewmate that was never chatted with as a crewmate --
  no DM thread on the Crewmates page and no session that recorded it as its
  agent -- by deleting its ``config.agents`` row (:func:`remove_never_chatted`);
* **leaves the chatted ones exactly as they are**: on the shared ``default``
  store, with no ``member_id``. A memory binding is identity and is chosen only
  at creation; an existing member keeps its exact V1 binding (see
  ``memory-skills-hooks.md``, "Member memory experience and lifecycle"), and no
  startup pass rewrites it.

Design:

* **Precise identification.** A row is a candidate only when it is EXACTLY
  what the sync wrote: its name is its ``kiro_agent``, that spec is on disk,
  user-authored (``source == "builtin"``), not the runtime's own and not a
  crew's private copy, and every field other than ``description`` sits at its
  default -- no ``member_id``, the shared ``default`` store, no model, effort,
  triggers, colour, star, avatar or workspace (:func:`_is_fresh_sync_shape`,
  tested on the RAW row as the file holds it, a missing key reading as its
  default). A row the owner touched in any of those ways is the owner's and
  is never a candidate; a hand-made crewmate has a ``member_id``; a package's
  spec has another source; a row whose spec is gone is left alone.
* **Fail closed on unreadable history.** Removal is decided from chat history
  read STRICTLY: every session file's metadata line is statted, opened and
  parsed by this module (:func:`_agents_named_in_history`), and the DM binding
  file is read by this module (:func:`_chatted`) -- never through the roster's
  total-by-contract readers, which answer "absent" for a damaged directory or
  file. Any failure raises :class:`HistoryUnreadable`: nothing further is
  removed and the marker is NOT written, so the pass runs again next boot. A
  crewmate is never deleted on missing evidence.
* **Serialized against thread creation.** The gateway clears
  ``DashboardState.crewmate_prune_settled`` before the pass and sets it after;
  ``api_member_thread`` -- the one route that writes a DM binding -- waits on
  it, so no thread can be opened between a candidate's check and its delete.
  Each candidate's check runs immediately before its own removal, never once
  for the whole list. The pass runs AFTER the listener binds, off the boot
  path, and before the slot restores.
* **A refused delete is not a commit.** The delete re-tests the row inside
  the config lock; a row that changed meanwhile is refused, the pass writes no
  marker and logs which rows, and the next boot re-judges them.
* **Agent files are never touched.** Only ``config.json`` rows move. The specs
  under ``~/.kiro/agents`` and any transcript on disk stay exactly as they are.
* **Idempotent, marker-gated.** A completed pass (even a no-op) writes
  :data:`PRUNE_MARKER` under the config directory with what it did -- the same
  marker-file seam the config loader's own one-shot migrations use
  (``CONNECTIONS_UI_MIGRATION_MARKER``); the next boot finds the marker and
  returns at once. It runs from ``start_dashboard`` rather than inside the
  loader because the decision needs chat history, which only the running
  gateway has. Removed rows do not come back: nothing in the dashboard calls
  ``POST /api/agents/sync`` (``useAgents`` reads the catalog), so the rows this
  pass removes come only from installs that ran an enrol-on-mount build.
* **Snapshot-safe writes.** Each removal is a delta mutate under the config
  lock that deletes the row only while the on-disk entry still equals the
  snapshot it was judged on; a row edited or re-created between the snapshot
  and the lock is newer evidence and survives.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    coerce_dict_section,
    update_config_locked,
)
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: Written under the config directory once a pass completes. Its body is the
#: record of what the pass did, so an operator can see which crewmates left.
PRUNE_MARKER = "crewmate_prune_migrated.json"

#: The discovery ``source`` of a user-authored spec under ``~/.kiro/agents``
#: (``kirocrew`` = the runtime's own, ``package`` = installed by a package).
#: Tested on the SPEC the row is bound to, never on the row's own stamp.
USER_SPEC_SOURCE = "builtin"


class HistoryUnreadable(RuntimeError):
    """Chat history could not be read; the pass must not remove anything."""


@dataclasses.dataclass
class PruneReport:
    removed: list[str] = dataclasses.field(default_factory=list)
    kept: list[str] = dataclasses.field(default_factory=list)
    refused: list[str] = dataclasses.field(default_factory=list)
    skipped_marker: bool = False


def marker_path() -> Path:
    return config_dir() / PRUNE_MARKER


def _raw_agents_section() -> dict:
    """The ``agents`` section exactly as ``config.json`` holds it."""
    from kiro_crew.config.loader import read_config_for_update

    doc = read_config_for_update()
    agents = doc.get("agents") if isinstance(doc, dict) else None
    return agents if isinstance(agents, dict) else {}


def _fresh_sync_row(kiro_agent: str, description: str, source: str) -> dict:
    """What ``_do_agents_sync`` wrote for one spec: the binding, the spec's
    description and source, every other field at its default."""
    return dataclasses.asdict(
        KiroCrewAgentConfig(kiro_agent=kiro_agent, description=description, source=source)
    )


def _is_fresh_sync_shape(raw: dict, *, kiro_agent: str) -> bool:
    """Whether a RAW ``config.agents`` row is exactly a sync-written row.

    Compared field by field against :func:`_fresh_sync_row` with the row's own
    description, so a description edit alone does not disqualify (the sync
    copies it from the spec, and specs change); every other field must equal
    its default -- a row the owner gave a model, triggers, a colour, a star, an
    avatar or a workspace is the owner's and is never a candidate. A key the
    row lacks reads as its default: a row written by a build whose record had
    fewer fields is still the sync's row.
    """
    expected = _fresh_sync_row(kiro_agent, str(raw.get("description", "")), USER_SPEC_SOURCE)
    for key, default in expected.items():
        if raw.get(key, default) != default:
            return False
    return True


def _synced_candidates(cfg: KiroCrewConfig, raw_agents: dict) -> list[str]:
    """Names of the crewmates an older sync generated, in config order.

    ``raw_agents`` is the ``agents`` section as read from disk (not the
    default-filled dataclasses): the shape test must see the row the file
    holds, and the same test is re-run inside the delete's lock.
    """
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.agent_discovery import list_agents

    specs = {info.name: info for info in list_agents(agents_dir=kiro_agents_dir_path())}
    names: list[str] = []
    for name, agent in cfg.agents.items():
        if name in ("default", cfg.default_agent):
            continue
        raw = raw_agents.get(name)
        if not isinstance(raw, dict) or name != agent.kiro_agent:
            continue
        if not _is_fresh_sync_shape(raw, kiro_agent=agent.kiro_agent):
            continue
        spec = specs.get(agent.kiro_agent)
        if (
            spec is None
            or spec.source != USER_SPEC_SOURCE
            or spec.kirocrew_owned
            or spec.private_to
        ):
            continue
        if not spec.filename:
            continue
        names.append(name)
    return names


def _agents_named_in_history(conversation_log) -> set[str]:
    """Every agent any session's metadata line names -- read STRICTLY.

    ``ConversationLog.agent_usage()`` is built on ``list_sessions()``, which
    skips a file it cannot stat and swallows a first line it cannot read or
    parse; a removal must not read either as "this crewmate was never chosen".
    Here every ``*.jsonl`` in the history directory is statted, opened and its
    first line parsed, and ANY failure raises :class:`HistoryUnreadable`. A
    first line that is not a metadata record simply names no agent (that is
    the contract ``list_sessions`` applies too). Symlinks are aliases of files
    already in the walk and are skipped.
    """
    history_dir = getattr(conversation_log, "_dir", None)
    if not isinstance(history_dir, Path):
        raise HistoryUnreadable("conversation log exposes no history directory")
    named: set[str] = set()
    try:
        if not history_dir.exists():
            return named
        paths = list(history_dir.glob("*.jsonl"))
    except OSError as exc:
        raise HistoryUnreadable(f"could not list session history: {exc}") from exc
    for path in paths:
        try:
            if path.is_symlink():
                continue
            with open(path, encoding="utf-8") as fh:
                first = fh.readline().strip()
        except (OSError, UnicodeError) as exc:
            raise HistoryUnreadable(f"could not read session {path.name}: {exc}") from exc
        if not first:
            continue
        try:
            record = json.loads(first)
        except ValueError as exc:
            raise HistoryUnreadable(f"session {path.name} metadata does not parse: {exc}") from exc
        if isinstance(record, dict) and record.get("_type") == "metadata":
            agent = record.get("agent")
            if isinstance(agent, str) and agent:
                named.add(agent)
    return named


def _chatted(cfg: KiroCrewConfig, name: str, named: set[str]) -> bool:
    """Whether any chat history records this crewmate.

    Two sources, either suffices: a session whose metadata named it as the
    agent (``named``, from :func:`_agents_named_in_history`), or the crewmate's
    own DM thread on the Crewmates page --
    the thread route writes the binding file the first time the owner opens
    the thread, so a binding that names this crewmate IS the evidence, whether
    or not a message was ever sent.

    The binding is read STRICTLY here, not through ``read_dm_binding``: that
    reader is total by contract and answers "not bound" for an unreadable
    directory, an unreadable file and a malformed payload alike, which a
    removal must never mistake for "never opened". Only a binding file that
    does not exist reads as never opened. Every other failure -- the path
    cannot be resolved, the file cannot be statted or read, the payload does
    not parse -- raises :class:`HistoryUnreadable` so the caller fails closed.
    """
    from kiro_crew import members as members_mod
    from kiro_crew.atomic_write import read_bytes_with_retry

    if name in named:
        return True
    try:
        slug = members_mod.member_slug(name, cfg)
    except members_mod.MemberSlugError:
        # No slug means no DM thread can exist; the usage read above covers the rest.
        return False
    try:
        path = members_mod.dm_binding_path(slug)
    except members_mod.MemberSlugError:
        return False
    except Exception as exc:  # noqa: BLE001 -- an unresolvable path is "unknown", never "no"
        raise HistoryUnreadable(f"could not resolve {name!r}'s DM binding: {exc}") from exc
    try:
        raw = read_bytes_with_retry(path)
    except FileNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001 -- present but unreadable is "unknown"
        raise HistoryUnreadable(f"could not read {name!r}'s DM binding: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise HistoryUnreadable(f"{name!r}'s DM binding does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise HistoryUnreadable(f"{name!r}'s DM binding is not a record")
    # A colliding slug's binding belongs to exactly one crew name; one that
    # names another crew is legitimately not this crewmate's thread.
    return data.get("member") == name


def remove_never_chatted(cfg: KiroCrewConfig, names: list[str]) -> tuple[list[str], list[str]]:
    """Delete the ``config.agents`` rows named; returns ``(removed, refused)``.

    Each delete re-runs the candidate test on the ROW AS THE FILE HOLDS IT,
    inside the config lock: same ``kiro_agent`` and still the fresh-sync shape
    (:func:`_is_fresh_sync_shape`). A row that changed meanwhile -- the owner
    edited it, a member-aware write stamped it -- is newer evidence and is
    refused, not deleted. The test is on identity and shape, never on equality
    with a default-filled snapshot: a row written by a build whose record had
    fewer keys must still be recognised as the sync's. Nothing but the row
    moves: the spec under ``~/.kiro/agents`` and any transcript stay.
    """
    removed: list[str] = []
    refused: list[str] = []
    for name in names:
        kiro_agent = cfg.agents[name].kiro_agent
        deleted = False

        def _mutate(doc: dict, _name: str = name, _bound: str = kiro_agent) -> dict | None:
            nonlocal deleted
            agents = coerce_dict_section(doc, "agents")
            raw = agents.get(_name)
            if not isinstance(raw, dict) or raw.get("kiro_agent") != _bound:
                return None
            if not _is_fresh_sync_shape(raw, kiro_agent=_bound):
                return None
            del agents[_name]
            deleted = True
            return doc

        update_config_locked(mutate=_mutate)
        if deleted:
            removed.append(name)
            del cfg.agents[name]
        else:
            refused.append(name)
    return removed, refused


def prune_synced_crewmates(conversation_log) -> PruneReport:
    """Run the pass once. Thread-side; safe to call on every boot.

    ``conversation_log`` is the gateway's :class:`~kiro_crew.history.ConversationLog`;
    ``None`` means history is unavailable and the pass fails closed.
    """
    report = PruneReport()
    marker = marker_path()
    if marker.exists():
        report.skipped_marker = True
        return report
    cfg = KiroCrewConfig.load()
    raw_agents = _raw_agents_section()
    candidates = _synced_candidates(cfg, raw_agents)
    if candidates:
        if conversation_log is None:
            raise HistoryUnreadable("no conversation log; removal needs chat history")
        named = _agents_named_in_history(conversation_log)
        # Check and delete ONE candidate at a time: the strict history check
        # runs immediately before its own row's removal, never once for the
        # whole list up front. The gateway holds new DM threads back while the
        # pass runs (``DashboardState.crewmate_prune_settled``), so no thread
        # can be opened between a candidate's check and its delete.
        for name in candidates:
            if _chatted(cfg, name, named):
                report.kept.append(name)
            else:
                removed, refused = remove_never_chatted(cfg, [name])
                report.removed.extend(removed)
                report.refused.extend(refused)
    if report.refused:
        # A refused delete is not a commit: the row on disk was not the row
        # judged. Nothing is recorded as done; the next boot re-judges it.
        logger.warning(
            "crewmate prune: %d row(s) changed while being judged, pass not recorded: %s",
            len(report.refused),
            ", ".join(report.refused),
        )
        return report
    body = {
        "migrated_at": time.time(),
        "removed": report.removed,
        "kept": report.kept,
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(marker, json.dumps(body, indent=2) + "\n")
    return report
