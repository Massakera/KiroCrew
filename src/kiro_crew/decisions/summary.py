"""Folding the decision log into a handful of numbers.

The log (:mod:`kiro_crew.decisions.log`) is the writer; this is the only reader.
It answers one question -- "over the last N days, what did the seam do and what
did people think of it" -- as a small JSON-safe mapping the chat strip's tooltip
can render without a second call.

Tolerant by contract, not by accident
-------------------------------------
Every input here is a line in a file the process may have been killed in the
middle of writing, that a day-rotation boundary may have split, that an operator
may have edited, and that an OLDER build of this code wrote with fewer fields. So
a line that is not JSON, a row that is not an object, and a field of the wrong
type are all SKIPPED -- counted as unreadable, never raised. A summary that
refuses to load because one byte is wrong is a summary nobody can use, and the
unreadable count is what keeps that skipping visible instead of silent.

Bounded by days AND by lines
----------------------------
The window is a whole number of UTC day-files, resolved through
:func:`kiro_crew.decisions.log.log_path`, so this reader never lists a directory
and never opens a name the writer did not write. Each file is capped at
``MAX_FILE_BYTES`` by the writer, and this reader caps the number of rows it will
fold besides, so an operator-grown file cannot turn a tooltip into an unbounded
read.

What "agree" and "p" mean here
-----------------------------
Both are read from the row, never inferred. ``agree`` is counted only on rows that
carry it as a real boolean, and ``agree_rate`` is a fraction OF THOSE rows -- so a
window whose rows predate the field reports ``agree_rate: null`` rather than a
confident zero. ``p`` is taken from a top-level ``p`` when the row has one, else
from the single answer's ``p`` in the gate's own row shape; a row with neither
contributes nothing to the mean. This is a report, not a score: it says what the
rows say.

This module performs blocking file reads, so every caller reaches
:func:`summarize` off the event loop.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from kiro_crew.decisions.log import log_path

logger = logging.getLogger(__name__)

#: Longest window this reader will fold, in days. The writer deletes day-files
#: past ``log.RETENTION_DAYS``, so a longer request cannot find more rows -- it
#: can only open more missing paths.
MAX_SINCE_DAYS = 14

#: The window used when a caller names none, or names one this module refuses.
DEFAULT_SINCE_DAYS = 7

#: Most rows folded in one call, across the whole window. A day-file is capped at
#: 8 MiB of a few-hundred-byte rows, so this is reached only by a file an operator
#: grew by hand -- and a tooltip is not worth an unbounded read either way.
MAX_ROWS = 200_000


def parse_since(raw: object) -> int:
    """Days from a ``since`` parameter: ``"7d"``, ``"7"``, ``7``, or a default.

    Deliberately lenient on SPELLING and strict on RANGE. The value arrives in a
    query string that a tooltip built, so ``7d`` and ``7`` are the same request;
    but anything that does not resolve to a whole number of days in
    ``1..MAX_SINCE_DAYS`` reads as :data:`DEFAULT_SINCE_DAYS`, so a typo narrows
    the report to the documented window instead of widening it or failing it.
    """
    if isinstance(raw, bool):
        return DEFAULT_SINCE_DAYS
    if isinstance(raw, int):
        days = raw
    elif isinstance(raw, str):
        text = raw.strip().lower().removesuffix("d").strip()
        try:
            days = int(text)
        except ValueError:
            return DEFAULT_SINCE_DAYS
    else:
        return DEFAULT_SINCE_DAYS
    if days < 1 or days > MAX_SINCE_DAYS:
        return DEFAULT_SINCE_DAYS
    return days


def _numeric(value: object) -> float | None:
    """*value* as a float when it is a real number, else ``None``.

    ``bool`` is refused explicitly: it is an ``int`` in Python, so a row carrying
    ``"p": true`` would otherwise contribute ``1.0`` to a mean of probabilities.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _row_p(row: dict[str, Any]) -> float | None:
    """The probability this row reports, from either shape, or ``None``.

    A top-level ``p`` wins when present. Otherwise the gate's own row shape is
    read: ``answers`` is ``{id: {value, p, confidence}}``, and a single answer's
    ``p`` is that row's probability. A row with several answers has no single
    ``p`` and contributes none, rather than having one picked for it.
    """
    direct = _numeric(row.get("p"))
    if direct is not None:
        return direct
    answers = row.get("answers")
    if not isinstance(answers, dict) or len(answers) != 1:
        return None
    only = next(iter(answers.values()))
    if not isinstance(only, dict):
        return None
    return _numeric(only.get("p"))


def _iter_rows(days: int) -> Iterator[dict[str, Any] | None]:
    """Yield parsed rows from the last *days* UTC day-files, newest day last.

    Skips a missing file (a day with no decisions writes none) and an unreadable
    line, yielding ``None`` for the latter so the caller can count it. Reads each
    file whole: the writer caps it at 8 MiB, which is smaller than the JSON this
    would otherwise stream through, and one read keeps the file open for the
    shortest possible time.
    """
    today = datetime.now(timezone.utc).date()
    for back in range(days - 1, -1, -1):
        path = log_path(today - timedelta(days=back))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.debug("decisions: summary skipped %s (%s)", path.name, type(exc).__name__)
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                yield None
                continue
            yield row if isinstance(row, dict) else None


def summarize(days: int = DEFAULT_SINCE_DAYS) -> dict[str, Any]:
    """Fold the last *days* day-files into the report the tooltip renders.

    The returned mapping is JSON-safe and every key is always present, because a
    tooltip that has to branch on a missing key is a tooltip that renders
    differently on a quiet install than on a busy one:

    ``since_days``   the window actually folded, after :func:`parse_since`.
    ``rows``         decision rows in the window (feedback rows are not decisions).
    ``errors``       decision rows carrying a non-null ``error`` category.
    ``scrubbed``     decision rows the request scrub refused.
    ``agree``        decision rows whose ``agree`` is exactly ``True``.
    ``disagree``     decision rows whose ``agree`` is exactly ``False``.
    ``agree_rate``   ``agree / (agree + disagree)``, or ``None`` when neither.
    ``mean_p``       mean of the probabilities rows report, or ``None``.
    ``tokens_saved`` sum of ``tokens_saved`` over rows that report it.
    ``feedback``     ``{"right": {jev, baseline, total}, "wrong": {...}}``.
    ``cleared``      feedback rows whose verdict is ``null``.
    ``unreadable``   lines skipped as unparseable, so the skipping is visible.

    ``feedback`` is keyed by VERDICT and then by side, plus a ``total`` per
    verdict, because the question it answers is "how often was each side judged
    right" -- a flat count of verdicts cannot answer it, and a flat count of sides
    cannot either. A null verdict is a verdict someone TOOK BACK, so it is counted
    as ``cleared`` beside the two rather than inside either: folding it into a
    verdict would report a retraction as an opinion, and dropping it would make it
    indistinguishable from never having had one.
    """
    window = parse_since(days)
    rows = errors = scrubbed = agree = disagree = unreadable = 0
    tokens_saved = 0
    p_total = 0.0
    p_count = 0
    feedback: dict[str, dict[str, int]] = {
        "right": {"jev": 0, "baseline": 0, "total": 0},
        "wrong": {"jev": 0, "baseline": 0, "total": 0},
    }
    cleared = 0
    folded = 0
    for row in _iter_rows(window):
        folded += 1
        if folded > MAX_ROWS:
            break
        if row is None:
            unreadable += 1
            continue
        if row.get("kind") == "feedback":
            verdict = row.get("verdict")
            side = row.get("side")
            if verdict in feedback:
                bucket = feedback[verdict]
                bucket["total"] += 1
                if side in ("jev", "baseline"):
                    bucket[side] += 1
            elif verdict is None:
                cleared += 1
            continue
        # Anything else is a decision row: `kind` is absent on every row the
        # writer produced before feedback existed, so "not feedback" is the
        # test that stays true for the files already on disk.
        rows += 1
        if row.get("error") is not None:
            errors += 1
        if row.get("scrubbed") is True:
            scrubbed += 1
        agreed = row.get("agree")
        if agreed is True:
            agree += 1
        elif agreed is False:
            disagree += 1
        probability = _row_p(row)
        if probability is not None:
            p_total += probability
            p_count += 1
        saved = _numeric(row.get("tokens_saved"))
        if saved is not None:
            tokens_saved += int(saved)
    judged = agree + disagree
    return {
        "since_days": window,
        "rows": rows,
        "errors": errors,
        "scrubbed": scrubbed,
        "agree": agree,
        "disagree": disagree,
        "agree_rate": (agree / judged) if judged else None,
        "mean_p": (p_total / p_count) if p_count else None,
        "tokens_saved": tokens_saved,
        "feedback": feedback,
        "cleared": cleared,
        "unreadable": unreadable,
    }
