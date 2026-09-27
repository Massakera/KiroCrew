"""The gateway door for the "Slack as me" tools (``dashboard/handlers/slack_user.py``).

Covers who may read (caller classes), the transport and governance gates, the
missing-token answer, the SEL trail, and that the routes are wired onto the
strict internal transport on both gateway entrypoints.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import slack_user
from kiro_crew.dashboard.handlers.slack_user import caller_refusal

TOKEN = "xoxp-1111-2222-3333-abcdef"


def _slot(**kw: Any) -> SimpleNamespace:
    base = {"_app": "", "linked_session_key": "", "_created_by": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def _state(slots: dict | None = None, agents: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        _slots=dict(slots or {}),
        subagents=SimpleNamespace(_agents=dict(agents or {})),
        crons=SimpleNamespace(_jobs=[]),
        slack_client=None,
    )


# ── caller classes ───────────────────────────────────────────────────────────


def test_owner_dashboard_tab_and_cli_are_admitted():
    state = _state({"tab1": _slot()})
    assert caller_refusal(state, "dashboard:tab1") == ""
    assert caller_refusal(state, "cli_chat") == ""


@pytest.mark.parametrize(
    "key",
    ["", "cron:abc123", "cron:abc123:run9", "taskrunner:job1", "dashboard:cron-abc"],
)
def test_unattended_or_unnamed_callers_are_refused(key):
    state = _state({"cron-abc": _slot()})
    assert caller_refusal(state, key)


def test_a_dashboard_key_with_no_live_tab_is_refused():
    assert "no live dashboard tab" in caller_refusal(_state(), "dashboard:gone")


def test_an_app_owned_tab_is_refused():
    state = _state({"tab1": _slot(_app="issue-radar")})
    assert "app" in caller_refusal(state, "dashboard:tab1")


def test_a_channel_linked_tab_is_refused_unless_it_is_the_owner_dm():
    state = _state({"tab1": _slot(linked_session_key="slack:C0GEN001:1712793600.0001")})
    with patch(
        "kiro_crew.dashboard.session_control.owner_dm_refusal",
        return_value="the conversation is not a 1:1 direct message",
    ):
        assert "channel-linked" in caller_refusal(state, "dashboard:tab1")
    with patch("kiro_crew.dashboard.session_control.owner_dm_refusal", return_value=""):
        assert caller_refusal(state, "dashboard:tab1") == ""


def test_a_cron_link_is_not_a_channel_link():
    state = _state({"tab1": _slot(linked_session_key="cron:abc123")})
    assert caller_refusal(state, "dashboard:tab1") == ""


def test_a_mirrored_tab_is_refused():
    state = _state({"tab1": _slot()})
    with patch("kiro_crew.dashboard.session_control._has_channel_mirror", return_value=True):
        assert "mirrored" in caller_refusal(state, "dashboard:tab1")


def test_channel_sessions_are_admitted_only_as_the_owners_own_dm():
    state = _state()
    with patch(
        "kiro_crew.dashboard.session_control.session_owner_dm_refusal",
        return_value="the conversation is not a 1:1 direct message",
    ):
        assert "reaches others" in caller_refusal(state, "slack:C0GEN001:1712793600.0001")
    with patch("kiro_crew.dashboard.session_control.session_owner_dm_refusal", return_value=""):
        assert caller_refusal(state, "slack:dm:U0OWNER1") == ""


def test_a_subagent_is_judged_by_the_root_of_its_dispatch_chain():
    child = SimpleNamespace(id="a1", conversation_key="", parent_session_key="subagent:a0", app="")
    parent = SimpleNamespace(
        id="a0", conversation_key="", parent_session_key="dashboard:tab1", app=""
    )
    state = _state({"tab1": _slot()}, {"a1": child, "a0": parent})
    assert caller_refusal(state, "subagent:a1") == ""
    # Same chain, but rooted in a cron run: refused on the ROOT's class.
    parent.parent_session_key = "cron:job1"
    assert "unattended" in caller_refusal(state, "subagent:a1")


def test_app_spawned_orphaned_and_looping_subagents_are_refused():
    app_child = SimpleNamespace(
        id="a1", conversation_key="", parent_session_key="dashboard:tab1", app="x"
    )
    state = _state({"tab1": _slot()}, {"a1": app_child})
    assert "app" in caller_refusal(state, "subagent:a1")
    assert "no record" in caller_refusal(state, "subagent:missing")
    loop = SimpleNamespace(id="a2", conversation_key="", parent_session_key="subagent:a2", app="")
    assert caller_refusal(_state(agents={"a2": loop}), "subagent:a2")


# ── the HTTP door ────────────────────────────────────────────────────────────


def _app(state: SimpleNamespace, *, internal: bool = True) -> web.Application:
    @web.middleware
    async def _auth(request, handler):
        if internal:
            request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    slack_user.register(app)
    return app


class _FakeReader:
    def __init__(self, token: str) -> None:
        assert token == TOKEN

    async def search(self, query, **kw):
        return {
            "query": query,
            "total": 1,
            "page": 1,
            "pages": 1,
            "matches": [{"ts": "1.0"}],
            "truncated": False,
        }

    async def read(self, ref, **kw):
        return {
            "conversation": {"id": ref.channel_id or "C0X", "kind": "channel", "name": "#x"},
            "messages": [],
            "truncated": False,
            "_kw": {k: v for k, v in kw.items()},
        }

    async def list_conversations(self, **kw):
        return {"conversations": [], "truncated": False}


@pytest.fixture()
def sel_mock():
    mock = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=mock):
        yield mock


@pytest.fixture()
def permitted():
    with patch(
        "kiro_crew.platform.governance_profiles.governance_permits",
        return_value=SimpleNamespace(permitted=True),
    ) as gp:
        yield gp


async def _post(app, path, body, key="dashboard:tab1"):
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(path, json=body, headers={"X-Session-Key": key})
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_search_happy_path_and_audit(sel_mock, permitted):
    state = _state({"tab1": _slot()})
    with (
        patch("kiro_crew.slack.user_read.resolve_user_token", return_value=TOKEN),
        patch("kiro_crew.slack.user_read.SlackUserReader", _FakeReader),
    ):
        status, body = await _post(_app(state), "/api/slack-user/search", {"query": "launch"})
    assert status == 200 and body["query"] == "launch"
    permitted.assert_called_once()
    assert permitted.call_args.args[0] == "capabilities.slack_user_read"
    assert permitted.call_args.kwargs["fail_closed"] is True
    audit = sel_mock.log_tool_invocation.call_args.kwargs
    assert audit["tool_name"] == "slack_search" and audit["outcome"] == "completed"
    # The audit trail never carries the query text.
    assert "launch" not in audit["resources"]


@pytest.mark.asyncio
async def test_read_passes_parsed_bounds(sel_mock, permitted):
    state = _state({"tab1": _slot()})
    with (
        patch("kiro_crew.slack.user_read.resolve_user_token", return_value=TOKEN),
        patch("kiro_crew.slack.user_read.SlackUserReader", _FakeReader),
    ):
        status, body = await _post(
            _app(state),
            "/api/slack-user/read",
            {"conversation": "C0GEN001", "since": "2026-09-01", "limit": 5},
        )
    assert status == 200
    assert body["_kw"]["oldest"] == "1788220800.000000" and body["_kw"]["limit"] == 5


@pytest.mark.asyncio
async def test_bad_arguments_are_a_400(sel_mock, permitted):
    state = _state({"tab1": _slot()})
    with (
        patch("kiro_crew.slack.user_read.resolve_user_token", return_value=TOKEN),
        patch("kiro_crew.slack.user_read.SlackUserReader", _FakeReader),
    ):
        status, body = await _post(
            _app(state), "/api/slack-user/read", {"conversation": "https://evil.example/x"}
        )
    assert status == 400 and body["code"] == "invalid_argument"


@pytest.mark.asyncio
async def test_non_internal_transport_is_refused(sel_mock, permitted):
    status, body = await _post(
        _app(_state({"tab1": _slot()}), internal=False), "/api/slack-user/search", {"query": "x"}
    )
    assert status == 403 and body["code"] == "forbidden"
    assert sel_mock.log_tool_invocation.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
async def test_refused_caller_never_reaches_the_token(sel_mock, permitted):
    with patch("kiro_crew.slack.user_read.resolve_user_token") as token:
        status, body = await _post(
            _app(_state()), "/api/slack-user/search", {"query": "x"}, key="cron:abc123"
        )
    assert status == 403 and body["code"] == "caller_not_permitted"
    token.assert_not_called()


@pytest.mark.asyncio
async def test_governance_denial_is_refused(sel_mock):
    with (
        patch(
            "kiro_crew.platform.governance_profiles.governance_permits",
            return_value=SimpleNamespace(permitted=False),
        ),
        patch("kiro_crew.slack.user_read.resolve_user_token") as token,
    ):
        status, body = await _post(
            _app(_state({"tab1": _slot()})), "/api/slack-user/search", {"query": "x"}
        )
    assert status == 403 and body["code"] == "governance_denied"
    token.assert_not_called()


@pytest.mark.asyncio
async def test_governance_failure_fails_closed(sel_mock):
    with patch(
        "kiro_crew.platform.governance_profiles.governance_permits",
        side_effect=RuntimeError("boom"),
    ):
        status, body = await _post(
            _app(_state({"tab1": _slot()})), "/api/slack-user/search", {"query": "x"}
        )
    assert status == 403 and body["code"] == "governance_denied"


@pytest.mark.asyncio
async def test_missing_token_says_how_to_set_it_up(sel_mock, permitted):
    with patch("kiro_crew.slack.user_read.resolve_user_token", return_value=""):
        status, body = await _post(
            _app(_state({"tab1": _slot()})), "/api/slack-user/conversations", {}
        )
    assert status == 409 and body["code"] == "slack_user_token_missing"
    assert "kirocrew setup --slack" in body["error"]


@pytest.mark.asyncio
async def test_a_bot_token_in_the_slot_is_refused(sel_mock, permitted):
    with patch("kiro_crew.slack.user_read.resolve_user_token", return_value="xoxb-1-2-3-bot"):
        status, body = await _post(
            _app(_state({"tab1": _slot()})), "/api/slack-user/search", {"query": "x"}
        )
    assert status == 409 and body["code"] == "slack_user_token_invalid"


@pytest.mark.asyncio
async def test_rate_limit_carries_retry_after(sel_mock, permitted):
    from kiro_crew.slack.user_read import SlackUserReadError

    class _Limited(_FakeReader):
        async def search(self, query, **kw):
            raise SlackUserReadError("rate_limited", "Slack rate limit reached", 429, 30.0)

    with (
        patch("kiro_crew.slack.user_read.resolve_user_token", return_value=TOKEN),
        patch("kiro_crew.slack.user_read.SlackUserReader", _Limited),
    ):
        status, body = await _post(
            _app(_state({"tab1": _slot()})), "/api/slack-user/search", {"query": "x"}
        )
    assert status == 429 and body["retry_after"] == 30.0


# ── wiring ───────────────────────────────────────────────────────────────────


def test_routes_are_on_the_strict_internal_transport():
    from kiro_crew.dashboard import server
    from kiro_crew.dashboard.token_auth import internal_path_matches

    for leaf in slack_user.OPERATIONS:
        assert internal_path_matches(f"/api/slack-user/{leaf}", server._STRICT_INTERNAL_API_PATHS)


def test_routes_are_registered_with_the_mcp_routes():
    """Both entrypoints (dashboard and headless) mount ``_register_mcp_routes``."""
    from kiro_crew.dashboard import server

    app = web.Application()
    server._register_mcp_routes(app)
    paths = {getattr(r, "resource", None) and r.resource.canonical for r in app.router.routes()}
    for leaf in slack_user.OPERATIONS:
        assert f"/api/slack-user/{leaf}" in paths


def test_the_token_is_vault_only(tmp_path, monkeypatch):
    """An ``.env`` entry or an environment variable is NOT a token source."""
    from kiro_crew.secrets import SecretVault
    from kiro_crew.slack import user_read

    monkeypatch.setenv("SLACK_USER_TOKEN", TOKEN)
    (tmp_path / ".env").write_text(f"SLACK_USER_TOKEN={TOKEN}\n")
    with patch("kiro_crew.config.paths.config_dir", return_value=tmp_path):
        assert user_read.resolve_user_token() == ""
        SecretVault(tmp_path).set_sync("SLACK_USER_TOKEN", TOKEN)
        assert user_read.resolve_user_token() == TOKEN
