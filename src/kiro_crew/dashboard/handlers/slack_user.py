"""Gateway door for the read-only "Slack as me" tools.

``kirocrew-core``'s ``slack_search``, ``slack_read`` and ``slack_list_conversations``
tools are thin forwarders; THIS module decides whether a call may read the
operator's Slack at all, and it is the only place the operator's user token is
opened. The Slack calls themselves are :mod:`kiro_crew.slack.user_read`.

**Why the decision lives here.** The MCP server runs inside the agent's sandbox,
where the vault is masked, and anything it decided about entitlement would be
decided inside the thing being entitled. The gateway holds the token, the
session registry and the governance ceiling, so it answers.

**Who may read.** The token reads every DM its owner can see, so the rule is about
where an answer LANDS rather than how much a session is trusted, and it mirrors
the caller classes :mod:`kiro_crew.dashboard.handlers.debug` refuses a host-wide
view (importing the same :mod:`kiro_crew.dashboard.session_control` constants):

* the owner's dashboard tabs and the attended CLI (``cli_chat``) are admitted;
* a subagent is judged by the session at the root of its dispatch chain, because
  its answer flows back into that conversation;
* an unattended session (a cron or workflow run) is refused: these tools are for
  a question someone just asked, never background collection;
* an app-owned session is refused: the conversation belongs to the app;
* a channel session, or a dashboard tab linked or mirrored to a channel, is
  refused -- whatever it reads is republished to that channel's audience, which
  is exactly how a private DM would leak -- EXCEPT the operator's own 1:1 DM with
  the bot, judged by :func:`~kiro_crew.dashboard.session_control.owner_dm_refusal`
  (the channel roster names the peer as sole owner and the mirror is the DM
  itself), whose only audience is the operator;
* anything the gateway cannot place is refused.

**Other gates, in order:** internal transport only (``request["internal_auth"]``;
the prefix is on ``_STRICT_INTERNAL_API_PATHS``, and a loopback request without
the secret falls through to cookie auth, so the handler re-checks), the
``capabilities.slack_user_read`` governance row (fail-closed), then a stored user
token. Every outcome is written to SEL with the method and conversation id --
never the query text or message content.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from aiohttp import web

logger = logging.getLogger(__name__)

#: The governance row consulted before any read.
GOVERNANCE_SCOPE: Final[str] = "capabilities.slack_user_read"

#: Operations this door serves, by URL leaf -> SEL tool name.
OPERATIONS: Final[dict[str, str]] = {
    "search": "slack_search",
    "read": "slack_read",
    "conversations": "slack_list_conversations",
}

#: How far up a subagent's parent chain the root lookup walks before refusing.
_MAX_DISPATCH_DEPTH: Final[int] = 16

_TOKEN_MISSING = (
    "no Slack user token is stored, so Slack cannot be read as you. Run "
    "`kirocrew setup --slack` (or add SLACK_USER_TOKEN under Settings → Secrets) "
    "with the User OAuth Token (xoxp-…) from your Slack app's OAuth & Permissions page"
)


def _sel() -> Any:
    """Late-binding sel() for test monkeypatch compatibility (see agents.py)."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


def _refuse(code: str, message: str, status: int, retry_after: float | None = None) -> web.Response:
    if retry_after is not None:
        return web.json_response(
            {"error": message, "code": code, "retry_after": retry_after}, status=status
        )
    return web.json_response({"error": message, "code": code}, status=status)


def _audit(
    session_key: str, tool_name: str, outcome: str, resources: str = "", error: str = ""
) -> None:
    try:
        _sel().log_tool_invocation(
            session_key=session_key or "unknown",
            source="mcp",
            tool_name=tool_name,
            outcome=outcome,
            downstream_service="slack",
            resources=resources,
            error=error[:200],
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("slack user read: SEL audit failed", exc_info=True)


# ── Caller classification ────────────────────────────────────────────────────


def _live_slot(state: Any, session_key: str) -> Any:
    slots = getattr(state, "_slots", None)
    lookup = getattr(slots, "get", None) if slots is not None else None
    if lookup is None or not session_key.startswith("dashboard:"):
        return None
    slot = session_key.partition(":")[2]
    return lookup(slot) if slot else None


def _subagent_record(state: Any, session_key: str) -> Any:
    agents = getattr(getattr(state, "subagents", None), "_agents", None)
    values = getattr(agents, "values", None) if agents is not None else None
    if values is None:
        return None
    bare = session_key.split(":", 1)[1] if session_key.startswith("subagent:") else session_key
    for record in list(values()):
        rid = str(getattr(record, "id", "") or "")
        conversation = str(getattr(record, "conversation_key", "") or "")
        if session_key in (conversation, f"subagent:{rid}") or (rid and bare == rid):
            return record
    return None


def _dispatch_root(state: Any, session_key: str) -> tuple[str, str]:
    """The session at the root of *session_key*'s dispatch chain, or a refusal.

    Returns ``(root_key, "")`` or ``("", reason)``. A subagent's answer returns
    to whoever spawned it, so its entitlement is its root's; an app-spawned
    subagent carries the app on its own record and is refused on that.
    """
    key = session_key
    seen: set[str] = set()
    for _ in range(_MAX_DISPATCH_DEPTH):
        if not (key.startswith("subagent:") or key.startswith("subagent_")):
            return key, ""
        if key in seen:
            break
        seen.add(key)
        record = _subagent_record(state, key)
        if record is None:
            return "", "the calling subagent has no record in this gateway"
        if str(getattr(record, "app", "") or ""):
            return "", "a subagent spawned by an app may not read the owner's Slack"
        parent = str(getattr(record, "parent_session_key", "") or "")
        if not parent:
            return "", "the calling subagent has no parent session to answer to"
        key = parent
    return "", "the calling subagent's dispatch chain could not be resolved"


def caller_refusal(state: Any, session_key: str) -> str:
    """``""`` when this caller may read the operator's Slack, else why not."""
    from kiro_crew.dashboard.session_control import (
        CRON_LINK_PREFIX,
        UNATTENDED_SLOT_PREFIXES,
        _has_channel_mirror,
        owner_dm_refusal,
        session_owner_dm_refusal,
    )
    from kiro_crew.dashboard.token_auth import derive_caller_app

    if not session_key:
        return "the request carried no session identity"
    root, reason = _dispatch_root(state, session_key)
    if reason:
        return reason
    if root.startswith(("cron:", "cron_", "taskrunner:", "taskrunner_")) or root.split(":", 1)[
        -1
    ].startswith(UNATTENDED_SLOT_PREFIXES):
        return (
            "an unattended session (a scheduled or workflow run) may not read Slack as "
            "you; these tools answer a question someone is asking now"
        )
    slots = getattr(state, "_slots", None)
    jobs = getattr(getattr(state, "crons", None), "_jobs", None)
    subagents = getattr(getattr(state, "subagents", None), "_agents", None)
    try:
        app = derive_caller_app(slots, root, jobs, subagents)
    except Exception:  # noqa: BLE001 - an unplaceable owner is a refusal
        return "the calling session's owner could not be determined"
    if app:
        return "an app-owned session may not read the owner's Slack"
    if root == "cli_chat" or root.startswith("cli_chat:"):
        return ""
    if not root.startswith("dashboard:"):
        # A channel conversation. The one channel audience that is the owner
        # alone is their own 1:1 DM with the bot, and session_control already
        # owns that predicate (roster, peer, and mirror == origin); anything
        # else would republish a private DM to other people.
        try:
            dm_refusal = session_owner_dm_refusal(state, root)
        except Exception:  # noqa: BLE001 - unknowable audience is a refusal
            dm_refusal = "the conversation's audience could not be established"
        if dm_refusal:
            return (
                "only a dashboard tab, the CLI, or your own 1:1 DM with the bot may "
                f"read Slack as you; this conversation reaches others ({dm_refusal})"
            )
        return ""
    slot = _live_slot(state, root)
    if slot is None:
        return "the calling session names no live dashboard tab"
    if getattr(slot, "_app", ""):
        return "an app-owned session may not read the owner's Slack"
    link = str(getattr(slot, "linked_session_key", "") or "")
    if link and not link.startswith(CRON_LINK_PREFIX):
        try:
            dm_refusal = owner_dm_refusal(state, slot)
        except Exception:  # noqa: BLE001 - unknowable audience is a refusal
            dm_refusal = "the conversation's audience could not be established"
        if dm_refusal:
            return (
                "a channel-linked tab may not read Slack as you; what it reads lands "
                f"in that channel's thread ({dm_refusal})"
            )
        return ""
    try:
        mirrored = _has_channel_mirror(state, slot)
    except Exception:  # noqa: BLE001 - an unknowable mirror is treated as one
        mirrored = True
    if mirrored:
        return (
            "a tab mirrored to a channel may not read Slack as you; what it reads is "
            "republished to that channel's audience"
        )
    return ""


# ── Shared request pipeline ──────────────────────────────────────────────────


def _governance_refusal(session_key: str) -> str:
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            GOVERNANCE_SCOPE, "", session_key=session_key, log_warning=False, fail_closed=True
        )
    except Exception:  # noqa: BLE001 - an ingestion gate fails closed
        return "reading Slack as you is blocked (governance unavailable)"
    if not getattr(decision, "permitted", False):
        return "reading Slack as you is disabled by governance policy"
    return ""


async def _admit(
    request: web.Request, operation: str
) -> tuple[str, dict[str, Any], Any, web.Response | None]:
    """Run every gate. Returns ``(session_key, body, reader, None)`` or a refusal."""
    from kiro_crew.slack.user_read import SlackUserReader, SlackUserReadError, resolve_user_token

    tool = OPERATIONS[operation]
    session_key = (request.headers.get("X-Session-Key") or "").strip()
    if not request.get("internal_auth"):
        _audit(session_key, tool, "denied", error="not internal transport")
        return (
            "",
            {},
            None,
            _refuse(
                "forbidden",
                "these reads are internal-transport only; the agent reaches them through "
                "the kirocrew-core Slack tools",
                403,
            ),
        )
    state = request.app.get("state")
    refusal = caller_refusal(state, session_key)
    if refusal:
        _audit(session_key, tool, "denied", error=refusal)
        return "", {}, None, _refuse("caller_not_permitted", refusal, 403)
    denied = _governance_refusal(session_key)
    if denied:
        _audit(session_key, tool, "denied", error="governance")
        return "", {}, None, _refuse("governance_denied", denied, 403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        _audit(session_key, tool, "error", error="invalid JSON")
        return "", {}, None, _refuse("invalid_json", "request body must be a JSON object", 400)
    token = await asyncio.to_thread(resolve_user_token)
    if not token:
        _audit(session_key, tool, "error", error="token missing")
        return "", {}, None, _refuse("slack_user_token_missing", _TOKEN_MISSING, 409)
    try:
        reader = SlackUserReader(token)
    except SlackUserReadError as exc:
        _audit(session_key, tool, "error", error=exc.code)
        return "", {}, None, _refuse(exc.code, exc.message, exc.status)
    return session_key, body, reader, None


def _error_response(exc: Any) -> web.Response:
    retry = round(float(exc.retry_after), 1) if exc.retry_after is not None else None
    return _refuse(exc.code, exc.message, exc.status, retry)


# ── Routes ───────────────────────────────────────────────────────────────────


async def api_slack_user_search(request: web.Request) -> web.Response:
    """POST /api/slack-user/search — ``search.messages`` as the operator."""
    from kiro_crew.slack.user_read import SlackUserReadError

    session_key, body, reader, refusal = await _admit(request, "search")
    if refusal is not None:
        return refusal
    try:
        answer = await reader.search(
            body.get("query", ""),
            count=body.get("count"),
            page=body.get("page"),
            sort=str(body.get("sort") or "score"),
        )
    except SlackUserReadError as exc:
        _audit(session_key, "slack_search", "error", error=exc.code)
        return _error_response(exc)
    _audit(
        session_key,
        "slack_search",
        "completed",
        resources=f"matches={len(answer['matches'])} query_len={len(answer['query'])}",
    )
    return web.json_response(answer)


async def api_slack_user_read(request: web.Request) -> web.Response:
    """POST /api/slack-user/read — one conversation's history or one thread."""
    from kiro_crew.slack.user_read import (
        SlackUserReadError,
        parse_conversation_ref,
        parse_cursor,
        parse_time_bound,
    )

    session_key, body, reader, refusal = await _admit(request, "read")
    if refusal is not None:
        return refusal
    try:
        ref = parse_conversation_ref(body.get("conversation"))
        thread_ts = body.get("thread_ts") or ""
        if not isinstance(thread_ts, str):
            raise SlackUserReadError("invalid_argument", "thread_ts must be a string", 400)
        answer = await reader.read(
            ref,
            thread_ts=thread_ts.strip(),
            limit=body.get("limit"),
            oldest=parse_time_bound(body.get("since"), "since"),
            latest=parse_time_bound(body.get("until"), "until"),
            cursor=parse_cursor(body.get("cursor")),
        )
    except SlackUserReadError as exc:
        _audit(session_key, "slack_read", "error", error=exc.code)
        return _error_response(exc)
    _audit(
        session_key,
        "slack_read",
        "completed",
        resources=(
            f"conversation={answer['conversation'].get('id', '')} "
            f"thread={'yes' if answer.get('thread_ts') else 'no'} "
            f"messages={len(answer['messages'])}"
        ),
    )
    return web.json_response(answer)


async def api_slack_user_conversations(request: web.Request) -> web.Response:
    """POST /api/slack-user/conversations — conversations the operator belongs to."""
    from kiro_crew.slack.user_read import SlackUserReadError, parse_cursor, parse_kinds

    session_key, body, reader, refusal = await _admit(request, "conversations")
    if refusal is not None:
        return refusal
    try:
        query = body.get("query") or ""
        if not isinstance(query, str):
            raise SlackUserReadError("invalid_argument", "query must be a string", 400)
        answer = await reader.list_conversations(
            query=query,
            types=parse_kinds(body.get("types")),
            limit=body.get("limit"),
            cursor=parse_cursor(body.get("cursor")),
        )
    except SlackUserReadError as exc:
        _audit(session_key, "slack_list_conversations", "error", error=exc.code)
        return _error_response(exc)
    _audit(
        session_key,
        "slack_list_conversations",
        "completed",
        resources=f"conversations={len(answer['conversations'])}",
    )
    return web.json_response(answer)


def register(app: web.Application) -> None:
    """Register the three routes. All live under ``/api/slack-user``, the single
    prefix ``server._STRICT_INTERNAL_API_PATHS`` carries for them."""
    app.router.add_post("/api/slack-user/search", api_slack_user_search)
    app.router.add_post("/api/slack-user/read", api_slack_user_read)
    app.router.add_post("/api/slack-user/conversations", api_slack_user_conversations)
