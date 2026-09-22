"""Wakes a conductor when a session it created records something actionable.

RECONSTRUCTED REFERENCE IMPLEMENTATION. This is the withdrawn session-ledger
channel, kept reachable for the open question "conductors outside
kirocrew-conductor hold no work-ledger grant". It is NOT on any shipping branch
and nothing imports it from one. See the commit message for what is faithful and
what is not.

A conductor that dispatches worker sessions has no way to learn that a worker
finished, got stuck, or needs a decision except by looking, and looking costs a
model turn whether or not anything changed. The worker already writes its own
ledger; this module turns that write into a push.

The split here is deliberate: every decision is a pure function of values the
caller already holds, and nothing in this module starts a turn, resolves a slot
or reads dashboard state. The gateway owns delivery, so the rule can be tested
without a gateway and a delivery bug can never be mistaken for a rule bug.

Two files per conductor live beside its ledger store, under the control root
that store purges cannot reach (see :func:`session_ledger.control_dir`): the
delivered map, which makes replay idempotent, and the per-child wake clock,
which bounds how many turns one child can cost.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from kiro_crew import session_ledger

logger = logging.getLogger(__name__)

#: Phases whose ARRIVAL needs the conductor. ``done`` and ``abandoned`` are the
#: ledger's own terminal pair; ``blocked`` and ``awaiting-ruling`` are in-flight
#: but need someone else to act, which is the same thing from the conductor's
#: side. A phase is free-form in the ledger, so these are recognized rather than
#: enforced -- an unlisted phase is a board update and nothing more.
WAKE_PHASES = frozenset({"done", "abandoned", "blocked", "awaiting-ruling"})

#: Event kinds that need the conductor whatever the phase says. ``blocked`` is
#: an external dependency; ``ruling`` is a request for the conductor's own
#: decision. They differ by WHO must act, which is why both are listed and why
#: ``decision`` is not: that one records a decision the worker already made.
WAKE_EVENT_KINDS = frozenset({"blocked", "ruling"})

#: Wakes one child may cause per hour. Past it the record still lands and the
#: board still updates; the wake is folded into the next delivered one, so the
#: conductor learns the newest state rather than a queue of stale ones. A
#: runaway worker cannot spend the conductor's whole budget.
MAX_WAKES_PER_CHILD_PER_HOUR = 12

#: The window the limit above is measured over.
_RATE_WINDOW_SECS = 3600.0

#: Children one conductor tracks in each control file. A conductor dispatches
#: one session per work item, so this is generous; past it the least recently
#: woken entry is dropped, which costs a duplicate wake after a restart rather
#: than a lost one.
_MAX_TRACKED_CHILDREN = 256

#: Control-file names, siblings of the fold-governing files already there.
_DELIVERED_FILE = "wake-delivered"
_RATE_FILE = "wake-rate"

#: Refuse to parse a control file past this size. Both are bounded maps of short
#: strings to numbers, so the legitimate maximum is far under it and anything
#: larger is damage rather than a record worth trusting.
_MAX_CONTROL_BYTES = 256_000


def enabled(config: Any) -> bool:
    """Whether a wake may be DELIVERED, read from *config*'s ``ledger_wake``.

    Default on. Off is not a pause on the whole mechanism: the record still
    lands, the delivered map still advances and the board still reads the child's
    state -- only the turn is not injected. That is what makes the switch safe to
    flip on a live gateway, because nothing accumulates behind it.
    """
    section = getattr(config, "ledger_wake", None)
    if section is None:
        return True
    return bool(getattr(section, "enabled", True))


def is_actionable(*, previous_phase: str, new_phase: str, event_kind: str) -> bool:
    """Whether one ledger record needs the conductor's action.

    *previous_phase* is what the record folded to BEFORE this update, so a
    conductor is woken by a phase MOVING into a waking phase, not by a worker
    re-recording progress while already in one. A worker in ``blocked`` that
    reports twice costs one wake, which is the difference between a push and a
    poll.

    An event kind is judged on its own, without reference to the phase, because
    a worker can need a ruling without changing phase at all -- that is the
    ordinary shape of "still implementing, but which of these two?".
    """
    kind = (event_kind or "").strip()
    if kind in WAKE_EVENT_KINDS:
        return True
    phase = (new_phase or "").strip()
    if phase not in WAKE_PHASES:
        return False
    return phase != (previous_phase or "").strip()


def fingerprint(child_key: str, event_index: int) -> str:
    """Identity of one wake: the child and how many events its ledger holds.

    The pair is what makes replay safe. An event index never goes backward for a
    live ledger, so a fingerprint already in the delivered map describes a wake
    the conductor has seen, whatever path re-derived it -- the write hook, or the
    replay sweep after a gateway start.
    """
    return f"{child_key}#{int(event_index)}"


def envelope(*, child_key: str, phase: str, event_index: int, snapshot: str) -> str:
    """The wake turn's text: one header line, then the child's ledger snapshot.

    The header is fixed-shape so a conductor (and a test) can read it without
    parsing prose, and it names the fingerprint so a conductor that sees the same
    wake twice can say so. The snapshot is the child's own rendered state, marked
    as reference data -- it is a report about a peer session, never an
    instruction to this one.
    """
    head = (
        f"[ledger wake] child={child_key} phase={phase or '(unset)'} "
        f"event={int(event_index)} fingerprint={fingerprint(child_key, event_index)}"
    )
    return f"{head}\n\n{snapshot}"


def _read_map(conductor_key: str, name: str) -> dict[str, Any]:
    """One control file as a dict, or empty when there is nothing to trust.

    Best-effort by contract. Every caller is on a path that must not fail because
    a maintenance file is missing or damaged: a missing map means no wake has been
    delivered yet, and a damaged one is discarded rather than half-read, which
    costs at most a duplicate wake. Never creates the directory -- a read that
    answers "nothing recorded" needs no directory, and a reader that creates one
    would leave residue for every slot anything ever asked about.
    """
    try:
        path = session_ledger.control_dir(conductor_key) / name
        if not path.is_file():
            return {}
        if path.stat().st_size > _MAX_CONTROL_BYTES:
            logger.warning("ledger wake: %s is larger than its own bound; ignoring it", name)
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("ledger wake: reading %s failed", name, exc_info=True)
        return {}
    return value if isinstance(value, dict) else {}


def _write_map(conductor_key: str, name: str, value: dict[str, Any]) -> bool:
    """Replace one control file atomically; whether it landed.

    Atomic because both maps are read by a later process to decide whether a wake
    was already delivered, and a half-written map reads as "delivered nothing" --
    which replays every wake the conductor had already seen. The temporary file is
    a sibling so the rename stays on one filesystem.

    Returns False rather than raising: the caller is on the write path of a
    worker's own ledger update, and a maintenance file it could not persist must
    not fail the worker's record. The cost of a lost write is a duplicate wake.
    """
    try:
        directory = session_ledger.control_dir(conductor_key)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        tmp = directory / f"{name}.tmp"
        tmp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError, TypeError):
        logger.debug("ledger wake: writing %s failed", name, exc_info=True)
        return False
    return True


def _as_number(value: Any) -> float:
    """*value* as a float, or ``-1`` when it is not one.

    A control file is on disk and its contents are not this module's to promise,
    so a non-numeric entry is treated as older than every real one instead of
    raising inside a comparison.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def _trim(value: dict[str, Any], keep: int) -> dict[str, Any]:
    """*value* reduced to its *keep* largest entries by stored number.

    Both maps are keyed by child and valued by a number that only grows (an event
    index, a wake timestamp), so the largest values are the most recently active
    children -- the ones whose state a duplicate wake would be most wrong about.
    """
    if len(value) <= keep:
        return value
    ordered = sorted(value.items(), key=lambda item: _as_number(item[1]), reverse=True)
    return dict(ordered[:keep])


def delivered_index(conductor_key: str, child_key: str) -> int:
    """The newest event index *conductor_key* has been woken for on *child_key*.

    ``-1`` when it has never been woken for that child, which is what makes a
    first wake and a replayed one distinguishable: every real event index is at
    least zero.
    """
    return int(_as_number(_read_map(conductor_key, _DELIVERED_FILE).get(child_key, -1)))


def already_delivered(conductor_key: str, child_key: str, event_index: int) -> bool:
    """Whether this exact wake has already been delivered.

    The comparison is ``<=`` rather than ``==`` on purpose. A restart replay walks
    a child's whole ledger, so an index BELOW the delivered mark is an older event
    the conductor has necessarily already seen -- dropping it is the same
    correctness claim as dropping the duplicate itself.
    """
    return int(event_index) <= delivered_index(conductor_key, child_key)


def mark_delivered(conductor_key: str, child_key: str, event_index: int) -> bool:
    """Record that *conductor_key* was woken for *child_key* up to *event_index*.

    Never moves the mark BACKWARD. Two wakes for one child can be decided
    concurrently -- a write hook and a replay sweep -- and the older one landing
    last would re-open every wake between them.
    """
    current = _read_map(conductor_key, _DELIVERED_FILE)
    if int(event_index) <= int(_as_number(current.get(child_key, -1))):
        return True
    current[child_key] = int(event_index)
    return _write_map(conductor_key, _DELIVERED_FILE, _trim(current, _MAX_TRACKED_CHILDREN))


def within_rate_limit(conductor_key: str, child_key: str, *, now: float | None = None) -> bool:
    """Whether a wake for *child_key* may be delivered now.

    A count and the start of the window it counts, per child. When the window has
    passed the count restarts, so the limit is a sliding budget rather than a
    permanent ceiling -- a worker that was noisy for an hour is not muted for the
    rest of the run.

    Read-only: it answers the question without spending the budget, so a caller
    that is refused for some other reason has not used a wake. :func:`note_wake`
    is what spends it.
    """
    moment = time.time() if now is None else now
    entry = _read_map(conductor_key, _RATE_FILE).get(child_key)
    if not isinstance(entry, dict):
        return True
    started = _as_number(entry.get("window_start"))
    if moment - started >= _RATE_WINDOW_SECS:
        return True
    return int(_as_number(entry.get("count"))) < MAX_WAKES_PER_CHILD_PER_HOUR


def note_wake(conductor_key: str, child_key: str, *, now: float | None = None) -> bool:
    """Spend one wake from *child_key*'s budget.

    Called only for a wake that was actually delivered. A wake the rate limit
    suppressed costs nothing, which is what makes the excess COALESCE rather than
    queue: the child's next delivered wake carries its newest state, and the
    states in between were superseded anyway.
    """
    moment = time.time() if now is None else now
    current = _read_map(conductor_key, _RATE_FILE)
    entry = current.get(child_key)
    if not isinstance(entry, dict) or moment - _as_number(entry.get("window_start")) >= (
        _RATE_WINDOW_SECS
    ):
        entry = {"window_start": moment, "count": 0}
    entry["count"] = int(_as_number(entry.get("count"))) + 1
    current[child_key] = entry
    trimmed = _trim({key: value for key, value in current.items()}, _MAX_TRACKED_CHILDREN)
    return _write_map(conductor_key, _RATE_FILE, trimmed)


def stall_is_due(*, next_due_ts: float, last_written_ts: float, now: float) -> bool:
    """Whether a child has gone silent past the deadline its conductor set.

    Silence is the one signal a write hook cannot carry, so it is the only thing
    in this design that needs a clock. The test is deliberately narrow: the
    deadline has passed AND the child's ledger has not been written since the
    deadline was set. A child that recorded anything at all after that point has
    proved it is alive, and its record already went through the wake rule -- so a
    stall wake on top would report silence that did not happen.

    The deadline is cleared by the caller once this fires, and only the conductor
    re-arms it. That is what bounds this to one wake per silent worker instead of
    one per tick.
    """
    if next_due_ts <= 0:
        return False
    if now < next_due_ts:
        return False
    return last_written_ts < next_due_ts


#: Prefix under which a conductor stores one child's liveness deadline in its OWN
#: ledger artifacts. Artifacts are a merge-only string map, so a deadline is
#: CLEARED by writing :data:`_DEADLINE_CLEARED` rather than by deleting the key --
#: there is no delete, and a key that vanished would be indistinguishable from one
#: that was never set.
DEADLINE_ARTIFACT_PREFIX = "next_due_ts:"

#: The value that means "no deadline". Zero rather than an empty string so the
#: field stays numeric wherever it is read.
_DEADLINE_CLEARED = "0"


def deadline_artifact_key(child_key: str) -> str:
    """The artifact key a conductor stores *child_key*'s deadline under."""
    return f"{DEADLINE_ARTIFACT_PREFIX}{child_key}"


def cleared_deadline_artifacts(child_key: str) -> dict[str, str]:
    """The artifact patch that disarms *child_key*'s deadline.

    Returned as a patch rather than applied here, so the caller writes it through
    the ordinary ledger record path and the disarm is one more entry in the
    conductor's own log instead of a side-channel edit of its state.
    """
    return {deadline_artifact_key(child_key): _DEADLINE_CLEARED}


def deadlines_from_artifacts(artifacts: dict[str, Any] | None) -> dict[str, float]:
    """Every armed liveness deadline in a conductor's *artifacts*.

    Cleared and unparseable entries are left out rather than reported as zero: a
    caller asks this to decide what to arm, and an entry it cannot read is not an
    entry it should fire on. A conductor that typed a deadline wrong gets no stall
    wake, which is the safe direction -- the alternative is a wake at an epoch it
    never meant.
    """
    found: dict[str, float] = {}
    for key, value in (artifacts or {}).items():
        if not isinstance(key, str) or not key.startswith(DEADLINE_ARTIFACT_PREFIX):
            continue
        child = key[len(DEADLINE_ARTIFACT_PREFIX) :]
        if not child:
            continue
        when = _as_number(value)
        if when > 0:
            found[child] = when
    return found


def earliest_deadline(deadlines: dict[str, float]) -> float | None:
    """The soonest armed deadline, or ``None`` when none is armed.

    This is the whole of what the scheduler needs: its next fire time becomes the
    minimum of its own next job and this. Returning ``None`` rather than infinity
    keeps "nothing to arm" a distinct answer, so a caller cannot accidentally pull
    the scheduler's wake time forward to a sentinel.
    """
    return min(deadlines.values()) if deadlines else None


def due_children(
    deadlines: dict[str, float],
    last_written: dict[str, float],
    *,
    now: float,
) -> list[str]:
    """Children whose deadline has passed with no ledger write since it was set.

    *last_written* is each child's newest ledger write time; a child missing from
    it has never written, which is the strongest possible case for a stall wake
    rather than a reason to skip it -- so it is read as zero.

    Sorted by deadline so the longest-overdue child is reported first, which is
    the order a conductor reading one coalesced turn should see them in.
    """
    due = [
        child
        for child, when in deadlines.items()
        if stall_is_due(
            next_due_ts=when,
            last_written_ts=_as_number(last_written.get(child, 0.0)),
            now=now,
        )
    ]
    return sorted(due, key=lambda child: deadlines[child])


def stall_envelope(*, child_key: str, phase: str, due_ts: float, snapshot: str) -> str:
    """The turn text for a SILENCE wake, marked as such.

    Deliberately a different header from :func:`envelope`. A record wake says
    "this child reported something"; this one says "this child reported nothing by
    the time you asked to be told", and a conductor that could not tell them apart
    would read a stall as a report and look for an event that does not exist.

    It carries no event index, because no event caused it -- which is also why it
    needs no fingerprint: the deadline is cleared when this fires, so the arming is
    what makes it at-most-once rather than a delivered map.
    """
    head = (
        f"[ledger wake] child={child_key} phase={phase or '(unset)'} "
        f"reason=stall due={int(due_ts)}"
    )
    return f"{head}\n\n{snapshot}"
