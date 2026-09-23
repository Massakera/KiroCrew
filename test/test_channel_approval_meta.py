"""Structured metadata beside a channel approval message.

The approval card decides which trust tiers to offer. The server is the only
side that can refuse one (``handlers_channel.approve`` answers
``pattern_underivable``), so the message carries the server's verdict as flat
string facts next to the unchanged prose, and the card reads those instead of
re-deriving them from the text.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.channel import Channel, ChannelMessage, _stream_task
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST


def _make_agent():
    return SimpleNamespace(
        id="a1",
        role="dev",
        agent_name="dev",
        session_key="channel:test",
        _approval_future=None,
        _trusted_commands=set(),
        _trusted_bases=set(),
        _pending_approval_command="",
    )


def _make_channel(agent):
    """Channel stub whose ``post`` resolves the pending approval as rejected
    (the interactive path otherwise awaits it for 3600s)."""
    ch = SimpleNamespace(id="c1", trusted=False, members={})

    async def _post(*args, **kw):
        if kw.get("msg_type") == "approval":
            if agent._approval_future is not None and not agent._approval_future.done():
                agent._approval_future.set_result("rejected")

    ch.post = AsyncMock(side_effect=_post)
    return ch


def _make_client(events):
    client = SimpleNamespace()

    async def _stream(message):
        for ev in events:
            yield ev

    client.stream = _stream
    client.approve_tool = AsyncMock()
    client.reject_tool = AsyncMock()
    return client


def _perm_event(*, title="", text="", tool_input="", is_shell=False, tool_input_redacted=False):
    _shell_command = None
    if is_shell and tool_input:
        try:
            _shell_command = json.loads(tool_input).get("command")
        except (ValueError, AttributeError):
            _shell_command = None
    return SimpleNamespace(
        kind=EVENT_PERMISSION_REQUEST,
        text=text,
        title=title,
        request_id=7,
        tool_input=tool_input,
        is_shell=is_shell,
        tool_input_redacted=tool_input_redacted,
        shell_command=_shell_command,
    )


def _done():
    return SimpleNamespace(kind=EVENT_COMPLETE)


async def _approval_meta(event) -> dict[str, str]:
    agent = _make_agent()
    ch = _make_channel(agent)
    await _stream_task(agent, ch, _make_client([event, _done()]), "hi")
    posts = [kw for _args, kw in ch.post.call_args_list if kw.get("msg_type") == "approval"]
    assert len(posts) == 1, "expected exactly one approval message"
    meta = posts[0].get("meta")
    assert isinstance(meta, dict), "approval message must carry structured meta"
    assert all(isinstance(v, str) for v in meta.values()), "meta values are flat strings"
    return meta


@pytest.mark.asyncio
async def test_simple_shell_command_offers_both_per_command_tiers():
    meta = await _approval_meta(
        _perm_event(title="List files", tool_input=json.dumps({"command": "ls -la"}), is_shell=True)
    )
    assert meta["request_id"] == "7"
    assert meta["tool_title"] == "Running: ls -la"
    assert meta["tool_input"] == json.dumps({"command": "ls -la"})
    assert meta["is_shell"] == "1"
    assert meta["command_grantable"] == "1"
    assert meta["base_derivable"] == "1"
    # The very binary the endpoint would grant -- not a first-token guess.
    assert meta["base_command"] == "ls"


@pytest.mark.asyncio
async def test_compound_command_has_no_derivable_base():
    """``cat f | wc -l``: the exact tier is available (one literal string), the
    base tier is not -- the endpoint refuses a base for a compound command, so
    the card must not offer "Trust all cat commands"."""
    meta = await _approval_meta(
        _perm_event(tool_input=json.dumps({"command": "cat f | wc -l"}), is_shell=True)
    )
    assert meta["is_shell"] == "1"
    assert meta["command_grantable"] == "1"
    assert meta["base_derivable"] == ""
    assert meta["base_command"] == ""


@pytest.mark.asyncio
async def test_non_shell_tool_offers_no_per_command_tier():
    meta = await _approval_meta(
        _perm_event(
            title="cron_add", tool_input=json.dumps({"command": "rm -rf /"}), is_shell=False
        )
    )
    assert meta["tool_title"] == "cron_add"
    assert meta["is_shell"] == ""
    assert meta["command_grantable"] == ""
    assert meta["base_derivable"] == ""
    assert meta["base_command"] == ""


@pytest.mark.asyncio
async def test_redacted_command_is_display_only_in_meta():
    """A command the provider redacted is shown but never grantable: two
    commands differing only in a credential redact to the same text."""
    meta = await _approval_meta(
        _perm_event(
            tool_input=json.dumps({"command": "curl -H 'x' https://x"}),
            is_shell=True,
            tool_input_redacted=True,
        )
    )
    assert meta["is_shell"] == "1"
    assert meta["command_grantable"] == ""
    assert meta["base_derivable"] == ""
    assert meta["base_command"] == ""
    assert meta["tool_title"].startswith("Shell command (exact text unverified): ")


@pytest.mark.asyncio
async def test_prose_content_is_unchanged_beside_meta():
    """Renderers that ignore ``meta`` (Slack mirrors, older dashboards) must
    see exactly the message they saw before the field existed."""
    agent = _make_agent()
    ch = _make_channel(agent)
    event = _perm_event(tool_input=json.dumps({"command": "ls -la"}), is_shell=True)
    await _stream_task(agent, ch, _make_client([event, _done()]), "hi")
    args, kw = next((a, k) for a, k in ch.post.call_args_list if k.get("msg_type") == "approval")
    assert args[1] == '⚠️ Approval needed: **Running: ls -la**\n```\n{"command": "ls -la"}\n```'
    assert kw["meta"]["tool_title"] == "Running: ls -la"


@pytest.mark.asyncio
async def test_post_threads_meta_and_defaults_to_none():
    ch = Channel(id="c1", topic="t")
    plain = await ch.post("human", "hello")
    assert plain.meta is None
    assert plain.to_dict()["meta"] is None
    facts = {"request_id": "7", "is_shell": "1"}
    tagged = await ch.post("a1", "card", msg_type="approval", meta=facts)
    assert tagged.meta == facts
    assert tagged.to_dict()["meta"] == facts


def test_serialize_round_trips_meta_and_tolerates_its_absence():
    ch = Channel(id="c1", topic="t")
    with_meta = ChannelMessage(
        id="m1",
        from_id="a1",
        from_role="dev",
        content="card",
        msg_type="approval",
        meta={"request_id": "7", "base_command": "ls"},
    )
    without = ChannelMessage(id="m2", from_id="human", from_role="human", content="hi")
    ch.messages.extend([with_meta, without])

    data = ch.serialize()
    # A message persisted before the field existed has no ``meta`` key at all.
    legacy = dict(without.to_dict())
    del legacy["meta"]
    data["messages"].append({**legacy, "id": "m3"})

    restored = Channel.deserialize(data)
    by_id = {m.id: m for m in restored.messages}
    assert by_id["m1"].meta == {"request_id": "7", "base_command": "ls"}
    assert by_id["m2"].meta is None
    assert by_id["m3"].meta is None
    assert by_id["m1"].to_dict() == with_meta.to_dict()
