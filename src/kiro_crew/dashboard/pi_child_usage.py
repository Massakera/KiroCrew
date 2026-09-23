"""Hang pi-subagents children on the parent session's usage ledger.

Rows are written on the parent slot (``surface=subagent``) so Telemetry's
turn drilldown lists them under that session. A ``subagent:`` slot key is
still not a session of its own. System sees a child only while a
non-terminal frame is in hand: the child runs inside the parent Pi process,
so RSS, CPU, and pid stay unmeasured rather than reported as zero.

The coordinator's own Pi turn is not touched here. ``provider_for_completed_turn``
only relabels a droid ACP turn that already carried usage.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

from kiro_crew.agent_sdk.backends import ACP_BACKEND_DROID, ACP_BACKEND_PI
from kiro_crew.dashboard.handlers.usage import append_usage_record_async
from kiro_crew.security import redact_and_truncate, redact_credentials, redact_exfiltration_urls

_LOCK = threading.Lock()
_LIVE: dict[str, dict[str, Any]] = {}
_PERSISTED: OrderedDict[str, None] = OrderedDict()
_SPAWNED: set[str] = set()
_MAX_LIVE = 256
_MAX_PERSISTED = 4096
_UNACCEPTABLE_AGENT = frozenset({"kiro", "kirocrew"})


def reset_pi_child_usage_for_tests() -> None:
    """Drop the in-memory live set and the persist dedupe."""
    with _LOCK:
        _LIVE.clear()
        _PERSISTED.clear()
        _SPAWNED.clear()


def provider_for_completed_turn(seam: str, backend: str) -> str:
    """Provider label for a turn that already has a usage frame.

    A droid ACP turn is ``droid``. Every other backend, including the Pi
    coordinator, keeps the seam it already persisted under.
    """
    if backend == ACP_BACKEND_DROID:
        return ACP_BACKEND_DROID
    return seam


def live_task_rows() -> list[dict[str, object]]:
    """System task rows for children whose latest frame was not terminal.

    ``sampled`` is false: these children share the parent process, and a
    zero RSS would be a measurement this process did not take.
    """
    with _LOCK:
        entries = list(_LIVE.values())
    return [
        {
            "id": entry["id"],
            "task": entry["task"],
            "agent": entry["agent"],
            "parent": entry["session_key"],
            "rss_mb": 0.0,
            "peak_rss_mb": 0.0,
            "cpu_cores": 0.0,
            "procs": None,
            "mcp": None,
            "started_at": entry["started"],
            "shared": False,
            "pid": None,
            "sampled": False,
        }
        for entry in entries
    ]


async def observe_pi_children_async(
    state: object,
    *,
    slot_key: str,
    session_key: str,
    children: Sequence[Mapping[str, Any]],
) -> None:
    """Record terminal children and publish live ones on the existing WS events."""
    broadcasts, records = _plan(slot_key, session_key, children, finishing=False)
    await _flush(state, broadcasts, records)


async def close_pi_children_async(state: object, *, slot_key: str, session_key: str) -> None:
    """Parent turn ended. Persist the last live snapshot, then drop the row."""
    with _LOCK:
        pending = [entry["child"] for entry in _LIVE.values() if entry["slot_key"] == slot_key]
    if not pending:
        return
    broadcasts, records = _plan(slot_key, session_key, pending, finishing=True)
    await _flush(state, broadcasts, records)


def _plan(
    slot_key: str,
    session_key: str,
    children: Sequence[Mapping[str, Any]],
    *,
    finishing: bool,
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]]]:
    broadcasts: list[tuple[str, dict[str, Any]]] = []
    records: list[dict[str, Any]] = []
    now = time.time()
    with _LOCK:
        for child in children:
            if not isinstance(child, dict):
                continue
            card_id = str(child.get("id") or "")
            if not card_id:
                continue
            agent, model = _labels(child)
            task = redact_and_truncate(str(child.get("task") or ""), 2000)
            live_key = f"{slot_key}\n{card_id}"
            terminal = finishing or bool(child.get("terminal"))
            entry = _LIVE.get(live_key)
            started = float(entry["started"]) if entry else now
            if not terminal:
                if entry is None and len(_LIVE) >= _MAX_LIVE:
                    _LIVE.pop(next(iter(_LIVE)))
                _LIVE[live_key] = {
                    "id": card_id,
                    "task": task[:80],
                    "agent": agent,
                    "session_key": session_key,
                    "slot_key": slot_key,
                    "started": started,
                    "child": child,
                }
                if live_key not in _SPAWNED:
                    _SPAWNED.add(live_key)
                    broadcasts.append(
                        ("subagent_spawn", _spawn_payload(slot_key, card_id, agent, model, task, child))
                    )
                continue
            _LIVE.pop(live_key, None)
            if live_key not in _SPAWNED:
                _SPAWNED.add(live_key)
                broadcasts.append(
                    ("subagent_spawn", _spawn_payload(slot_key, card_id, agent, model, task, child))
                )
            elapsed = 0.0 if entry is None else max(0.0, now - started)
            broadcasts.append(
                (
                    "subagent_done",
                    _done_payload(slot_key, card_id, agent, model, task, child, elapsed),
                )
            )
            persist_key = f"{slot_key}\n{card_id}"
            if persist_key in _PERSISTED or not _billable(child):
                continue
            _PERSISTED[persist_key] = None
            while len(_PERSISTED) > _MAX_PERSISTED:
                _PERSISTED.popitem(last=False)
            records.append(_record(child, slot_key, agent, model))
    return broadcasts, records


async def _flush(
    state: object,
    broadcasts: list[tuple[str, dict[str, Any]]],
    records: list[dict[str, Any]],
) -> None:
    for record in records:
        await append_usage_record_async(record)
    send = getattr(state, "broadcast_ws", None)
    if send is None:
        return
    for kind, payload in broadcasts:
        send(kind, payload)


def _labels(child: dict[str, Any]) -> tuple[str, str]:
    model = str(child.get("model") or "") or str(child.get("requested_model") or "")
    agent = str(child.get("agent") or "").strip()
    if agent:
        agent, _ = redact_credentials(agent)
        agent, _ = redact_exfiltration_urls(agent)
        agent = agent.strip()
    if not agent or agent.lower() in _UNACCEPTABLE_AGENT:
        agent = model or "pi-child"
        if agent.lower() in _UNACCEPTABLE_AGENT:
            agent = "pi-child"
    return agent[:80], model


def _billable(child: dict[str, Any]) -> bool:
    for key in ("input", "output", "cache_read", "cache_write", "total_tokens", "cost"):
        if child.get(key) is not None:
            return True
    return False


def _record(child: dict[str, Any], slot_key: str, agent: str, model: str) -> dict[str, Any]:
    served = model or str(child.get("requested_model") or "")
    provider = ACP_BACKEND_DROID if served.startswith("factory/") else ACP_BACKEND_PI
    record: dict[str, Any] = {
        "slot": slot_key,
        "provider": provider,
        "model": served,
        "surface": "subagent",
        "agent": agent,
    }
    _copy(record, "input", child.get("input"))
    _copy(record, "output", child.get("output"))
    _copy(record, "cache_read", child.get("cache_read"))
    _copy(record, "cache_create", child.get("cache_write"))
    _copy(record, "cost", child.get("cost"))
    _copy(record, "total_tokens", child.get("total_tokens"))
    _copy(record, "turns", child.get("turns"))
    return record


def _copy(record: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        record[key] = value


def _usage_fields(child: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    mapping = (
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("cache_read", "cache_read_tokens"),
        ("cache_write", "cache_write_tokens"),
        ("total_tokens", "total_tokens"),
        ("cost", "cost_usd"),
    )
    for src, dst in mapping:
        value = child.get(src)
        if value is not None:
            fields[dst] = value
    return fields


def _spawn_payload(
    slot_key: str,
    card_id: str,
    agent: str,
    model: str,
    task: str,
    child: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": card_id,
        "slot": slot_key,
        "task": task,
        "agent": agent,
        **_usage_fields(child),
    }
    if model:
        payload["model"] = model
    requested = str(child.get("requested_model") or "")
    if requested:
        payload["requested_model"] = requested
    return payload


def _done_payload(
    slot_key: str,
    card_id: str,
    agent: str,
    model: str,
    task: str,
    child: dict[str, Any],
    elapsed: float,
) -> dict[str, Any]:
    payload = _spawn_payload(slot_key, card_id, agent, model, task, child)
    payload["elapsed"] = elapsed
    payload["outcome"] = "failed" if child.get("failed") else "completed"
    return payload
