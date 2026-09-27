"""End to end: the ``slack_read`` MCP tool through the gateway door to Slack.

Every hop is the real code -- the ``kirocrew-core`` handler, ``mcp_core._post``
over loopback HTTP, the ``/api/slack-user`` handlers, the governance check with
the default (standalone) ceiling, ``SlackUserReader`` and ``slack_sdk``'s
``AsyncWebClient``. Only the two ends are local stand-ins: an aiohttp app that
authenticates the internal secret the way ``token_auth_middleware`` does, and a
fake Slack Web API. This is the acceptance path of "summarize thread X": a
permalink goes in, the thread comes back fenced, with no bot involved.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kiro_crew import mcp_core
from kiro_crew.dashboard.handlers import slack_user
from kiro_crew.mcp_tools import slack as tools

TOKEN = "xoxp-1111-2222-3333-abcdef"
SECRET = "e2e-internal-secret"

#: The real ``_post``, captured at import: the suite's autouse ``gateway_posts``
#: recorder replaces it per test. These tests opt back in explicitly, and only
#: toward the loopback test server each one starts (``mcp_core._API`` is pinned
#: to it), never toward a real gateway.
_REAL_POST = mcp_core._post


def _fake_slack() -> tuple[web.Application, list[tuple[str, dict, str]]]:
    seen: list[tuple[str, dict, str]] = []

    async def handler(request: web.Request) -> web.Response:
        method = request.match_info["method"]
        seen.append((method, dict(request.query), request.headers.get("Authorization", "")))
        if method == "conversations.info":
            return web.json_response(
                {"ok": True, "channel": {"id": "C0ENG001", "name": "eng", "is_private": True}}
            )
        if method == "conversations.replies":
            return web.json_response(
                {
                    "ok": True,
                    "messages": [
                        {
                            "ts": "1712790000.000100",
                            "user": "U0ANA001",
                            "text": "migrate on Friday?",
                            "reply_count": 1,
                        },
                        {
                            "ts": "1712790100.000200",
                            "user": "U0OWNER1",
                            "thread_ts": "1712790000.000100",
                            "text": "agreed, <@U0ANA001> owns the runbook",
                        },
                    ],
                }
            )
        if method == "users.info":
            uid = request.query.get("user", "")
            name = {"U0ANA001": "ana", "U0OWNER1": "me"}.get(uid, uid)
            return web.json_response(
                {"ok": True, "user": {"id": uid, "profile": {"display_name": name}}}
            )
        return web.json_response({"ok": False, "error": "unknown_method"}, status=404)

    app = web.Application()
    app.router.add_route("*", "/api/{method}", handler)
    return app, seen


def _gateway(state: SimpleNamespace) -> web.Application:
    @web.middleware
    async def _internal_secret(request, handler):
        # The shape token_auth_middleware gives a strict internal path.
        if request.headers.get("X-Internal-Secret") == SECRET:
            request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[_internal_secret])
    app["state"] = state
    slack_user.register(app)
    return app


@pytest.mark.asyncio
async def test_slack_read_permalink_end_to_end(monkeypatch):
    from slack_sdk.web.async_client import AsyncWebClient

    slack_app, seen = _fake_slack()
    slack_server = TestServer(slack_app)
    state = SimpleNamespace(
        _slots={"tab1": SimpleNamespace(_app="", linked_session_key="", _created_by="")},
        subagents=SimpleNamespace(_agents={}),
        crons=SimpleNamespace(_jobs=[]),
    )
    gateway = TestServer(_gateway(state))
    await slack_server.start_server()
    await gateway.start_server()
    slack_base = str(slack_server.make_url("/api/"))
    monkeypatch.setattr(mcp_core, "_API", str(gateway.make_url("")).rstrip("/"))
    monkeypatch.setattr(mcp_core, "_API_UNIX_SOCKET", "")
    monkeypatch.setattr(mcp_core, "_post", _REAL_POST)
    try:
        with (
            patch.object(mcp_core, "_internal_secret", return_value=SECRET),
            patch.object(
                mcp_core, "require_strict_session_key", return_value=("dashboard:tab1", "")
            ),
            patch("kiro_crew.slack.user_read.resolve_user_token", return_value=TOKEN),
            patch(
                "kiro_crew.slack.user_read._default_client",
                lambda token: AsyncWebClient(token=token, base_url=slack_base),
            ),
            patch("kiro_crew.dashboard.handlers.sel", return_value=MagicMock()),
        ):
            out = await asyncio.to_thread(
                tools.slack_read,
                "slack_read",
                {
                    "conversation": "https://acme.slack.com/archives/C0ENG001/p1712790100000200"
                    "?thread_ts=1712790000.000100&cid=C0ENG001"
                },
            )
    finally:
        await gateway.close()
        await slack_server.close()

    assert not out.startswith("Error"), out
    assert "thread 1712790000.000100 in #eng (C0ENG001, private_channel)" in out
    assert (
        "- 2024-04-10 23:00Z · ana · ts 1712790000.000100 · 1 replies\n  migrate on Friday?" in out
    )
    assert "agreed, @ana owns the runbook" in out
    assert out.index("UNTRUSTED DATA") < out.index("<<<BEGIN_UNTRUSTED_SLACK_")
    # Slack saw only reads, all carrying the USER token -- no bot anywhere.
    assert {m for m, _, _ in seen} == {"conversations.info", "conversations.replies", "users.info"}
    assert all(auth == f"Bearer {TOKEN}" for _, _, auth in seen)
    replies = next(q for m, q, _ in seen if m == "conversations.replies")
    assert replies["ts"] == "1712790000.000100" and replies["channel"] == "C0ENG001"


@pytest.mark.asyncio
async def test_without_the_internal_secret_the_door_stays_shut(monkeypatch):
    gateway = TestServer(_gateway(SimpleNamespace(_slots={})))
    await gateway.start_server()
    monkeypatch.setattr(mcp_core, "_API", str(gateway.make_url("")).rstrip("/"))
    monkeypatch.setattr(mcp_core, "_API_UNIX_SOCKET", "")
    monkeypatch.setattr(mcp_core, "_post", _REAL_POST)
    try:
        with (
            patch.object(mcp_core, "_internal_secret", return_value="wrong"),
            patch.object(
                mcp_core, "require_strict_session_key", return_value=("dashboard:tab1", "")
            ),
            patch("kiro_crew.dashboard.handlers.sel", return_value=MagicMock()),
        ):
            out = await asyncio.to_thread(tools.slack_search, "slack_search", {"query": "x"})
    finally:
        await gateway.close()
    assert out.startswith("Error: could not search Slack") and "[forbidden]" in out
