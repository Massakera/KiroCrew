"""The read-only Slack user-token client (``kiro_crew.slack.user_read``).

Driven through a fake ``api_call`` so no test reaches Slack: the fake records
every method and params it was asked for, which is also how the read-only
guarantee is asserted.
"""

from __future__ import annotations

from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from kiro_crew.slack import user_read as ur
from kiro_crew.slack.user_read import (
    ConversationRef,
    SlackUserReader,
    SlackUserReadError,
    parse_conversation_ref,
    parse_cursor,
    parse_kinds,
    parse_time_bound,
    render_mrkdwn,
)

TOKEN = "xoxp-1111-2222-3333-abcdef"


class _Resp(dict):
    """Enough of a SlackResponse for ``SlackApiError`` consumers."""

    def __init__(self, data: dict[str, Any], status: int = 200, headers: dict | None = None):
        super().__init__(data)
        self.status_code = status
        self.headers = headers or {}


def _api_error(code: str, status: int = 200, headers: dict | None = None, **extra) -> SlackApiError:
    return SlackApiError(code, _Resp({"ok": False, "error": code, **extra}, status, headers))


class FakeSlack:
    """A scripted ``AsyncWebClient.api_call``: method -> list of answers."""

    def __init__(self, script: dict[str, list[Any]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, str, dict]] = []

    async def api_call(self, method: str, *, http_verb: str = "POST", params: dict | None = None):
        self.calls.append((method, http_verb, dict(params or {})))
        answers = self.script.get(method)
        if not answers:
            raise AssertionError(f"unexpected Slack call {method}")
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return _Resp(answer)


def _reader(script: dict[str, list[Any]], names: dict[str, str] | None = None, sleeps=None):
    fake = FakeSlack(script)
    names = names or {}
    original = fake.api_call

    async def api_call(method, *, http_verb="POST", params=None):
        if method == "users.info" and method not in fake.script:
            fake.calls.append((method, http_verb, dict(params or {})))
            uid = (params or {}).get("user", "")
            if uid not in names:
                raise _api_error("user_not_found")
            return _Resp({"ok": True, "user": {"id": uid, "profile": {"display_name": names[uid]}}})
        return await original(method, http_verb=http_verb, params=params)

    fake.api_call = api_call  # type: ignore[method-assign]

    async def _sleep(secs: float) -> None:
        if sleeps is not None:
            sleeps.append(secs)

    return SlackUserReader(TOKEN, client=fake, sleep=_sleep), fake


# ── read-only and token shape ────────────────────────────────────────────────


def test_every_allowed_method_is_a_read():
    """The allowlist IS the write-surface review: nothing that posts, edits,
    reacts, joins, opens, marks, deletes or uploads may appear in it."""
    write_words = (
        "post",
        "update",
        "delete",
        "add",
        "remove",
        "join",
        "leave",
        "open",
        "mark",
        "invite",
        "kick",
        "archive",
        "create",
        "rename",
        "set",
        "upload",
    )
    for method in ur.READ_METHODS:
        verb = method.split(".", 1)[1].lower()
        assert not any(verb.startswith(w) for w in write_words), method


@pytest.mark.asyncio
async def test_a_method_outside_the_allowlist_is_refused_before_any_request():
    reader, fake = _reader({})
    with pytest.raises(SlackUserReadError) as err:
        await reader._call("chat.postMessage", {"channel": "C1", "text": "hi"})
    assert err.value.code == "method_not_allowed"
    assert fake.calls == []


@pytest.mark.parametrize("token", ["xoxb-1-2-3-bot", "xapp-1-A-1-x", "", "xoxp-"])
def test_non_user_tokens_are_refused(token):
    with pytest.raises(SlackUserReadError) as err:
        SlackUserReader(token, client=FakeSlack({}))
    assert err.value.code == "slack_user_token_invalid"


def test_rotating_user_token_is_accepted():
    assert ur.is_user_token("xoxe.xoxp-1-abcdefghijkl")


def test_default_client_is_pinned_to_slack():
    client = ur._default_client(TOKEN)
    assert client.base_url == "https://slack.com/api/"


# ── argument parsing ─────────────────────────────────────────────────────────


def test_permalink_resolves_to_its_thread():
    ref = parse_conversation_ref(
        "https://acme.slack.com/archives/C0123ABCD/p1712793600123456"
        "?thread_ts=1712790000.000100&cid=C0123ABCD"
    )
    assert ref == ConversationRef(channel_id="C0123ABCD", ts="1712790000.000100")
    plain = parse_conversation_ref("https://acme.slack.com/archives/C0123ABCD/p1712793600123456")
    assert plain.ts == "1712793600.123456"


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/archives/C0123ABCD/p1712793600123456",
        "https://slack.com.evil.example/archives/C0123ABCD/p1712793600123456",
        "http://acme.slack.com/archives/C0123ABCD/p1712793600123456",
    ],
)
def test_links_off_slack_are_refused(value):
    with pytest.raises(SlackUserReadError) as err:
        parse_conversation_ref(value)
    assert err.value.status == 400


def test_conversation_forms():
    assert parse_conversation_ref("#General") == ConversationRef(name="general")
    assert parse_conversation_ref("eng-team") == ConversationRef(name="eng-team")
    assert parse_conversation_ref("C0123ABCD") == ConversationRef(channel_id="C0123ABCD")
    assert parse_conversation_ref("D0123ABCD") == ConversationRef(channel_id="D0123ABCD")
    assert parse_conversation_ref("U0123ABCD") == ConversationRef(user_id="U0123ABCD")
    with pytest.raises(SlackUserReadError):
        parse_conversation_ref("")
    with pytest.raises(SlackUserReadError):
        parse_conversation_ref("@Some Person")


def test_time_bounds():
    assert parse_time_bound("", "since") == ""
    assert parse_time_bound("1712793600.123456", "since") == "1712793600.123456"
    assert parse_time_bound("2026-09-01", "since") == "1788220800.000000"
    assert parse_time_bound("2026-09-01T03:00:00-03:00", "since") == "1788242400.000000"
    with pytest.raises(SlackUserReadError):
        parse_time_bound("yesterday", "since")


def test_cursor_and_kinds():
    assert parse_cursor("dXNlcjpVMDYxTkZUVDI=") == "dXNlcjpVMDYxTkZUVDI="
    with pytest.raises(SlackUserReadError):
        parse_cursor("bad cursor with spaces")
    assert parse_kinds(None) == ["public_channel", "private_channel", "mpim", "im"]
    assert parse_kinds(["dm", "channel", "dm"]) == ["im", "public_channel"]
    with pytest.raises(SlackUserReadError):
        parse_kinds(["everything"])


def test_mrkdwn_rendering():
    text = "hi <@U1AB|bob> and <@U2CD>, see <https://x.example|site> in <#C12|gen> &amp; <!here>"
    assert render_mrkdwn(text, {"U2CD": "carol"}) == (
        "hi @bob and @carol, see site (https://x.example) in #gen & @here"
    )


# ── search ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_names_authors_and_dm_peers_and_keeps_permalinks():
    reader, fake = _reader(
        {
            "search.messages": [
                {
                    "ok": True,
                    "messages": {
                        "total": 2,
                        "paging": {"page": 1, "pages": 1},
                        "matches": [
                            {
                                "ts": "1712793600.000100",
                                "user": "U0ALICE1",
                                "text": "ship it on <!date^1712793600^{date}|Friday>",
                                "channel": {"id": "C0GEN001", "name": "general"},
                                "permalink": "https://acme.slack.com/archives/C0GEN001/p1712793600000100",
                            },
                            {
                                "ts": "1712793700.000200",
                                "user": "U0BOB001",
                                "text": "agreed with <@U0ALICE1>",
                                "channel": {"id": "D0DM0001", "name": "U0BOB001", "is_im": True},
                                "permalink": "https://evil.example/p1",
                            },
                        ],
                    },
                }
            ]
        },
        names={"U0ALICE1": "alice", "U0BOB001": "bob"},
    )
    answer = await reader.search("from:@bob launch", count=500, sort="timestamp")
    method, verb, params = fake.calls[0]
    assert (method, verb) == ("search.messages", "GET")
    assert params["count"] == ur.SEARCH_COUNT_MAX  # clamped
    assert params["sort"] == "timestamp"
    first, second = answer["matches"]
    assert first["author"] == "alice" and first["conversation"]["name"] == "#general"
    assert first["text"] == "ship it on Friday"
    assert first["permalink"].startswith("https://acme.slack.com/")
    assert second["conversation"]["name"] == "DM with @bob"
    assert second["text"] == "agreed with @alice"
    # A permalink that is not a Slack archive link is dropped, not relayed.
    assert "permalink" not in second
    assert answer["total"] == 2 and answer["truncated"] is False


@pytest.mark.asyncio
async def test_search_requires_a_query():
    reader, _ = _reader({})
    with pytest.raises(SlackUserReadError) as err:
        await reader.search("   ")
    assert err.value.status == 400


@pytest.mark.asyncio
async def test_message_text_is_redacted():
    # Assembled at runtime so the source holds no token-shaped literal for
    # secret scanners to flag; the redactor still sees the full shape.
    leaked = "-".join(["xoxb", "123456789012", "123456789012", "abcdefghijklmnopqrstuvwx"])
    reader, _ = _reader(
        {
            "search.messages": [
                {
                    "ok": True,
                    "messages": {
                        "matches": [
                            {
                                "ts": "1.1",
                                "user": "U0ALICE1",
                                "text": f"token is {leaked}",
                                "channel": {"id": "C0GEN001", "name": "general"},
                            }
                        ]
                    },
                }
            ]
        },
        names={"U0ALICE1": "alice"},
    )
    answer = await reader.search("token")
    assert leaked not in answer["matches"][0]["text"]


# ── read ─────────────────────────────────────────────────────────────────────


_INFO = {"ok": True, "channel": {"id": "C0GEN001", "name": "general", "topic": {"value": "news"}}}


@pytest.mark.asyncio
async def test_history_is_returned_oldest_first_with_a_cursor():
    reader, fake = _reader(
        {
            "conversations.info": [_INFO],
            "conversations.history": [
                {
                    "ok": True,
                    "has_more": True,
                    "response_metadata": {"next_cursor": "bmV4dA=="},
                    "messages": [
                        {"ts": "3.0", "user": "U0BOB001", "text": "newest", "reply_count": 2},
                        {
                            "ts": "2.0",
                            "subtype": "channel_join",
                            "user": "U0BOB001",
                            "text": "joined",
                        },
                        {"ts": "1.0", "user": "U0ALICE1", "text": "oldest"},
                    ],
                }
            ],
        },
        names={"U0ALICE1": "alice", "U0BOB001": "bob"},
    )
    answer = await reader.read(ConversationRef(channel_id="C0GEN001"), limit=10)
    assert [m["text"] for m in answer["messages"]] == ["oldest", "newest"]
    assert answer["messages"][1]["reply_count"] == 2
    assert answer["messages"][1]["thread_ts"] == "3.0"
    assert answer["next_cursor"] == "bmV4dA=="
    assert answer["conversation"] == {
        "id": "C0GEN001",
        "kind": "channel",
        "name": "#general",
        "topic": "news",
    }
    assert all(verb == "GET" for _, verb, _ in fake.calls)


@pytest.mark.asyncio
async def test_permalink_reads_the_thread_with_replies():
    reader, fake = _reader(
        {
            "conversations.info": [_INFO],
            "conversations.replies": [
                {
                    "ok": True,
                    "messages": [{"ts": "1712790000.000100", "user": "U0ALICE1", "text": "root"}],
                }
            ],
        },
        names={"U0ALICE1": "alice"},
    )
    ref = parse_conversation_ref(
        "https://acme.slack.com/archives/C0GEN001/p1712793600123456?thread_ts=1712790000.000100"
    )
    answer = await reader.read(ref)
    replies = [c for c in fake.calls if c[0] == "conversations.replies"][0][2]
    assert replies["ts"] == "1712790000.000100" and replies["channel"] == "C0GEN001"
    assert answer["thread_ts"] == "1712790000.000100"
    assert answer["messages"][0]["text"] == "root"


@pytest.mark.asyncio
async def test_channel_name_resolves_through_memberships_only():
    reader, fake = _reader(
        {
            "users.conversations": [
                {
                    "ok": True,
                    "channels": [{"id": "C0OTHER1", "name": "random"}],
                    "response_metadata": {"next_cursor": "cGFnZTI="},
                },
                {"ok": True, "channels": [{"id": "C0ENG001", "name": "Eng", "is_private": True}]},
            ],
            "conversations.history": [{"ok": True, "messages": []}],
        }
    )
    answer = await reader.read(ConversationRef(name="eng"))
    assert answer["conversation"] == {"id": "C0ENG001", "kind": "private_channel", "name": "#Eng"}
    history = [c for c in fake.calls if c[0] == "conversations.history"][0][2]
    assert history["channel"] == "C0ENG001"


@pytest.mark.asyncio
async def test_unknown_channel_name_is_a_404():
    reader, _ = _reader({"users.conversations": [{"ok": True, "channels": []}]})
    with pytest.raises(SlackUserReadError) as err:
        await reader.read(ConversationRef(name="nope"))
    assert err.value.status == 404 and err.value.code == "conversation_not_found"


@pytest.mark.asyncio
async def test_user_id_reads_the_dm_with_that_person():
    reader, _ = _reader(
        {
            "users.conversations": [
                {"ok": True, "channels": [{"id": "D0DM0001", "is_im": True, "user": "U0BOB001"}]}
            ],
            "conversations.history": [
                {"ok": True, "messages": [{"ts": "1.0", "user": "U0BOB001", "text": "hey"}]}
            ],
        },
        names={"U0BOB001": "bob"},
    )
    answer = await reader.read(ConversationRef(user_id="U0BOB001"))
    assert answer["conversation"] == {"id": "D0DM0001", "kind": "dm", "name": "@bob"}


@pytest.mark.asyncio
async def test_long_messages_and_long_answers_are_cut_and_reported():
    big = "x" * (ur.MESSAGE_TEXT_MAX + 50)
    messages = [{"ts": f"{i}.0", "user": "U0ALICE1", "text": big} for i in range(40, 0, -1)]
    reader, _ = _reader(
        {
            "conversations.info": [_INFO],
            "conversations.history": [{"ok": True, "messages": messages}],
        },
        names={"U0ALICE1": "alice"},
    )
    answer = await reader.read(ConversationRef(channel_id="C0GEN001"), limit=200)
    assert answer["truncated"] is True
    assert all(len(m["text"]) <= ur.MESSAGE_TEXT_MAX + 40 for m in answer["messages"])
    total = sum(len(m["text"]) for m in answer["messages"])
    assert total <= ur.ANSWER_TEXT_BUDGET + ur.MESSAGE_TEXT_MAX + 40
    assert len(answer["messages"]) < 40


# ── errors and rate limits ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_short_rate_limit_is_waited_out_once():
    sleeps: list[float] = []
    reader, fake = _reader(
        {
            "conversations.info": [
                _api_error("ratelimited", 429, {"Retry-After": "2"}),
                _INFO,
            ],
            "conversations.history": [{"ok": True, "messages": []}],
        },
        sleeps=sleeps,
    )
    await reader.read(ConversationRef(channel_id="C0GEN001"))
    assert sleeps == [2.0]
    assert [c[0] for c in fake.calls].count("conversations.info") == 2


@pytest.mark.asyncio
async def test_a_long_rate_limit_is_reported_with_its_wait():
    sleeps: list[float] = []
    reader, _ = _reader(
        {"conversations.info": [_api_error("ratelimited", 429, {"Retry-After": "120"})]},
        sleeps=sleeps,
    )
    with pytest.raises(SlackUserReadError) as err:
        await reader.read(ConversationRef(channel_id="C0GEN001"))
    assert err.value.code == "rate_limited" and err.value.retry_after == 120.0
    assert err.value.status == 429 and sleeps == []


@pytest.mark.parametrize(
    ("code", "expected", "status"),
    [
        ("invalid_auth", "slack_user_token_rejected", 409),
        ("token_revoked", "slack_user_token_rejected", 409),
        ("missing_scope", "missing_scope", 409),
        ("channel_not_found", "conversation_not_found", 404),
        ("not_in_channel", "conversation_not_found", 404),
        ("fatal_error", "slack_api_error", 502),
    ],
)
@pytest.mark.asyncio
async def test_slack_errors_map_to_actionable_codes(code, expected, status):
    reader, _ = _reader({"conversations.info": [_api_error(code, needed="im:history")]})
    with pytest.raises(SlackUserReadError) as err:
        await reader.read(ConversationRef(channel_id="C0GEN001"))
    assert (err.value.code, err.value.status) == (expected, status)
    if code == "missing_scope":
        assert "im:history" in err.value.message


@pytest.mark.asyncio
async def test_transport_failure_is_unreachable_not_a_crash():
    reader, _ = _reader({"conversations.info": [TimeoutError()]})
    with pytest.raises(SlackUserReadError) as err:
        await reader.read(ConversationRef(channel_id="C0GEN001"))
    assert err.value.code == "slack_unreachable"


# ── conversation listing ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_filters_by_name_and_describes_dms():
    reader, fake = _reader(
        {
            "users.conversations": [
                {
                    "ok": True,
                    "channels": [
                        {"id": "C0GEN001", "name": "general"},
                        {"id": "C0ENG001", "name": "eng-backend", "is_private": True},
                        {"id": "D0DM0001", "is_im": True, "user": "U0BOB001"},
                        {"id": "G0MPIM01", "name": "mpdm-a--b-1", "is_mpim": True},
                    ],
                }
            ]
        },
        names={"U0BOB001": "bob"},
    )
    everything = await reader.list_conversations()
    assert [c["kind"] for c in everything["conversations"]] == [
        "channel",
        "private_channel",
        "dm",
        "group_dm",
    ]
    assert everything["conversations"][2]["name"] == "@bob"
    filtered = await reader.list_conversations(query="#ENG")
    assert [c["id"] for c in filtered["conversations"]] == ["C0ENG001"]
    assert fake.calls[0][2]["types"] == "public_channel,private_channel,mpim,im"


# ── the real slack_sdk client against a local fake Slack ─────────────────────


@pytest.mark.asyncio
async def test_real_sdk_client_sends_gets_with_the_user_token_and_honours_retry_after():
    """Exercise ``AsyncWebClient`` itself (not a fake ``api_call``) so the GET
    params, the bearer header and the 429 ``Retry-After`` path are what the SDK
    really produces. The server is local; only the base URL differs from prod."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from slack_sdk.web.async_client import AsyncWebClient

    seen: list[tuple[str, str, dict, str]] = []
    limited = {"count": 0}

    async def handler(request: web.Request) -> web.Response:
        method = request.match_info["method"]
        seen.append(
            (request.method, method, dict(request.query), request.headers.get("Authorization", ""))
        )
        if method == "conversations.info" and limited["count"] == 0:
            limited["count"] += 1
            return web.json_response(
                {"ok": False, "error": "ratelimited"}, status=429, headers={"Retry-After": "1"}
            )
        if method == "conversations.info":
            return web.json_response({"ok": True, "channel": {"id": "C0GEN001", "name": "general"}})
        if method == "conversations.history":
            return web.json_response(
                {"ok": True, "messages": [{"ts": "1.0", "user": "U0ALICE1", "text": "hello"}]}
            )
        if method == "users.info":
            return web.json_response(
                {"ok": True, "user": {"id": "U0ALICE1", "profile": {"display_name": "alice"}}}
            )
        return web.json_response({"ok": False, "error": "unknown_method"}, status=404)

    app = web.Application()
    app.router.add_route("*", "/api/{method}", handler)
    server = TestServer(app)
    await server.start_server()
    sleeps: list[float] = []

    async def _sleep(secs: float) -> None:
        sleeps.append(secs)

    try:
        client = AsyncWebClient(token=TOKEN, base_url=str(server.make_url("/api/")))
        reader = SlackUserReader(TOKEN, client=client, sleep=_sleep)
        answer = await reader.read(ConversationRef(channel_id="C0GEN001"), limit=10)
    finally:
        await server.close()
    assert answer["messages"][0] == {
        "ts": "1.0",
        "time": "1970-01-01 00:00Z",
        "author": "alice",
        "text": "hello",
        "author_id": "U0ALICE1",
    }
    assert sleeps == [1.0]
    assert all(verb == "GET" for verb, *_ in seen)
    assert all(auth == f"Bearer {TOKEN}" for *_, auth in seen)
    history = next(q for _, m, q, _ in seen if m == "conversations.history")
    assert history == {"channel": "C0GEN001", "limit": "10"}
