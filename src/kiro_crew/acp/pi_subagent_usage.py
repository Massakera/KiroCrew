"""Child usage that pi-subagents 0.69 already records on a tool update.

The coordinator's own Pi turn is a different wire (``pi-acp`` answering
``session/prompt``). This module does not read that frame and does not
invent one. It only recognizes a child the extension already described:

* a foreground ``results`` entry with ``usage`` (preferred over
  ``totalChildUsage``, which is those same children added together)
* an async status carrying ``model`` or ``requestedModel`` plus
  ``totalTokens``, ``totalCost``, or ``sessionFile``

Recognition is the shape of that object, not which ACP backend is running,
so a Droid child (``factory/...``) and any other Pi child share one parser.

Missing counts stay missing. The one filled zero is a ``factory/...`` cost:
pi-droid-sdk publishes tokens and a zero dollar amount, and a missing cost
on that model is that same published zero, not a price computed here.
"""

from __future__ import annotations

import hashlib
from typing import Any, TypedDict

from kiro_crew.acp.types import TERMINAL_TOOL_STATUSES

_USAGE_NUMBERS = ("input", "output", "cacheRead", "cacheWrite", "cost")
_LIVE_STATES = frozenset(
    {"running", "pending", "queued", "starting", "in_progress", "in-progress"}
)
_DONE_STATES = frozenset(
    {
        "complete",
        "completed",
        "failed",
        "stopped",
        "rejected",
        "error",
        "done",
        "cancelled",
        "canceled",
        "success",
        "succeeded",
    }
)
_FAILED_STATES = frozenset(
    {"failed", "error", "rejected", "cancelled", "canceled", "stopped"}
)


class PiChildUsage(TypedDict):
    """One child, with absent measurements as ``None`` (not zero)."""

    id: str
    agent: str
    model: str
    requested_model: str
    task: str
    status: str
    terminal: bool
    failed: bool
    input: int | None
    output: int | None
    cache_read: int | None
    cache_write: int | None
    cost: float | None
    total_tokens: int | None
    turns: int | None


def parse_pi_subagent_children(update: dict[str, Any]) -> list[PiChildUsage]:
    """Children carried on one ``tool_call_update``, or an empty list.

    The session-update wrapper itself is not a child. Only ``rawOutput`` and
    a top-level ``details`` block are. A caller that already holds the tool
    result object (no ``sessionUpdate``) is parsed as that object.
    """
    if not isinstance(update, dict):
        return []
    parent_status = str(update.get("status") or "")
    tool_call_id = str(update.get("toolCallId") or "")
    blobs: list[dict[str, Any]] = []
    raw = update.get("rawOutput")
    if isinstance(raw, dict):
        blobs.append(raw)
    details = update.get("details")
    if isinstance(details, dict):
        blobs.append(details)
    if not blobs and "sessionUpdate" not in update:
        blobs.append(update)
    rows: list[PiChildUsage] = []
    seen: set[str] = set()
    for blob in blobs:
        for row in _rows_from(blob, parent_status, tool_call_id):
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            rows.append(row)
    return rows


def _rows_from(
    blob: dict[str, Any], parent_status: str, tool_call_id: str
) -> list[PiChildUsage]:
    if not isinstance(blob, dict) or "sessionUpdate" in blob:
        return []
    details = blob.get("details")
    if isinstance(details, dict):
        inner = _rows_from(details, parent_status, tool_call_id)
        if inner:
            return inner
    results = blob.get("results")
    if isinstance(results, list):
        per = [
            row
            for index, item in enumerate(results)
            if isinstance(item, dict)
            and (row := _row_from_result(item, index, parent_status, tool_call_id))
            is not None
        ]
        if per:
            return per
    total = blob.get("totalChildUsage")
    if isinstance(total, dict) and _has_usage_number(total):
        return [_row_from_aggregate(blob, total, parent_status, tool_call_id)]
    nested: list[PiChildUsage] = []
    children = blob.get("children")
    if isinstance(children, list):
        for index, child in enumerate(children):
            if isinstance(child, dict):
                nested.extend(_rows_from(child, parent_status, tool_call_id))
    if isinstance(blob.get("usage"), dict):
        row = _row_from_result(blob, 0, parent_status, tool_call_id)
        if row is not None:
            return [row, *nested]
    own = _row_from_async(blob, parent_status, tool_call_id, index=None)
    if own is not None:
        return [own, *nested]
    return nested


def _row_from_result(
    item: dict[str, Any],
    index: int,
    parent_status: str,
    tool_call_id: str,
) -> PiChildUsage | None:
    usage = item.get("usage") if isinstance(item.get("usage"), dict) else None
    model = _text(item.get("model"))
    agent = _text(item.get("agent"))
    session_file = _text(item.get("sessionFile"))
    if usage is None and not model and not session_file:
        return None
    if usage is not None and not _has_usage_number(usage) and not model and not session_file:
        return None
    status = _text(item.get("status") or item.get("state"))
    exit_code = item.get("exitCode")
    finished = isinstance(exit_code, int) and not isinstance(exit_code, bool)
    if finished and not status and exit_code != 0:
        status = "failed"
    numbers = _from_usage(usage) if usage is not None else _empty_numbers()
    raw_index = item.get("index")
    if isinstance(raw_index, int) and not isinstance(raw_index, bool):
        index = raw_index
    return _finish(
        item,
        index=index,
        parent_status=parent_status,
        tool_call_id=tool_call_id,
        agent=agent,
        model=model,
        requested_model=_text(item.get("requestedModel")),
        session_file=session_file,
        status=status,
        force_terminal=finished,
        numbers=numbers,
        total_tokens=None,
    )


def _row_from_aggregate(
    blob: dict[str, Any],
    total: dict[str, Any],
    parent_status: str,
    tool_call_id: str,
) -> PiChildUsage:
    """One row when the extension published only the summed child usage."""
    results = blob.get("results")
    agent = ""
    model = _text(blob.get("model"))
    if isinstance(results, list) and len(results) == 1 and isinstance(results[0], dict):
        agent = _text(results[0].get("agent"))
        model = model or _text(results[0].get("model"))
    return _finish(
        blob,
        index=0,
        parent_status=parent_status,
        tool_call_id=tool_call_id,
        agent=agent or "pi-child",
        model=model,
        requested_model=_text(blob.get("requestedModel")),
        session_file=_text(blob.get("sessionFile")),
        status=_text(blob.get("status") or blob.get("state")),
        force_terminal=False,
        numbers=_from_usage(total),
        total_tokens=None,
    )


def _row_from_async(
    blob: dict[str, Any],
    parent_status: str,
    tool_call_id: str,
    index: int | None,
) -> PiChildUsage | None:
    model = _text(blob.get("model"))
    requested = _text(blob.get("requestedModel"))
    if not model and not requested:
        return None
    total_tokens = _opt_int(blob.get("totalTokens"))
    cost = _cost_value(blob.get("totalCost"))
    session_file = _text(blob.get("sessionFile"))
    if total_tokens is None and cost is None and not session_file:
        return None
    identity = _text(blob.get("state") or blob.get("status")) or _text(blob.get("agent"))
    identity = identity or _text(blob.get("runId") or blob.get("id"))
    tool = _text(blob.get("toolName") or blob.get("tool"))
    if not identity and tool != "subagent":
        return None
    numbers = _empty_numbers()
    numbers["cost"] = cost
    turns = _opt_int(blob.get("turnCount"))
    if turns is None:
        turns = _opt_int(blob.get("turns"))
    numbers["turns"] = turns
    return _finish(
        blob,
        index=0 if index is None else index,
        parent_status=parent_status,
        tool_call_id=tool_call_id,
        agent=_text(blob.get("agent")),
        model=model,
        requested_model=requested,
        session_file=session_file,
        status=_text(blob.get("state") or blob.get("status")),
        force_terminal=False,
        numbers=numbers,
        total_tokens=total_tokens,
    )


def _finish(
    blob: dict[str, Any],
    *,
    index: int,
    parent_status: str,
    tool_call_id: str,
    agent: str,
    model: str,
    requested_model: str,
    session_file: str,
    status: str,
    force_terminal: bool,
    numbers: dict[str, Any],
    total_tokens: int | None,
) -> PiChildUsage:
    served = model or requested_model
    cost = numbers["cost"]
    if served.startswith("factory/") and cost is None:
        cost = 0.0
    state = status.lower()
    if state in _LIVE_STATES:
        terminal = False
    elif state in _DONE_STATES or force_terminal:
        terminal = True
    else:
        terminal = parent_status.lower() in TERMINAL_TOOL_STATUSES
    run_id = _text(blob.get("runId") or blob.get("id"))
    row: PiChildUsage = {
        "id": _card_id(tool_call_id, index, run_id, session_file),
        "agent": agent,
        "model": model,
        "requested_model": requested_model,
        "task": _text(blob.get("task")),
        "status": status,
        "terminal": terminal,
        "failed": state in _FAILED_STATES,
        "input": numbers["input"],
        "output": numbers["output"],
        "cache_read": numbers["cache_read"],
        "cache_write": numbers["cache_write"],
        "cost": cost,
        "total_tokens": total_tokens,
        "turns": numbers["turns"],
    }
    return row


def _card_id(tool_call_id: str, index: int, run_id: str, session_file: str) -> str:
    """Stable card id. A session path stays out of the id."""
    if run_id:
        token = run_id
    elif session_file:
        token = hashlib.sha256(session_file.encode("utf-8")).hexdigest()[:16]
    else:
        token = f"{tool_call_id}:{index}"
    return f"pi:{token}"


def _from_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "input": _opt_int(usage.get("input")),
        "output": _opt_int(usage.get("output")),
        "cache_read": _opt_int(usage.get("cacheRead")),
        "cache_write": _opt_int(usage.get("cacheWrite")),
        "cost": _opt_float(usage.get("cost")),
        "turns": _opt_int(usage.get("turns")),
    }


def _empty_numbers() -> dict[str, Any]:
    return {
        "input": None,
        "output": None,
        "cache_read": None,
        "cache_write": None,
        "cost": None,
        "turns": None,
    }


def _has_usage_number(usage: dict[str, Any]) -> bool:
    return any(_opt_float(usage.get(key)) is not None for key in _USAGE_NUMBERS)


def _cost_value(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("amount", "total", "usd"):
            found = _opt_float(value.get(key))
            if found is not None:
                return found
        return None
    return _opt_float(value)


def _opt_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
