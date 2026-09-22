"""HTTP routes for the per-session work ledger.

Thin mapping over :mod:`kiro_crew.session_ledger`. The security contract is
the Issue Radar crew-route one: the ledger a request touches is derived from
the CALLING SESSION's identity (``X-Session-Key``, vetted by
``_recognize_session``), never from the request body — so a session can only
ever read or write its own ledger, and raw HTTP with no recognized session
identity is refused. Restricted (incognito/temporary/guest) sessions are
refused too: a ledger is durable on-disk state, which is exactly what those
modes promise not to leave behind.

Both routes are MCP-only (no browser caller) and listed in
``server._STRICT_INTERNAL_API_PATHS`` — without that entry the internal-secret
call falls through to cookie auth and every tool call fails with 403.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew import session_ledger
from kiro_crew.dashboard.handlers._shared import _is_restricted_session

# Module-scope like memory.py's identical imports: the recognition gate and
# incognito classifier are this module's own load-bearing dependencies.
from kiro_crew.dashboard.handlers.cron import _recognize_session
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import is_incognito_transcript
from kiro_crew.sel import sel
from kiro_crew.validation import (
    SESSION_LEDGER_RECORD_SCHEMA,
    ValidationError,
    validate_tool_args,
)

logger = logging.getLogger(__name__)


async def _resolve_ledger_key(
    request: web.Request, operation: str
) -> tuple[str, None] | tuple[None, web.Response]:
    """Vet the calling session and fold its key to the ledger spelling.

    Returns ``(key, None)`` on success or ``(None, refusal_response)``. The
    fold is :func:`session_ledger.ledger_key` — a LOSSLESS dashboard-prefix
    strip, so the spelling here matches what the nudge composer derives from a
    loop's slot key, while distinct channel session keys can never collide.
    """
    state: DashboardState = request.app["state"]
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state, sk, operation, blocks_persisted_mode=is_incognito_transcript
    )
    if refusal is not None:
        return None, refusal
    if _is_restricted_session(state, request):
        sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="restricted_session_block",
            error="Ledger writes are not allowed in this session mode.",
        )
        return None, web.json_response(
            {
                "error": "The work ledger is not available in this session mode.",
                "code": "restricted_session",
            },
            status=403,
        )
    return session_ledger.ledger_key(sk), None


async def api_session_ledger_get(request: web.Request) -> web.Response:
    """GET /api/session-ledger — the calling session's state record + event tail.

    One fold: the event tail is part of the record the ledger's crew log entries
    fold to, so state and events always come from the same read of the same
    entries and can never be a torn pairing.
    """
    key, refusal = await _resolve_ledger_key(request, "session_ledger_read")
    if refusal is not None:
        return refusal
    assert key is not None
    state: DashboardState = request.app["state"]
    # The calling session's unit, for the same two reasons the record path needs it:
    # it resolves the caller's key to the slot identity the unit headers record, and
    # it pins the unit being written last. A read without it is not wrong, only less
    # informed -- a session with no live unit still reads its slot by key.
    state_record = await asyncio.to_thread(
        session_ledger.read_state,
        key,
        _session_unit(state, request.headers.get("X-Session-Key", "")),
    )
    events = state_record.get("events", [])[-session_ledger._MAX_EVENT_TAIL :]
    return web.json_response({"state": state_record, "events": events})


async def api_session_ledger_record(request: web.Request) -> web.Response:
    """POST /api/session-ledger/record — one update, one appended crew log entry."""
    key, refusal = await _resolve_ledger_key(request, "session_ledger_record")
    if refusal is not None:
        return refusal
    assert key is not None
    state: DashboardState = request.app["state"]
    sk = request.headers.get("X-Session-Key", "")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_body"},
            status=400,
        )
    known = {f.name for f in SESSION_LEDGER_RECORD_SCHEMA.fields}
    try:
        cleaned = validate_tool_args(
            {k: v for k, v in body.items() if k in known},
            SESSION_LEDGER_RECORD_SCHEMA,
        )
    except ValidationError as exc:
        return web.json_response({"error": str(exc), "code": "validation_error"}, status=400)
    artifacts = cleaned.get("artifacts")
    if artifacts is not None and not all(
        isinstance(k, str) and isinstance(v, str) for k, v in artifacts.items()
    ):
        return web.json_response(
            {
                "error": "artifacts must map strings to strings",
                "code": "artifacts_not_string_map",
            },
            status=400,
        )
    try:
        # record() appends through the crew log's writer and folds the result
        # (bounded file reads); off-loop so a slow filesystem cannot freeze
        # chat/WS/heartbeat.
        state_record, durable = await asyncio.to_thread(
            session_ledger.record_update,
            key,
            session_id=_session_unit(state, sk),
            goal=cleaned.get("goal"),
            phase=cleaned.get("phase"),
            next_step=cleaned.get("next"),
            tried_approach=cleaned.get("tried_approach"),
            tried_rejected_because=cleaned.get("tried_rejected_because"),
            artifacts=artifacts,
            event=cleaned.get("event"),
            event_kind=cleaned.get("event_kind"),
        )
    except session_ledger.LedgerUnavailable as exc:
        # The request is well formed; what blocks it is the state of the record's
        # home. 409, the same answer the crew log's own reads give for a log this
        # build cannot serve.
        return web.json_response({"error": str(exc), "code": "crew_log_unavailable"}, status=409)
    except session_ledger.LedgerEntryTooLarge as exc:
        # BEFORE the ValueError branch below, which this subclasses: the caller's remedy
        # differs. A discipline refusal is fixed by supplying a field; this one is fixed
        # by sending less, and 413 is the answer that says so. Non-success is the point
        # -- the append can never land, so the record this reported would not exist.
        return web.json_response({"error": str(exc), "code": "ledger_entry_too_large"}, status=413)
    except ValueError as exc:
        # The phase-requires-event discipline (and key validation) surface here.
        return web.json_response({"error": str(exc), "code": "ledger_discipline"}, status=400)
    except OSError:
        logger.warning("session ledger write failed for %s", key, exc_info=True)
        return web.json_response(
            {"error": "ledger write failed; try again", "code": "ledger_write_failed"},
            status=503,
        )
    # ``durable`` comes back FROM the write, so it describes this append rather than
    # the newest one on the slot. False means the append had not reached disk by the
    # time this answered: the writer was still busy, or it refused an append while
    # this one was in flight. The update is recorded either way and the next one
    # supersedes it, but a 200 that implied otherwise would overstate the write.
    return web.json_response({"ok": True, "durable": durable, "state": state_record})


def _session_unit(state: DashboardState, session_key: str) -> str:
    """The crew log unit *session_key* is serving on now, or ``""``.

    A ledger entry is appended to the session's OWN crew log, so the write needs
    the ACP session id the slot is running under at this moment. The resolver is an
    exact registry read plus an attribute read -- no disk, no mutation of session
    state as a side effect of describing it.

    ``""`` when the slot has no live ACP session: it has never run a turn, or its
    session was torn down and not yet re-created. The ledger refuses on that rather
    than guessing a unit, because an update filed under the wrong session is worse
    than one the caller is told did not land.
    """
    from kiro_crew.crew_log.resolve import unit_for_session_key

    try:
        return unit_for_session_key(state.sessions, session_key)
    except Exception:
        logger.debug("session ledger: resolving the calling session's unit failed", exc_info=True)
        return ""


def _creator_owned_slot(
    state: DashboardState, caller_key: str, target_session_key: str
) -> tuple[object | None, web.Response | None]:
    """The slot for *target_session_key*, if *caller_key* created it.

    The same ownership rule ``session_read_message`` enforces, reached the same
    way ``handlers/work_ledger.py`` reaches it: ``session_create`` stamps
    ``_created_by`` with the calling session's key inside the synchronous window
    after the mint, and it is the only entry point that does -- a person's own tab
    stays unattributed, so an unattributed slot is owned by nobody and is refused.

    Three spellings are tried because one dashboard session is legitimately
    spelled both ``dashboard_chat-X`` and ``chat-X``; :func:`session_ledger.ledger_key`
    is the lossless fold between them.

    Workspace is checked too. It is the memory boundary ``authorize_target``
    already refuses across, and a ledger carries a workstream's goal and artifacts,
    which is exactly the kind of state that boundary exists to keep apart.

    Returns ``(slot, None)`` or ``(None, refusal)``.
    """
    folded = session_ledger.ledger_key(target_session_key)
    slot = None
    for candidate in (target_session_key, folded, f"dashboard_{folded}"):
        try:
            slot = state.get_slot(candidate)
        except Exception:  # pragma: no cover - a slot-table read must not 500
            logger.debug("child ledger: slot lookup failed for %s", candidate, exc_info=True)
            slot = None
        if slot is not None:
            break
    if slot is None:
        # 404 rather than 403: the caller is allowed to ask, and there is nothing
        # there. The ownership refusal below is what protects a slot that DOES
        # exist, so the pair is not an existence oracle for sessions the caller
        # could not read anyway -- it created them or it gets nothing.
        return None, web.json_response(
            {
                "error": (
                    "That session is not open, so its ledger cannot be read. A "
                    "conductor reads the ledgers of sessions it created."
                ),
                "code": "unknown_child_session",
            },
            status=404,
        )
    creator = session_ledger.ledger_key(str(getattr(slot, "_created_by", "") or ""))
    if not creator or creator != caller_key:
        sel().log_api_access(
            caller=caller_key,
            operation="session_ledger_read_child",
            outcome="denied",
            source="mcp",
            resources=f"target={folded}:child_not_owned",
            error="That session was not created by this one.",
        )
        return None, web.json_response(
            {
                "error": (
                    "That session was not created by this one, so its ledger is "
                    "not readable from here. A session reads its own ledger and "
                    "the ledgers of the sessions it created."
                ),
                "code": "child_not_owned",
            },
            status=403,
        )
    caller_slot = None
    for candidate in (caller_key, f"dashboard_{caller_key}"):
        try:
            caller_slot = state.get_slot(candidate)
        except Exception:  # pragma: no cover
            caller_slot = None
        if caller_slot is not None:
            break
    if caller_slot is not None:
        mine = str(getattr(caller_slot, "workspace", "default") or "default")
        theirs = str(getattr(slot, "workspace", "default") or "default")
        if mine != theirs:
            sel().log_api_access(
                caller=caller_key,
                operation="session_ledger_read_child",
                outcome="denied",
                source="mcp",
                resources=f"target={folded}:child_cross_workspace",
                error="That session is in a different workspace.",
            )
            return None, web.json_response(
                {
                    "error": (
                        "That session is in a different workspace. Workspace is "
                        "the memory boundary, and a ledger is not readable across it."
                    ),
                    "code": "child_cross_workspace",
                },
                status=403,
            )
    return slot, None


async def api_session_ledger_child_get(request: web.Request) -> web.Response:
    """GET /api/session-ledger/child?target=<session_key> — a CHILD's ledger.

    The read a conductor needs to act on a wake, and the only way it can get one:
    a ledger lives under a fenced path, so a shell listing there shows the
    sandbox placeholder and proves nothing. Permission is creator-only, identical
    to ``session_read_message`` -- the caller's identity comes from the vetted
    session header and the target from the query, so a caller can only ever widen
    its reach to sessions it created itself.

    Same response shape as the self-read, deliberately: a conductor that can
    already read its own ledger needs no second shape to learn, and the board and
    the wake snapshot are then one projection rather than two.
    """
    caller_key, refusal = await _resolve_ledger_key(request, "session_ledger_read_child")
    if refusal is not None:
        return refusal
    assert caller_key is not None
    target = (request.query.get("target") or "").strip()
    if not target:
        return web.json_response(
            {"error": "target is required", "code": "missing_target"}, status=400
        )
    state: DashboardState = request.app["state"]
    slot, denied = _creator_owned_slot(state, caller_key, target)
    if denied is not None:
        return denied
    assert slot is not None
    folded = session_ledger.ledger_key(target)
    # The CHILD's own unit, not the caller's: the fold joins the units that child
    # recorded under, and passing the caller's would read the wrong slot's log.
    state_record = await asyncio.to_thread(
        session_ledger.read_state,
        folded,
        _session_unit(state, target),
    )
    events = state_record.get("events", [])[-session_ledger._MAX_EVENT_TAIL :]
    return web.json_response({"state": state_record, "events": events, "child": folded})
