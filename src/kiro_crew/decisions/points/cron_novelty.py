"""``cron.novelty`` — does this cron result carry news worth delivering?

The existing test is byte equality: ``slack/gateway.py`` hashes the result and
suppresses delivery when the hash repeats. Two results that differ by a
timestamp are therefore both delivered. This hook asks the oracle whether the
new result actually says anything the previous one did not, and records the
probability beside the delivery that happened anyway.

SHADOW ONLY, and asked only on the hash-DIFFERS path — the path that delivers.
Nothing here can suppress a delivery: the answer is never read, so a "no news"
verdict costs a log line and nothing else.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

POINT = "cron.novelty"

MAX_RESULT_CHARS = 3000

#: Test seam — see :mod:`kiro_crew.decisions.points.skills_select`.
_decide: Any = None


def build_state(job_id: str, title: str, last_result: str, new_result: str) -> dict[str, Any]:
    """The state: which job, and the two result texts being compared."""
    return {
        "job_id": str(job_id or ""),
        "title": str(title or ""),
        "last_result": (last_result or "")[:MAX_RESULT_CHARS],
        "new_result": (new_result or "")[:MAX_RESULT_CHARS],
    }


def build_baseline() -> dict[str, Any]:
    """The existing logic's answer on this path is always "deliver".

    Fixed, not measured: the hook is only reached when the hashes differ, and
    that branch has no other outcome. Agreement is therefore a measure of how
    often byte inequality was also a real difference.
    """
    return {"delivered": True}


def agree(has_new_info: Any, delivered: bool = True) -> bool:
    """Whether the oracle's novelty verdict matches the delivery that happened."""
    if has_new_info is None:
        return False
    return bool(has_new_info) == bool(delivered)


async def shadow_cron_novelty(
    job_id: str,
    title: str,
    last_result: str,
    new_result: str,
    *,
    session_key: str | None = None,
) -> Any:
    """Ask the oracle whether the new result is news; never act on the answer."""
    try:
        from kiro_crew import decisions as _core
        from kiro_crew.decisions.types import Noul
    except ImportError:
        return None
    # ``decide`` is read off the package rather than imported by name: on a tree
    # where the core has not landed, ``kiro_crew.decisions`` still resolves as a
    # namespace package, so the by-name import is a type error rather than the
    # ImportError above. A core without the symbol degrades like an absent one.
    fn = _decide or getattr(_core, "decide", None)
    if fn is None:
        return None
    try:
        state = build_state(job_id, title, last_result, new_result)
        questions = [
            Noul(
                "has_new_info",
                "Does new_result contain information not in last_result worth " "delivering?",
            )
        ]
        return await fn(
            POINT,
            state,
            questions,
            session_key=session_key,
            baseline=build_baseline(),
        )
    except Exception:
        logger.debug("cron.novelty shadow failed", exc_info=True)
        return None
