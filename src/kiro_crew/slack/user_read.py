"""Read Slack as the operator: a thin, read-only wrapper over the Web API.

The Slack gateway speaks as a bot (``xoxb``), and a bot sees only the
conversations it was invited to. This module is the other half: it reads with
the operator's own USER token (``xoxp``), so the agent can find context the
operator already has -- their DMs, group DMs, channels they belong to, and
workspace search -- without a bot being added to any conversation.

What it guarantees, and where each guarantee lives:

* **Read-only by construction.** Every request goes through
  :meth:`SlackUserReader._call`, which refuses any method outside
  :data:`READ_METHODS`. There is no write verb to misuse, whatever arguments
  the model supplies.
* **On demand only.** Nothing here caches, indexes, polls or persists. Each
  call is one bounded question and one bounded answer; the user-name lookups a
  single answer needs are memoized for that call alone.
* **Slack only.** The client is pinned to :data:`SLACK_API_BASE`, and a
  permalink is accepted only when its host is under ``slack.com``.
* **Bounded output.** Page sizes, per-message text and the whole answer are
  capped, and every cut is reported (``truncated``) rather than silent.
* **Redacted text.** Message text leaves through ``redact_credentials`` and
  ``redact_exfiltration_urls``. It is still UNTRUSTED content -- anyone in the
  workspace can write it -- which is why the MCP tools fence it before the
  model sees it.

The token is never read here: callers pass it in, per call, from the vault
(:func:`resolve_user_token`). It never reaches config, logs, or an agent
process.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)

#: The only host this module talks to.
SLACK_API_BASE = "https://slack.com/api/"

#: The complete set of Web API methods a request may name. Every one of them is
#: a read; adding a method here is the whole review surface for "can this module
#: change anything in Slack", so a write verb must never appear.
READ_METHODS: frozenset[str] = frozenset(
    {
        "auth.test",
        "conversations.history",
        "conversations.info",
        "conversations.replies",
        "search.messages",
        "users.conversations",
        "users.info",
    }
)

#: Accepted user-token prefixes: a classic user token, and the rotating form
#: Slack issues when token rotation is enabled. A bot token is refused: it sees a
#: narrower workspace than the operator, and ``search.messages`` rejects it.
USER_TOKEN_PREFIXES: tuple[str, ...] = ("xoxp-", "xoxe.xoxp-")

# ── Bounds ────────────────────────────────────────────────────────────────────

SEARCH_COUNT_DEFAULT = 20
SEARCH_COUNT_MAX = 50
SEARCH_PAGE_MAX = 100
READ_LIMIT_DEFAULT = 50
READ_LIMIT_MAX = 200
LIST_LIMIT_DEFAULT = 100
LIST_LIMIT_MAX = 200
QUERY_MAX_LEN = 500
#: Characters of one message's text kept before it is cut.
MESSAGE_TEXT_MAX = 3000
#: Characters of message text one answer carries before later messages are dropped.
ANSWER_TEXT_BUDGET = 40_000
#: Distinct ``users.info`` lookups one answer may spend naming authors/mentions.
USER_LOOKUPS_MAX = 40
#: ``users.conversations`` pages a name lookup scans before giving up.
NAME_SCAN_PAGES_MAX = 10
NAME_SCAN_PAGE_SIZE = 200
#: Longest ``Retry-After`` honoured inline; a longer one is reported instead.
RETRY_AFTER_INLINE_MAX_SECS = 10.0
REQUEST_TIMEOUT_SECS = 20

CONVERSATION_KINDS: dict[str, str] = {
    "channel": "public_channel",
    "private_channel": "private_channel",
    "group_dm": "mpim",
    "dm": "im",
}

_CHANNEL_ID_RE = re.compile(r"^[CDG][A-Z0-9]{2,19}$")
_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,19}$")
_TS_RE = re.compile(r"^[0-9]{9,11}\.[0-9]{1,6}$")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9=_\-%:+/]{1,512}$")
_CHANNEL_NAME_RE = re.compile(r"^#?([a-z0-9][a-z0-9._\-]{0,79})$")
_PERMALINK_RE = re.compile(
    r"^https://(?:[a-z0-9-]+\.)*slack\.com/archives/([CDG][A-Z0-9]{2,19})"
    r"/p([0-9]{10})([0-9]{6})(?:[?#](.*))?$"
)
_THREAD_TS_QUERY_RE = re.compile(r"(?:^|&)thread_ts=([0-9]{9,11}\.[0-9]{1,6})(?:&|$)")

#: Message subtypes that carry no conversation content.
_NOISE_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "group_join",
        "group_leave",
        "channel_purpose",
        "channel_topic",
        "reminder_add",
    }
)

# Slack mrkdwn entities: <@U123>, <@U123|name>, <#C123|name>, <!here>,
# <!subteam^S123|@team>, <https://x|label>, <mailto:a@b|a@b>.
_ENTITY_RE = re.compile(r"<([^<>\s][^<>]*)>")


class SlackUserReadError(Exception):
    """A request that could not be answered, in the gateway's error vocabulary.

    ``code`` is the stable identifier the MCP tool and tests branch on;
    ``message`` is the actionable sentence the agent relays; ``status`` is the
    HTTP status the gateway answers with; ``retry_after`` is set only for a rate
    limit that was too long to wait out inline.
    """

    def __init__(
        self, code: str, message: str, status: int = 502, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after


def is_user_token(token: str) -> bool:
    """Whether *token* has the shape of a Slack user token (never a bot token)."""
    return isinstance(token, str) and token.startswith(USER_TOKEN_PREFIXES) and len(token) > 12


def resolve_user_token() -> str:
    """The operator's Slack user token from the encrypted vault, or ``""``.

    Vault only, deliberately without an ``.env`` fallback: a value in ``.env``
    is propagated into the gateway's process environment, and from there toward
    children the sandbox has to remember to strip. The vault keeps the token on
    the gateway's side of that boundary. Read per call so a rotated or removed
    token takes effect on the next request without a restart. Blocking file IO:
    call through ``asyncio.to_thread`` from the event loop.
    """
    from kiro_crew.config.loader import CRED_SLACK_USER_TOKEN
    from kiro_crew.config.paths import config_dir
    from kiro_crew.secrets.vault import SecretVault

    try:
        secret = SecretVault(config_dir()).get(CRED_SLACK_USER_TOKEN)
    except Exception:
        logger.debug("slack user token: vault read failed", exc_info=True)
        return ""
    return secret.reveal() if secret is not None else ""


def _as_dict(value: Any) -> dict[str, Any]:
    """*value* when it is a JSON object, else an empty one (Slack/gateway payloads)."""
    return value if isinstance(value, dict) else {}


# ── Argument parsing ─────────────────────────────────────────────────────────


def clamp_int(value: Any, default: int, low: int, high: int) -> int:
    """An int in ``[low, high]``; *default* when *value* is absent or not an int."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(high, value))


def parse_time_bound(value: Any, field: str) -> str:
    """A Slack ``oldest``/``latest`` bound from a Slack ts or an ISO-8601 time.

    ``""`` when *value* is empty. A naive ISO time is read as UTC, which is what
    the answer's own timestamps are rendered in, so a bound copied from one
    answer into the next request means the same instant.
    """
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > 64:
        raise SlackUserReadError("invalid_argument", f"{field} must be a string", 400)
    text = value.strip()
    if _TS_RE.match(text):
        return text
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise SlackUserReadError(
            "invalid_argument",
            f"{field} must be an ISO-8601 date/time (e.g. 2026-09-01 or "
            "2026-09-01T14:00Z) or a Slack message timestamp",
            400,
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return f"{parsed.timestamp():.6f}"


def parse_cursor(value: Any) -> str:
    """A pagination cursor copied from a previous answer, or ``""``."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not _CURSOR_RE.match(value):
        raise SlackUserReadError(
            "invalid_argument", "cursor must be the next_cursor value from a previous answer", 400
        )
    return value


@dataclass(frozen=True)
class ConversationRef:
    """What a ``conversation`` argument named, before any Slack call."""

    channel_id: str = ""
    user_id: str = ""
    name: str = ""
    ts: str = ""


def parse_conversation_ref(value: Any) -> ConversationRef:
    """Classify a ``conversation`` argument without contacting Slack.

    Accepted forms: a message permalink (``https://<ws>.slack.com/archives/C…/p…``,
    optionally with ``thread_ts``), a conversation id (``C…``/``G…``/``D…``), a
    user id (``U…``/``W…``, meaning the DM with that person), or a channel name
    with or without ``#``. A permalink resolves to the thread it belongs to.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise SlackUserReadError(
            "invalid_argument",
            "conversation is required: a channel name (#general), a conversation id "
            "(C…/G…/D…), a user id (U…) for a DM, or a message permalink",
            400,
        )
    text = value.strip()
    link = _PERMALINK_RE.match(text)
    if link:
        channel, secs, micros, query = link.groups()
        ts = f"{secs}.{micros}"
        thread = _THREAD_TS_QUERY_RE.search(query or "")
        return ConversationRef(channel_id=channel, ts=thread.group(1) if thread else ts)
    if text.lower().startswith(("http://", "https://")):
        raise SlackUserReadError(
            "invalid_argument",
            "only Slack message permalinks (https://<workspace>.slack.com/archives/…) "
            "are accepted as a conversation link",
            400,
        )
    if _CHANNEL_ID_RE.match(text):
        return ConversationRef(channel_id=text)
    if _USER_ID_RE.match(text):
        return ConversationRef(user_id=text)
    name = _CHANNEL_NAME_RE.match(text.lower())
    if name:
        return ConversationRef(name=name.group(1))
    raise SlackUserReadError(
        "invalid_argument",
        "conversation must be a channel name, a conversation id, a user id or a "
        "Slack message permalink; to find a person's messages, use slack_search "
        "with from:@name or in:@name",
        400,
    )


def parse_kinds(value: Any) -> list[str]:
    """Slack ``types`` values for the requested conversation kinds (all by default)."""
    if value in (None, "", []):
        return list(CONVERSATION_KINDS.values())
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SlackUserReadError("invalid_argument", "types must be a list of strings", 400)
    unknown = sorted({v for v in value if v not in CONVERSATION_KINDS})
    if unknown:
        raise SlackUserReadError(
            "invalid_argument",
            f"unknown conversation type(s) {unknown}; use {sorted(CONVERSATION_KINDS)}",
            400,
        )
    return [CONVERSATION_KINDS[v] for v in dict.fromkeys(value)]


# ── Rendering ────────────────────────────────────────────────────────────────


def _redact(text: str) -> str:
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def format_ts(ts: str) -> str:
    """A Slack ts as ``YYYY-MM-DD HH:MMZ`` (UTC), or ``""`` when unparseable."""
    try:
        moment = _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%MZ")


def _mention_ids(text: str) -> set[str]:
    ids: set[str] = set()
    for match in _ENTITY_RE.finditer(text or ""):
        body = match.group(1)
        if body.startswith("@"):
            uid = body[1:].split("|", 1)[0]
            if _USER_ID_RE.match(uid):
                ids.add(uid)
    return ids


def render_mrkdwn(text: str, names: dict[str, str]) -> str:
    """Slack mrkdwn as plain readable text: mentions, channels, links, entities."""

    def _entity(match: re.Match[str]) -> str:
        body = match.group(1)
        target, _, label = body.partition("|")
        if target.startswith("@"):
            uid = target[1:]
            return "@" + (label or names.get(uid) or uid)
        if target.startswith("#"):
            return "#" + (label or target[1:])
        if target.startswith("!"):
            if target.startswith("!subteam^"):
                return label or "@group"
            if target.startswith("!date^"):
                return label or target
            return "@" + target[1:]
        if label and label != target:
            return f"{label} ({target})"
        return target

    rendered = _ENTITY_RE.sub(_entity, text or "")
    return rendered.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


class _Budget:
    """The answer-wide text budget, so one oversized thread cannot flood context."""

    def __init__(self, total: int = ANSWER_TEXT_BUDGET) -> None:
        self.remaining = total
        self.truncated = False

    def take(self, text: str) -> str | None:
        """*text* cut to the per-message cap, or ``None`` once the budget is spent."""
        if self.remaining <= 0:
            self.truncated = True
            return None
        if len(text) > MESSAGE_TEXT_MAX:
            dropped = len(text) - MESSAGE_TEXT_MAX
            text = text[:MESSAGE_TEXT_MAX] + f" [... {dropped} more characters]"
            self.truncated = True
        self.remaining -= len(text)
        return text


# ── The reader ───────────────────────────────────────────────────────────────


def _default_client(token: str) -> Any:
    from slack_sdk.web.async_client import AsyncWebClient

    # Pinned base URL and no environment proxy lookup: this client carries the
    # operator's user token, so where it connects is not configurable.
    return AsyncWebClient(token=token, base_url=SLACK_API_BASE, timeout=REQUEST_TIMEOUT_SECS)


class SlackUserReader:
    """One request's worth of read-only Slack access with the operator's token.

    Construct per request and discard: the only state it holds is the author
    names resolved while building this one answer.
    """

    def __init__(
        self,
        token: str,
        *,
        client: Any | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if not is_user_token(token):
            raise SlackUserReadError(
                "slack_user_token_invalid",
                "the stored SLACK_USER_TOKEN is not a Slack user token (it must start "
                "with xoxp-); store the User OAuth Token from the Slack app's "
                "OAuth & Permissions page",
                409,
            )
        self._client = client if client is not None else _default_client(token)
        self._sleep = sleep
        self._names: dict[str, str] = {}
        self._lookups = 0

    # ── transport ──

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One Web API read, with a single inline wait on a short rate limit."""
        if method not in READ_METHODS:
            # Unreachable from the public methods; the guard is what makes
            # "read-only" a property of this class rather than of its callers.
            raise SlackUserReadError("method_not_allowed", f"{method} is not a read method", 500)
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        for attempt in (1, 2):
            try:
                resp = await self._client.api_call(method, http_verb="GET", params=clean)
            except Exception as exc:  # noqa: BLE001 - classified below
                error = _classify_error(exc)
                if error.code == "rate_limited" and attempt == 1:
                    wait = error.retry_after if error.retry_after is not None else 1.0
                    if wait <= RETRY_AFTER_INLINE_MAX_SECS:
                        logger.info(
                            "slack user read: %s rate limited, retrying in %.1fs", method, wait
                        )
                        await self._sleep(wait)
                        continue
                raise error from None
            data = getattr(resp, "data", resp)
            return data if isinstance(data, dict) else {}
        raise SlackUserReadError("rate_limited", "Slack rate limit", 429)  # pragma: no cover

    # ── names ──

    async def _resolve_names(self, user_ids: Iterable[str]) -> None:
        for uid in sorted(set(user_ids)):
            if uid in self._names or not _USER_ID_RE.match(uid):
                continue
            if self._lookups >= USER_LOOKUPS_MAX:
                return
            self._lookups += 1
            try:
                data = await self._call("users.info", {"user": uid})
            except SlackUserReadError as exc:
                if exc.code in ("rate_limited", "slack_user_token_rejected"):
                    raise
                self._names[uid] = uid
                continue
            user = data.get("user") or {}
            profile = user.get("profile") or {}
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or uid
            )
            self._names[uid] = _redact(str(name))[:80]

    def _author(self, msg: dict[str, Any]) -> str:
        uid = str(msg.get("user") or "")
        if uid:
            return self._names.get(uid, uid)
        bot = msg.get("bot_profile") or {}
        return _redact(
            str(msg.get("username") or bot.get("name") or msg.get("bot_id") or "unknown")
        )[:80]

    # ── message shaping ──

    def _message(self, msg: dict[str, Any], budget: _Budget) -> dict[str, Any] | None:
        text = render_mrkdwn(str(msg.get("text") or ""), self._names)
        files = [f for f in msg.get("files") or [] if isinstance(f, dict)]
        if files:
            listed = ", ".join(str(f.get("name") or f.get("title") or "file") for f in files[:10])
            text = f"{text}\n[files: {listed}]" if text else f"[files: {listed}]"
        kept = budget.take(_redact(text))
        if kept is None:
            return None
        ts = str(msg.get("ts") or "")
        out: dict[str, Any] = {
            "ts": ts,
            "time": format_ts(ts),
            "author": self._author(msg),
            "text": kept,
        }
        if msg.get("user"):
            out["author_id"] = str(msg["user"])
        thread_ts = str(msg.get("thread_ts") or "")
        if thread_ts and thread_ts != ts:
            out["thread_ts"] = thread_ts
        replies = msg.get("reply_count")
        if isinstance(replies, int) and replies > 0:
            out["reply_count"] = replies
            out["thread_ts"] = ts
        if msg.get("edited"):
            out["edited"] = True
        return out

    async def _messages(self, raw: list[Any], budget: _Budget) -> list[dict[str, Any]]:
        msgs = [
            m
            for m in raw
            if isinstance(m, dict) and str(m.get("subtype") or "") not in _NOISE_SUBTYPES
        ]
        ids: set[str] = set()
        for m in msgs:
            if m.get("user"):
                ids.add(str(m["user"]))
            ids |= _mention_ids(str(m.get("text") or ""))
        await self._resolve_names(ids)
        shaped: list[dict[str, Any]] = []
        for m in msgs:
            item = self._message(m, budget)
            if item is None:
                break
            shaped.append(item)
        return shaped

    # ── conversations ──

    async def _describe(self, conv: dict[str, Any]) -> dict[str, Any]:
        cid = str(conv.get("id") or "")
        if conv.get("is_im"):
            other = str(conv.get("user") or "")
            await self._resolve_names([other])
            return {"id": cid, "kind": "dm", "name": "@" + self._names.get(other, other or cid)}
        if conv.get("is_mpim"):
            kind = "group_dm"
        elif conv.get("is_private") or conv.get("is_group"):
            kind = "private_channel"
        else:
            kind = "channel"
        name = _redact(str(conv.get("name") or cid))[:120]
        out: dict[str, Any] = {
            "id": cid,
            "kind": kind,
            "name": name if kind == "group_dm" else f"#{name}",
        }
        topic = _as_dict(conv.get("topic")).get("value")
        if topic:
            out["topic"] = _redact(render_mrkdwn(str(topic), self._names))[:300]
        return out

    async def _conversation_info(self, channel_id: str) -> dict[str, Any]:
        data = await self._call("conversations.info", {"channel": channel_id})
        conv = data.get("channel")
        return await self._describe(conv if isinstance(conv, dict) else {"id": channel_id})

    async def _scan_memberships(
        self, types: list[str], match: Callable[[dict[str, Any]], bool]
    ) -> dict[str, Any] | None:
        cursor = ""
        for _page in range(NAME_SCAN_PAGES_MAX):
            data = await self._call(
                "users.conversations",
                {
                    "types": ",".join(types),
                    "exclude_archived": "true",
                    "limit": NAME_SCAN_PAGE_SIZE,
                    "cursor": cursor,
                },
            )
            for conv in data.get("channels") or []:
                if isinstance(conv, dict) and match(conv):
                    return conv
            cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                return None
        return None

    async def resolve_conversation(self, ref: ConversationRef) -> dict[str, Any]:
        """The conversation a :class:`ConversationRef` names, described."""
        if ref.channel_id:
            return await self._conversation_info(ref.channel_id)
        if ref.user_id:
            conv = await self._scan_memberships(["im"], lambda c: c.get("user") == ref.user_id)
            if conv is None:
                raise SlackUserReadError(
                    "conversation_not_found",
                    f"you have no open DM with {ref.user_id}; slack_search with in:<@{ref.user_id}> "
                    "finds messages exchanged elsewhere",
                    404,
                )
            return await self._describe(conv)
        wanted = ref.name
        conv = await self._scan_memberships(
            ["public_channel", "private_channel", "mpim"],
            lambda c: str(c.get("name") or "").lower() == wanted,
        )
        if conv is None:
            raise SlackUserReadError(
                "conversation_not_found",
                f"no channel named #{wanted} among the conversations you belong to; "
                "slack_list_conversations lists them",
                404,
            )
        return await self._describe(conv)

    # ── public operations ──

    async def whoami(self) -> dict[str, str]:
        """``auth.test``: which user and workspace the token belongs to."""
        data = await self._call("auth.test", {})
        return {
            "user_id": str(data.get("user_id") or ""),
            "user": str(data.get("user") or ""),
            "team": str(data.get("team") or ""),
        }

    async def search(
        self, query: str, *, count: int = SEARCH_COUNT_DEFAULT, page: int = 1, sort: str = "score"
    ) -> dict[str, Any]:
        """``search.messages`` over everything the operator can see."""
        if not isinstance(query, str) or not query.strip():
            raise SlackUserReadError("invalid_argument", "query is required", 400)
        if len(query) > QUERY_MAX_LEN:
            raise SlackUserReadError(
                "invalid_argument", f"query is longer than {QUERY_MAX_LEN} characters", 400
            )
        sort = sort if sort in ("score", "timestamp") else "score"
        data = await self._call(
            "search.messages",
            {
                "query": query.strip(),
                "count": clamp_int(count, SEARCH_COUNT_DEFAULT, 1, SEARCH_COUNT_MAX),
                "page": clamp_int(page, 1, 1, SEARCH_PAGE_MAX),
                "sort": sort,
                "sort_dir": "desc",
                "highlight": "false",
            },
        )
        block = data.get("messages") or {}
        matches = [m for m in block.get("matches") or [] if isinstance(m, dict)]
        budget = _Budget()
        ids: set[str] = set()
        for m in matches:
            if m.get("user"):
                ids.add(str(m["user"]))
            ids |= _mention_ids(str(m.get("text") or ""))
            channel = _as_dict(m.get("channel"))
            if channel.get("is_im") and _USER_ID_RE.match(str(channel.get("name") or "")):
                # A DM's search-result "name" is the other person's user id.
                ids.add(str(channel["name"]))
        await self._resolve_names(ids)
        results: list[dict[str, Any]] = []
        for m in matches:
            item = self._message(m, budget)
            if item is None:
                break
            channel = _as_dict(m.get("channel"))
            cid = str(channel.get("id") or "")
            if channel.get("is_im"):
                other = str(channel.get("name") or "")
                label = f"DM with @{self._names[other]}" if other in self._names else "DM"
            elif channel.get("is_mpim"):
                label = "group DM"
            else:
                label = "#" + _redact(str(channel.get("name") or cid))[:120]
            item["conversation"] = {"id": cid, "name": label}
            permalink = str(m.get("permalink") or "")
            if _PERMALINK_RE.match(permalink.split("?", 1)[0]):
                item["permalink"] = permalink
            results.append(item)
        paging = block.get("paging") or {}
        return {
            "query": query.strip(),
            "total": int(block.get("total") or paging.get("total") or len(results)),
            "page": int(paging.get("page") or 1),
            "pages": int(paging.get("pages") or 1),
            "matches": results,
            "truncated": budget.truncated,
        }

    async def read(
        self,
        ref: ConversationRef,
        *,
        thread_ts: str = "",
        limit: int = READ_LIMIT_DEFAULT,
        oldest: str = "",
        latest: str = "",
        cursor: str = "",
    ) -> dict[str, Any]:
        """A conversation's recent messages, or one thread when a ts is known.

        History comes back from Slack newest-first; it is returned oldest-first
        so the answer reads like the conversation, and ``next_cursor`` continues
        toward OLDER messages. A thread is returned oldest-first as Slack sends
        it, starting with its parent message.
        """
        if thread_ts and not _TS_RE.match(thread_ts):
            raise SlackUserReadError(
                "invalid_argument", "thread_ts must be a Slack message timestamp", 400
            )
        conv = await self.resolve_conversation(ref)
        root = thread_ts or ref.ts
        size = clamp_int(limit, READ_LIMIT_DEFAULT, 1, READ_LIMIT_MAX)
        common = {
            "channel": conv["id"],
            "limit": size,
            "cursor": cursor,
            "oldest": oldest,
            "latest": latest,
        }
        if root:
            data = await self._call("conversations.replies", {**common, "ts": root})
            raw = list(data.get("messages") or [])
        else:
            data = await self._call("conversations.history", common)
            raw = list(reversed(data.get("messages") or []))
        budget = _Budget()
        messages = await self._messages(raw, budget)
        answer: dict[str, Any] = {
            "conversation": conv,
            "messages": messages,
            "truncated": budget.truncated,
        }
        if root:
            answer["thread_ts"] = root
        next_cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
        if next_cursor and data.get("has_more", True):
            answer["next_cursor"] = next_cursor
        return answer

    async def list_conversations(
        self,
        *,
        query: str = "",
        types: list[str] | None = None,
        limit: int = LIST_LIMIT_DEFAULT,
        cursor: str = "",
    ) -> dict[str, Any]:
        """Conversations the operator belongs to, optionally filtered by name."""
        needle = (query or "").strip().lower().lstrip("#")
        if len(needle) > 80:
            raise SlackUserReadError("invalid_argument", "query is longer than 80 characters", 400)
        size = clamp_int(limit, LIST_LIMIT_DEFAULT, 1, LIST_LIMIT_MAX)
        kinds = types or list(CONVERSATION_KINDS.values())
        found: list[dict[str, Any]] = []
        pages = NAME_SCAN_PAGES_MAX if needle else 1
        next_cursor = cursor
        for _page in range(pages):
            data = await self._call(
                "users.conversations",
                {
                    "types": ",".join(kinds),
                    "exclude_archived": "true",
                    "limit": NAME_SCAN_PAGE_SIZE if needle else size,
                    "cursor": next_cursor,
                },
            )
            raw = [c for c in data.get("channels") or [] if isinstance(c, dict)]
            await self._resolve_names(str(c.get("user") or "") for c in raw if c.get("is_im"))
            for conv in raw:
                described = await self._describe(conv)
                if needle and needle not in described["name"].lower().lstrip("#@"):
                    continue
                found.append(described)
            next_cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
            if not next_cursor or len(found) >= size:
                break
        answer: dict[str, Any] = {"conversations": found[:size], "truncated": len(found) > size}
        if next_cursor:
            answer["next_cursor"] = next_cursor
        return answer


# ── Error classification ─────────────────────────────────────────────────────

_TOKEN_REJECTED = frozenset(
    {
        "not_authed",
        "invalid_auth",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "not_allowed_token_type",
    }
)
_NOT_FOUND = frozenset(
    {"channel_not_found", "not_in_channel", "thread_not_found", "message_not_found"}
)


def _retry_after(response: Any) -> float | None:
    headers = getattr(response, "headers", None) or {}
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except AttributeError:
        return None
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _classify_error(exc: BaseException) -> SlackUserReadError:
    """Map a slack_sdk / transport exception onto :class:`SlackUserReadError`."""
    if isinstance(exc, SlackUserReadError):
        return exc
    from slack_sdk.errors import SlackApiError

    if isinstance(exc, SlackApiError):
        response = exc.response
        status = getattr(response, "status_code", None)
        try:
            code = str(response.get("error", "") or "")
            needed = str(response.get("needed", "") or "")
        except Exception:  # noqa: BLE001 - a malformed response is just unknown
            code, needed = "", ""
        if status == 429 or code == "ratelimited":
            wait = _retry_after(response)
            hint = f" retry in {int(wait)}s" if wait is not None else " retry shortly"
            return SlackUserReadError("rate_limited", f"Slack rate limit reached;{hint}", 429, wait)
        if code in _TOKEN_REJECTED:
            return SlackUserReadError(
                "slack_user_token_rejected",
                f"Slack rejected the stored user token ({code}); store a fresh xoxp- token "
                "with `kirocrew setup --slack` or under Settings → Secrets",
                409,
            )
        if code == "missing_scope":
            scope = f" ({_redact(needed)[:120]})" if needed else ""
            return SlackUserReadError(
                "missing_scope",
                f"the user token lacks a required scope{scope}; add it under User Token "
                "Scopes, reinstall the Slack app, and store the new xoxp- token",
                409,
            )
        if code in _NOT_FOUND:
            return SlackUserReadError(
                "conversation_not_found",
                f"Slack does not show you that conversation or message ({code}); you "
                "may not be a member of it",
                404,
            )
        return SlackUserReadError(
            "slack_api_error", f"Slack API error ({_redact(code)[:60] or 'unknown'})", 502
        )
    return SlackUserReadError(
        "slack_unreachable", f"could not reach Slack ({type(exc).__name__})", 502
    )
