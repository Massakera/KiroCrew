"""The wake judge: its mapping table, its bounds, its collectors and its tick.

No Jev key is spent anywhere here. The provider is stubbed at ``decisions.decide``,
which is the seam the point calls, so these tests exercise the real state builder,
the real mapping and the real tick without a network request.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import autonudge_judge as judge
from kiro_crew.autonudge import (
    _JUDGE_QUIET_STREAK_FLOOR_DEFAULT,
    _MAX_QUIET_STREAK,
    AutoNudgeService,
    MonitorState,
    NudgeLoop,
)
from kiro_crew.decisions.points import nudge_wake as point
from kiro_crew.decisions.types import Answer
from kiro_crew.irq import Outcome
from kiro_crew.validation import ValidationError, validate_judge_spec


def answers(
    owner: str = point.NEEDS_OWNER_QUIET,
    owner_p: float = 0.9,
    outcome: str = point.OUTCOME_PROGRESS_ONLY,
    outcome_p: float = 0.9,
    urgency: str = point.URGENCY_NONE,
) -> dict[str, Answer]:
    """One complete, in-domain answer set."""
    return {
        point.Q_NEEDS_OWNER: Answer(point.Q_NEEDS_OWNER, owner, owner_p),
        point.Q_OUTCOME: Answer(point.Q_OUTCOME, outcome, outcome_p),
        point.Q_URGENCY: Answer(point.Q_URGENCY, urgency, 0.9),
    }


class TestMappingTable:
    """Every branch of :func:`nudge_wake.map_answers`, including the failure ones."""

    def test_no_answer_is_fallback(self) -> None:
        assert point.map_answers(None).outcome is Outcome.FALLBACK

    def test_missing_question_is_fallback(self) -> None:
        partial = answers()
        del partial[point.Q_OUTCOME]
        assert point.map_answers(partial).outcome is Outcome.FALLBACK

    def test_out_of_domain_outcome_is_fallback(self) -> None:
        assert point.map_answers(answers(outcome="banana")).outcome is Outcome.FALLBACK

    def test_low_confidence_wakes(self) -> None:
        verdict = point.map_answers(answers(outcome_p=point.OUTCOME_MIN_P - 0.01))
        assert verdict.outcome is Outcome.WAKE

    def test_needs_owner_wake_at_bar(self) -> None:
        verdict = point.map_answers(
            answers(owner=point.NEEDS_OWNER_WAKE, owner_p=point.NEEDS_OWNER_MIN_P)
        )
        assert verdict.outcome is Outcome.WAKE

    def test_needs_owner_wake_below_bar_does_not_fire_on_that_rule(self) -> None:
        """The threshold is applied literally, as the design specifies.

        Only reachable from a provider that returns a non-argmax choice: with two
        options the chosen one carries at least half the mass.
        """
        verdict = point.map_answers(
            answers(owner=point.NEEDS_OWNER_WAKE, owner_p=point.NEEDS_OWNER_MIN_P - 0.01)
        )
        assert verdict.outcome is Outcome.QUIET

    @pytest.mark.parametrize("value", sorted(point.ACTION_OUTCOMES))
    def test_action_outcomes_wake_even_when_owner_says_quiet(self, value: str) -> None:
        verdict = point.map_answers(answers(owner=point.NEEDS_OWNER_QUIET, outcome=value))
        assert verdict.outcome is Outcome.WAKE

    @pytest.mark.parametrize("value", sorted(point.QUIET_OUTCOMES))
    def test_quiet_outcomes_are_the_only_quiet(self, value: str) -> None:
        assert point.map_answers(answers(outcome=value)).outcome is Outcome.QUIET

    def test_quiet_is_an_allowlist(self) -> None:
        """No outcome outside :data:`QUIET_OUTCOMES` can produce silence."""
        for value in point.OUTCOME_OPTIONS:
            if value in point.QUIET_OUTCOMES:
                continue
            for probability in (0.41, 0.5, 0.59, 0.75, 1.0):
                verdict = point.map_answers(answers(outcome=value, outcome_p=probability))
                assert verdict.outcome is not Outcome.QUIET, (value, probability)

    def test_terminal_bar_is_above_the_confidence_floor(self) -> None:
        """Ordering still matters, for the wording rather than for the outcome.

        Below :data:`OUTCOME_MIN_P` every answer takes the unsure branch, so a
        terminal bar underneath that floor would make the confident wording
        unreachable for part of its own range.
        """
        assert point.TERMINAL_MIN_P > point.OUTCOME_MIN_P

    def test_the_judge_can_never_end_a_loop(self) -> None:
        """No answer, at any confidence, may produce TERMINAL.

        The judge reads prose. If prose could end a watch, one hostile or mistaken
        comment in a watched transcript would buy permanent silence, so ending a loop
        stays with the typed probe layer. Swept across the whole answer space rather
        than asserted on the two obvious outcomes, because the guarantee is about
        every reachable answer and not about the cases someone remembered.
        """
        for outcome in point.OUTCOME_OPTIONS:
            for owner in point.NEEDS_OWNER_OPTIONS:
                for probability in (0.0, 0.39, 0.4, 0.5, 0.59, 0.6, 0.75, 1.0):
                    verdict = point.map_answers(
                        answers(
                            owner=owner, owner_p=probability, outcome=outcome, outcome_p=probability
                        )
                    )
                    assert verdict.outcome is not Outcome.TERMINAL, (outcome, owner, probability)

    @pytest.mark.parametrize("value", sorted(point.TERMINAL_OUTCOMES))
    def test_finished_and_broken_wake_at_every_confidence(self, value: str) -> None:
        for probability in (0.4, 0.59, 0.6, 0.95, 1.0):
            verdict = point.map_answers(answers(outcome=value, outcome_p=probability))
            assert verdict.outcome is Outcome.WAKE, (value, probability)
            assert value in verdict.body, "the woken session is told what the judge saw"

    def test_confidence_changes_only_the_wording(self) -> None:
        confident = point.map_answers(
            answers(outcome=point.OUTCOME_FINISHED, outcome_p=point.TERMINAL_MIN_P)
        )
        unsure = point.map_answers(
            answers(outcome=point.OUTCOME_FINISHED, outcome_p=point.TERMINAL_MIN_P - 0.01)
        )
        assert confident.outcome is unsure.outcome is Outcome.WAKE
        assert "may be" in unsure.body and "may be" not in confident.body


class TestQuestions:
    """The three questions, their domains, and where the owner's words go."""

    def test_domains(self) -> None:
        built = {q.id: q.options for q in point.build_questions()}
        assert built[point.Q_NEEDS_OWNER] == [point.NEEDS_OWNER_WAKE, point.NEEDS_OWNER_QUIET]
        assert built[point.Q_OUTCOME] == list(point.OUTCOME_OPTIONS)
        assert built[point.Q_URGENCY] == list(point.URGENCY_OPTIONS)

    def test_criteria_ride_in_the_prompt_labelled_by_option(self) -> None:
        questions = point.build_questions("a line starts with RULING", "workers say WORKING")
        prompt = questions[0].prompt
        assert "RULING" in prompt and "WORKING" in prompt
        assert point.NEEDS_OWNER_WAKE in prompt and point.NEEDS_OWNER_QUIET in prompt

    def test_criteria_are_clipped(self) -> None:
        questions = point.build_questions("w" * 5_000, "q" * 5_000)
        assert len(questions[0].prompt) < 2 * point.MAX_CRITERION_CHARS + 500

    def test_evidence_never_enters_a_question(self) -> None:
        """Evidence is data and lives in ``state``; a question is an instruction."""
        questions = point.build_questions("wake on RULING", "quiet on WORKING")
        rendered = " ".join(q.prompt + " ".join(q.options) for q in questions)
        assert "RULING" in rendered  # the owner's criterion, which does belong
        assert "leaked-evidence-marker" not in rendered


class TestStateBounds:
    """The request's ceiling, the per-item clip, and what the scrub drops."""

    def test_state_fits_the_ceiling_and_drops_oldest_first(self) -> None:
        evidence = [
            {
                "source": f"session:chat-{i}",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": float(i),
                "text": "x" * 900,
            }
            for i in range(40)
        ]
        trace: dict[str, Any] = {}
        state = point.build_state("watch the workers", evidence=evidence, trace=trace)
        assert trace["state_chars"] <= point.MAX_STATE_CHARS
        ages = [row["age_s"] for row in state["since_last_tick"]]
        assert ages == sorted(ages), "newest first"
        assert ages and max(ages) < 39.0, "the oldest items were the ones dropped"
        assert trace["dropped"] > 0

    def test_per_item_clip(self) -> None:
        state = point.build_state(
            "w",
            evidence=[
                {
                    "source": "s",
                    "kind": point.KIND_TRANSCRIPT_TAIL,
                    "age_s": 1.0,
                    "text": "y" * 9_000,
                }
            ],
        )
        assert len(state["since_last_tick"][0]["text"]) == point.MAX_ITEM_CHARS

    def test_instruction_is_clipped(self) -> None:
        state = point.build_state("i" * 9_000)
        assert len(state["loop"]["instruction"]) == point.MAX_INSTRUCTION_CHARS

    def test_last_verdict_is_carried(self) -> None:
        state = point.build_state(
            "w",
            evidence=[
                {
                    "source": "s",
                    "kind": point.KIND_PR_CHECKS,
                    "age_s": 1.0,
                    "text": "checks pending",
                }
            ],
            last_verdict={"outcome": "quiet", "evidence_items": 2},
        )
        assert state["last_verdict"]["outcome"] == "quiet"

    def test_scrub_drops_an_item_carrying_a_credential(self) -> None:
        assert point.evidence_item("pr:x#1", point.KIND_PR_CHECKS, 1.0, "AKIA" + "A" * 16) is None

    def test_unknown_kind_is_dropped(self) -> None:
        assert point.evidence_item("s", "not-a-kind", 1.0, "hello") is None

    def test_scrub_drop_everything_leaves_no_evidence(self) -> None:
        screened, dropped = point.screen_evidence(
            [
                {
                    "source": "s",
                    "kind": point.KIND_PR_CHECKS,
                    "age_s": 1.0,
                    "text": "AKIA" + "B" * 16,
                }
            ]
        )
        assert screened == [] and dropped == 1

    def test_the_cap_retains_the_newest_rows_across_targets(self) -> None:
        """The cap lands after the sort, so a later target keeps its newest rows.

        The collector groups rows by target and each group is chronological
        within itself, so a 2-target input is not globally newest-first. A cap
        taken off the front of that list would keep every stale row of the first
        target and shed the freshest rows of the second.
        """
        stale = [
            {
                "source": "chat-1",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": float(age),
                "text": f"stale {age}",
            }
            for age in range(300, 270, -1)
        ]
        fresh = [
            {
                "source": "chat-2",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": float(age),
                "text": f"fresh {age}",
            }
            for age in range(30, 0, -1)
        ]
        screened, dropped = point.screen_evidence([*stale, *fresh])
        assert len(screened) == point.MAX_EVIDENCE_ITEMS
        assert screened[0]["text"] == "fresh 1"
        texts = {row["text"] for row in screened}
        assert {f"fresh {age}" for age in range(1, 31)} <= texts
        assert dropped == len(stale) + len(fresh) - point.MAX_EVIDENCE_ITEMS

    def test_rows_the_cap_sheds_are_counted(self) -> None:
        items = [
            {
                "source": "chat-1",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": float(age),
                "text": f"row {age}",
            }
            for age in range(point.MAX_EVIDENCE_ITEMS + 5)
        ]
        screened, dropped = point.screen_evidence(items)
        assert len(screened) == point.MAX_EVIDENCE_ITEMS
        assert dropped == 5


class TestEmptyDelta:
    """An empty delta is QUIET only when every target was READ and had nothing new.

    The three causes of an empty delta are not one fact. A target that could not be
    read leaves the tick blind, and evidence the scrub shed leaves it unable to send
    what it found; answering either as calm would suppress a turn nobody established
    was unwanted. Each branch is told apart by its body, not only by its outcome, so
    a test cannot pass by reaching the right verdict for the wrong reason.
    """

    def test_every_target_read_and_nothing_new_is_quiet(self) -> None:
        verdict = asyncio.run(point.judge_tick("watch", evidence=[], dropped=0))
        assert verdict.outcome is Outcome.QUIET
        assert "no new evidence" in verdict.body

    def test_a_dropped_target_is_fallback_not_quiet(self) -> None:
        verdict = asyncio.run(point.judge_tick("watch", evidence=[], dropped=1))
        assert verdict.outcome is Outcome.FALLBACK
        assert "could not read every target" in verdict.body

    def test_a_quiet_empty_delta_reports_zero_items_and_no_answers(self) -> None:
        """The notice and the verdict record are built from the trace on this path too.

        A quiet tick writes one transcript row, so the count it reports has to come
        from the same out-parameter every other path fills; a caller left reading the
        list it passed in would render a judged-on count for a tick with nothing in it.
        """
        trace: dict[str, Any] = {}
        verdict = asyncio.run(point.judge_tick("watch", evidence=[], dropped=0, trace=trace))
        assert verdict.outcome is Outcome.QUIET
        assert trace["evidence_items"] == 0
        assert trace["answers"] is None

    def test_the_tick_hands_the_point_its_dropped_count(self) -> None:
        """The caller holds the count and the point decides on it.

        A collector reporting an unreadable target and a caller that does not forward
        it produce the one indistinguishable failure on this path: the point sees an
        empty delta with nothing dropped and rules the tick calm, so a blind tick is
        suppressed and reads in the transcript exactly like a quiet one.
        """
        source = inspect.getsource(AutoNudgeService._judge_tick_is_quiet)
        assert "dropped=dropped" in source, "the collector's count reaches the point"


class TestJudgeTickFallsOpen:
    """Every failure path reaches FALLBACK, which fires the loop."""

    def test_a_fallback_publishes_the_screened_count(self) -> None:
        """A scrubbed-away tick reports zero items, not the input's length.

        The caller renders the notice and the verdict record from this count, so
        an unfilled trace would leave it reading the list it passed in -- which
        holds the rows the scrub rejected.
        """
        trace: dict[str, Any] = {}
        verdict = asyncio.run(
            point.judge_tick(
                "watch",
                evidence=[
                    {
                        "source": "s",
                        "kind": point.KIND_PR_CHECKS,
                        "age_s": 1.0,
                        "text": "AKIA" + "C" * 16,
                    }
                ],
                trace=trace,
            )
        )
        assert verdict.outcome is Outcome.FALLBACK
        assert "no evidence it could send" in verdict.body
        assert trace["evidence_items"] == 0
        assert trace["answers"] is None

    def test_refused_decision_is_fallback(self) -> None:
        async def refuse(*args: Any, **kwargs: Any) -> None:
            return None

        evidence = [
            {"source": "s", "kind": point.KIND_PR_CHECKS, "age_s": 1.0, "text": "checks pending"}
        ]
        with patch("kiro_crew.decisions.decide", refuse):
            verdict = asyncio.run(point.judge_tick("watch", evidence=evidence))
        assert verdict.outcome is Outcome.FALLBACK

    def test_raising_provider_is_fallback(self) -> None:
        async def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("provider exploded")

        evidence = [
            {"source": "s", "kind": point.KIND_PR_CHECKS, "age_s": 1.0, "text": "checks pending"}
        ]
        with patch("kiro_crew.decisions.decide", boom):
            verdict = asyncio.run(point.judge_tick("watch", evidence=evidence))
        assert verdict.outcome is Outcome.FALLBACK


class TestCollectors:
    """What the collectors read, what they refuse, and what they remember."""

    def test_assistant_rows_only(self) -> None:
        rows = [
            {"role": "assistant", "content": "RULING: need a call", "ts": 100.0},
            {"role": "tool", "content": "a file nobody asked to send", "ts": 101.0},
            {"role": "user", "content": "typed by the owner", "ts": 102.0},
        ]
        items = judge.session_evidence(rows, "chat-2-2", now_ts=110.0)
        assert [i["text"] for i in items] == ["RULING: need a call"]

    def test_a_pr_observation_renders_one_row_from_its_typed_keys(self) -> None:
        """The canonical keys ARE the producer: no separate summary field exists."""
        items = judge.pr_evidence(
            {
                "state": "open",
                "draft": False,
                "mergeability": "conflicting",
                "review_decision": "changes_requested",
                "blocking_review": "changes_requested",
                "unresolved_review_threads": 3,
                "checks": {
                    "failed": ["lint", "tests-2"],
                    "pending": ["e2e"],
                    "passed": ["a", "b", "c"],
                    "unknown": [],
                },
                "checks_complete": True,
                "review_threads_complete": True,
                "head_revision": "2c0adbc00da0e1878ce44a0eb47a6f02293506ba",
                "observed_at": 100.0,
            },
            "owner/name#1",
            now_ts=110.0,
        )
        assert len(items) == 1
        row = items[0]
        assert row["kind"] == point.KIND_PR_CHECKS
        assert row["source"] == "pr:owner/name#1"
        assert row["age_s"] == 10.0, "the observation's own time ages the row"
        text = row["text"]
        for fragment in (
            "state=open",
            "mergeability=conflicting",
            "review_decision=changes_requested",
            "unresolved_review_threads=3",
            "draft=no",
        ):
            assert fragment in text
        assert "checks failed 2 (lint, tests-2)" in text, "a red bucket is named, not just counted"
        assert "checks pending 1 (e2e)" in text
        assert "checks passed 3" in text, "a green bucket is counted only"
        assert "head=2c0adbc00da0" in text

    def test_an_incomplete_reading_says_so(self) -> None:
        """A criterion about red checks means something else on a partial list."""
        items = judge.pr_evidence(
            {"state": "open", "checks_complete": False, "review_threads_complete": False},
            "owner/name#1",
            now_ts=110.0,
        )
        assert "checks_complete=no" in items[0]["text"]
        assert "review_threads_complete=no" in items[0]["text"]

    def test_the_comment_fingerprint_is_carried_and_no_body_is(self) -> None:
        items = judge.pr_evidence(
            {"state": "open", "pr_comment_body_digest": "a" * 64},
            "owner/name#1",
            now_ts=110.0,
        )
        text = items[0]["text"]
        assert "pr_comments_fingerprint=aaaaaaaaaaaa" in text
        assert len(text) < 200, "a fingerprint is 12 chars of hex, never a body"

    def test_a_long_red_bucket_reports_a_remainder_instead_of_every_name(self) -> None:
        """The canonical object holds up to 100 identities; the item budget is 1000."""
        items = judge.pr_evidence(
            {"state": "open", "checks": {"failed": [f"lane-{n}" for n in range(40)]}},
            "owner/name#1",
            now_ts=110.0,
        )
        text = items[0]["text"]
        assert "checks failed 40 (" in text
        assert "+32 more" in text
        assert "lane-39" not in text
        assert len(text) <= point.MAX_ITEM_CHARS

    def test_an_observation_with_no_usable_key_yields_nothing(self) -> None:
        """Rather than an empty row: a blank item would read as a calm reading."""
        assert judge.pr_evidence({}, "owner/name#1", now_ts=110.0) == []
        assert judge.pr_evidence(None, "owner/name#1", now_ts=110.0) == []

    def test_a_mistyped_key_is_skipped_rather_than_coerced(self) -> None:
        items = judge.pr_evidence(
            {"state": "open", "unresolved_review_threads": "three", "draft": "no"},
            "owner/name#1",
            now_ts=110.0,
        )
        text = items[0]["text"]
        assert "state=open" in text
        assert "unresolved_review_threads" not in text
        assert "draft" not in text

    def test_the_real_canonical_object_is_what_the_renderer_reads(self) -> None:
        """Built by the monitor's own projection, not by hand.

        This is the assertion the feature lacked: a hand-written observation can
        agree with the reader while the actual producer emits different keys, which
        is how a collector reading absent fields passed every test it had.
        """
        from kiro_crew.monitoring.pull_request import (
            PullRequestCheck,
            PullRequestFacts,
            canonical_pull_request_facts,
        )

        canonical = canonical_pull_request_facts(
            PullRequestFacts(
                kind="github_pull_request",
                target="owner/name#1",
                state="open",
                draft=False,
                head_revision="2c0adbc00da0e1878ce44a0eb47a6f02293506ba",
                mergeability="blocked",
                review_decision="changes_requested",
                checks=(
                    PullRequestCheck(identity="lint", state="failed"),
                    PullRequestCheck(identity="tests-2", state="passed"),
                ),
                unresolved_review_threads=2,
                review_threads_complete=True,
                pr_comment_body_digest="b" * 64,
            )
        )
        items = judge.pr_evidence(canonical, "owner/name#1", now_ts=110.0)
        assert items, "the canonical projection must yield evidence, not an empty list"
        text = items[0]["text"]
        assert "state=open" in text
        assert "checks failed 1 (lint)" in text
        assert "review_decision=changes_requested" in text
        assert "pr_comments_fingerprint=bbbbbbbbbbbb" in text

    def test_a_pr_target_reaches_a_verdict_instead_of_falling_back(self) -> None:
        """Evidence with no producer made every pull-request tick FALLBACK, which fires.

        Driven through the same screen and state builder the tick uses, so this
        fails if the rendered row is dropped by the scrub or the kind check.
        """
        from kiro_crew.monitoring.pull_request import (
            PullRequestCheck,
            PullRequestFacts,
            canonical_pull_request_facts,
        )

        canonical = canonical_pull_request_facts(
            PullRequestFacts(
                kind="github_pull_request",
                target="owner/name#1",
                state="open",
                draft=False,
                head_revision="",
                mergeability="blocked",
                review_decision="review_required",
                checks=(PullRequestCheck(identity="lint", state="failed"),),
                unresolved_review_threads=0,
                review_threads_complete=True,
            )
        )
        evidence = judge.pr_evidence(canonical, "owner/name#1", now_ts=110.0)
        screened, dropped = point.screen_evidence(evidence)
        assert screened and dropped == 0, "the rendered row must survive the scrub"
        state = point.build_state("watch", wake_when="a check goes red", evidence=screened)
        assert state["since_last_tick"], "an empty state is what produced FALLBACK"

    def test_refused_target_is_dropped_and_its_cursor_held(self) -> None:
        async def refuse(target: str, since: int) -> tuple[list[dict], int]:
            raise PermissionError("not the creator")

        cursors = {"chat-2-2": 7}
        items, dropped = asyncio.run(
            judge.collect_evidence(["chat-2-2"], read_session=refuse, cursors=cursors)
        )
        assert items == [] and dropped == 1
        assert cursors == {"chat-2-2": 7}, "a refusal must not advance the cursor"

    def test_an_unusable_cursor_is_cleared_so_the_next_tick_reads_again(self) -> None:
        """A rewound transcript refuses the stored cursor, which must not be kept.

        Holding it makes every later tick re-send the same unusable value and be
        refused again, so the judge stops reading that target for as long as the
        loop is armed.
        """

        class Refusal(Exception):
            code = "cursor_unavailable"

        async def refuse(target: str, since: int) -> tuple[list[dict], int]:
            raise Refusal("cursor 7 is past the end of this transcript")

        cursors = {"chat-2-2": 7}
        items, dropped = asyncio.run(
            judge.collect_evidence(["chat-2-2"], read_session=refuse, cursors=cursors)
        )
        assert items == [] and dropped == 1
        assert cursors == {}, "an unusable cursor is cleared, which makes the next read a tail"

    def test_cursor_advances_on_a_successful_read(self) -> None:
        async def read(target: str, since: int) -> tuple[list[dict], int]:
            return [{"role": "assistant", "content": "progress", "ts": 1.0}], 12

        cursors: dict[str, int] = {}
        asyncio.run(judge.collect_evidence(["chat-2-2"], read_session=read, cursors=cursors))
        assert cursors == {"chat-2-2": 12}

    def test_absent_reader_drops_rather_than_raising(self) -> None:
        items, dropped = asyncio.run(judge.collect_evidence(["chat-2-2"]))
        assert items == [] and dropped == 1

    def test_an_unread_pull_request_is_a_dropped_target(self) -> None:
        """``None`` from the PR reader is "not read", which must not read as "calm".

        The reader answers ``None`` for a loop carrying no monitor, which is every loop
        armed through the plain nudge endpoint, and for a brief naming a pull request
        the loop does not watch. Handing that to ``pr_evidence`` yields an empty list,
        so without the drop the tick holds no evidence and no drop -- the one shape the
        point reads as every target having been read and found quiet, which would
        suppress the turn on a subject nobody looked at.
        """

        async def unread(target: str) -> None:
            return None

        items, dropped = asyncio.run(
            judge.collect_evidence(
                ["https://github.com/o/r/pull/7"],
                read_pr=unread,
            )
        )
        assert items == [] and dropped == 1

    def test_a_read_pull_request_is_not_a_drop(self) -> None:
        """The control: a real observation contributes evidence and drops nothing."""

        async def observed(target: str) -> dict:
            return {"state": "open", "mergeable": "MERGEABLE"}

        items, dropped = asyncio.run(
            judge.collect_evidence(
                ["https://github.com/o/r/pull/7"],
                read_pr=observed,
            )
        )
        assert dropped == 0 and len(items) == 1

    def test_targets_from_the_spec_are_deduped_and_screened(self) -> None:
        targets = judge.parse_targets(
            {"targets": ["chat-2-2", "chat-2-2", "not a target at all"]}, ""
        )
        assert targets == ["chat-2-2"]

    def test_session_target_shape(self) -> None:
        assert judge.is_session_target("chat-1751-1790052364")
        assert not judge.is_session_target("../etc/passwd")
        assert not judge.is_session_target("")


class TestSchema:
    """What ``monitor_start`` / ``monitor_update`` accept as a brief."""

    def test_accepts_and_normalises(self) -> None:
        out = validate_judge_spec(
            {"targets": ["chat-2-2", "chat-2-2"], "wake_when": "RULING", "quiet_when": "WORKING"}
        )
        assert out["targets"] == ["chat-2-2"]
        assert out["wake_when"] == "RULING"

    def test_absent_and_empty_are_both_legal(self) -> None:
        assert validate_judge_spec(None) == {}
        assert validate_judge_spec({}) == {}

    @pytest.mark.parametrize(
        "bad",
        [
            "a string",
            5,
            {"wake_whn": "a typo nobody would notice"},
            {"targets": "chat-2-2"},
            {"targets": [1]},
            {"targets": ["chat-x"] * 9},
            {"targets": ["c" * 500]},
            {"wake_when": "x" * 501},
            {"quiet_when": 5},
        ],
    )
    def test_refuses(self, bad: Any) -> None:
        with pytest.raises(ValidationError):
            validate_judge_spec(bad)


def _service() -> AutoNudgeService:
    """A service with no disk and no evidence reader wired, for tick tests."""
    svc = AutoNudgeService.__new__(AutoNudgeService)
    svc._collect_judge_evidence = None
    svc._emit_judge_notice = None

    # Built without ``__init__``, so it holds no service lock, no store path and no
    # in-flight task set: the real durable write would fail on all three. The judge
    # path AWAITS its write as a SHIELDED supervised task and fires when it does not
    # land, so leaving these out would make every quiet tick here report a fire and
    # hide what the test is actually about.
    svc._inflight_adds = set()

    async def _no_disk() -> None:
        return None

    svc._persist_locked = _no_disk  # type: ignore[method-assign]
    return svc


def _loop(brief: dict | None = None) -> NudgeLoop:
    loop = NudgeLoop(id="l1", slot_key="chat-1-1", message="watch chat-2-2")
    loop.judge = dict(brief or {})
    return loop


class TestTickLeavesEverythingAloneWhenItShould:
    """``None`` means the judge had no say, so the tick behaves exactly as today."""

    def test_no_brief(self) -> None:
        assert asyncio.run(_service()._judge_tick_is_quiet(_loop())) is None

    def test_brief_but_no_evidence_reader(self) -> None:
        assert asyncio.run(_service()._judge_tick_is_quiet(_loop({"wake_when": "x"}))) is None

    def test_the_seam_refusing_stores_the_brief_and_ignores_it(self) -> None:
        """Refused means the loop is a plain timer, and the brief waits for later.

        One test, because the service does not tell the two refusals apart: a missing
        ``nudge_evidence`` scope and a lane with no runner installed both arrive as
        ``is_enabled`` answering False, which is the seam resolving the lane and its
        arming together. Keeping the brief is what makes granting the scope later arm
        every loop already carrying one, with no re-arming.
        """
        svc = _service()

        async def never(loop: NudgeLoop) -> tuple[list[dict], int]:
            raise AssertionError("collected evidence while the seam refused")

        svc._collect_judge_evidence = never
        loop = _loop({"wake_when": "x"})
        with patch("kiro_crew.decisions.is_enabled", lambda *a, **k: False):
            assert asyncio.run(svc._judge_tick_is_quiet(loop)) is None
        assert loop.judge == {"wake_when": "x"}, "the brief is kept for when the scope arrives"

    def test_the_tick_asks_the_seam_and_resolves_no_lane_of_its_own(self) -> None:
        """One rule, and it is the gate's.

        The gate arms Jev only on consent AND this point's scope. A resolver here that
        arms it on endpoint consent alone picks the Jev lane on a machine that consented
        without granting the scope; the gate then refuses that lane and the judge is
        skipped, when the gate itself picks the small-model lane, which needs neither.
        So this asserts against the module's SYMBOLS rather than a call: the fault is a
        second rule existing at all, and a call-level test passes either way.
        """
        from kiro_crew import autonudge as _autonudge

        for gone in (
            "_judge_lane",
            "_judge_provider",
            "_judge_llm_lane_available",
            "_JUDGE_PROVIDERS",
            "_JUDGE_LLM_MODULE",
        ):
            assert not hasattr(_autonudge, gone), f"{gone} re-spells the gate's lane rule"
            assert not hasattr(AutoNudgeService, gone), f"{gone} re-spells the gate's lane rule"
        source = inspect.getsource(AutoNudgeService._judge_tick_is_quiet)
        assert "core_decisions.is_enabled" in source, "the tick reads the seam's own answer"


class TestTickVerdicts:
    """QUIET spends no turn, everything else spends one, and the floor bounds QUIET."""

    @staticmethod
    def _armed(verdict_answers: dict[str, Answer] | None) -> tuple[AutoNudgeService, NudgeLoop]:
        svc = _service()

        async def collect(loop: NudgeLoop) -> tuple[list[dict], int]:
            return [
                {
                    "source": "session:chat-2-2",
                    "kind": point.KIND_TRANSCRIPT_TAIL,
                    "age_s": 1.0,
                    "text": "WORKING: still building",
                }
            ], 0

        svc._collect_judge_evidence = collect
        svc._persist_soon = lambda: None  # type: ignore[method-assign]
        return svc, _loop({"wake_when": "RULING", "quiet_when": "WORKING"})

    def _run(self, svc: AutoNudgeService, loop: NudgeLoop, ans: Any) -> Any:
        async def decide(*args: Any, **kwargs: Any) -> Any:
            return ans

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.decide", decide),
        ):
            return asyncio.run(svc._judge_tick_is_quiet(loop))

    def test_quiet_spends_no_turn(self) -> None:
        svc, loop = self._armed(None)
        assert self._run(svc, loop, answers()) is True
        assert loop.judge_quiet_streak == 1
        assert loop.judge_last_verdict["outcome"] == "quiet"

    def test_a_brief_replaced_mid_tick_is_discarded_and_fires(self) -> None:
        """The reads and the decision happen outside the service lock.

        An update that replaces the brief clears the streak, the cursors and the verdict
        record, because all three are facts about the brief that is gone. A tick already
        in flight would otherwise commit its own copies of them afterwards, reinstating
        state nobody armed and able to hold the loop quiet on a withdrawn criterion.
        """
        svc, loop = self._armed(None)
        loop.judge_cursors = {"chat-2-2": 5}

        async def replace_then_read(loop_: NudgeLoop) -> tuple[list[dict], int]:
            loop_.judge = {"wake_when": "something else entirely"}
            return [
                {
                    "source": "session:chat-2-2",
                    "kind": point.KIND_TRANSCRIPT_TAIL,
                    "age_s": 1.0,
                    "text": "WORKING: still building",
                }
            ], 0

        svc._collect_judge_evidence = replace_then_read  # type: ignore[method-assign]
        assert self._run(svc, loop, answers()) is False, "a stale verdict fires"
        assert loop.judge_quiet_streak == 0, "the streak is not credited to the new brief"
        assert loop.judge_cursors == {}, "the cursors go back to what the update installed"

    def test_an_unchanged_brief_still_commits(self) -> None:
        """The control for the test above: the discard is the change, not the re-read."""
        svc, loop = self._armed(None)
        assert self._run(svc, loop, answers()) is True
        assert loop.judge_quiet_streak == 1

    @staticmethod
    def _reads(svc: AutoNudgeService, dropped: int) -> None:
        """Make the collector return an empty delta, with *dropped* targets unread."""

        async def read(loop: NudgeLoop) -> tuple[list[dict], int]:
            return [], dropped

        svc._collect_judge_evidence = read  # type: ignore[method-assign]

    def test_a_calm_read_spends_no_turn_and_counts_toward_the_floor(self) -> None:
        """Nothing new on any target is an answer, and the answer costs no turn.

        The provider returns ``None`` here, which is the shape that would FALL BACK if
        the tick reached it -- so a QUIET verdict can only have come from the delta
        being empty with every target read, and it still lands in the streak the floor
        bounds rather than in a silence nothing ends.
        """
        svc, loop = self._armed(None)
        self._reads(svc, dropped=0)
        assert self._run(svc, loop, None) is True
        assert loop.judge_quiet_streak == 1
        assert loop.judge_last_verdict["outcome"] == "quiet"
        assert loop.judge_last_verdict["evidence_items"] == 0

    def test_an_unread_target_fires_and_resets_the_streak(self) -> None:
        """A tick that could not look is not calm, and it does not keep its credit."""
        svc, loop = self._armed(None)
        loop.judge_quiet_streak = 2
        self._reads(svc, dropped=1)
        assert self._run(svc, loop, None) is False
        assert loop.judge_quiet_streak == 0

    def test_a_subject_that_stays_calm_still_fires_at_the_floor(self) -> None:
        """The floor bounds the new quiet path as it bounds a provider-answered one.

        A subject with genuinely nothing to say would otherwise hold its loop silent
        for as long as it stayed silent, which is the one outcome the floor exists to
        make impossible.
        """
        svc, loop = self._armed(None)
        self._reads(svc, dropped=0)
        loop.judge_quiet_streak = svc._judge_quiet_streak_floor() - 1
        assert self._run(svc, loop, None) is False, "the floor delivers a turn"
        assert loop.judge_quiet_streak == 0

    def test_a_quiet_whose_write_does_not_land_fires_instead(self) -> None:
        """Suppressing a turn is the irreversible direction, so it needs durable state.

        The cursors, the streak and the verdict record are published in memory before
        the write. If the write is lost, a restart re-reads the rows this tick
        consumed and recounts a streak it had already spent -- so the tick must not
        also claim the turn was not owed.
        """
        svc, loop = self._armed(None)

        async def refuse() -> None:
            raise OSError("disk is gone")

        svc._persist_locked = refuse  # type: ignore[method-assign]
        assert self._run(svc, loop, answers()) is False
        assert loop.judge_last_verdict["outcome"] == "quiet", "the verdict itself stands"

    def test_a_quiet_whose_write_lands_still_spends_no_turn(self) -> None:
        """The control for the test above: the fire is the write's failure, not the await."""
        svc, loop = self._armed(None)
        calls: list[int] = []

        async def landed() -> None:
            calls.append(1)

        svc._persist_locked = landed  # type: ignore[method-assign]
        assert self._run(svc, loop, answers()) is True
        assert calls, "the quiet path awaits a durable write rather than scheduling one"

    def test_wake_spends_a_turn_and_clears_the_streak(self) -> None:
        svc, loop = self._armed(None)
        loop.judge_quiet_streak = 4
        assert self._run(svc, loop, answers(outcome=point.OUTCOME_NEEDS_ACTION)) is False
        assert loop.judge_quiet_streak == 0

    def test_finished_wakes_and_leaves_the_loop_armed(self) -> None:
        """The judge may say the work is over; only a typed probe may END a watch.

        A judge reading prose that could stop a loop would let one hostile or
        mistaken comment in a watched transcript buy permanent silence.
        """
        svc, loop = self._armed(None)
        finished = answers(outcome=point.OUTCOME_FINISHED, outcome_p=0.95)
        assert self._run(svc, loop, finished) is False, "the session is woken"
        assert loop.judge_last_verdict["outcome"] == "wake", "never terminal"

    def test_a_repeated_finished_keeps_waking(self) -> None:
        """No suppression, because nothing recorded a delivery to suppress against."""
        svc, loop = self._armed(None)
        finished = answers(outcome=point.OUTCOME_BROKEN, outcome_p=0.99)
        for _ in range(4):
            assert self._run(svc, loop, finished) is False

    def test_the_verdict_rides_on_the_fired_turn(self) -> None:
        svc, loop, sink = TestTranscriptNotice._armed_with_sink()
        TestTranscriptNotice()._run(
            svc, loop, answers(outcome=point.OUTCOME_FINISHED, outcome_p=0.95)
        )
        assert len(sink) == 1 and point.OUTCOME_FINISHED in sink[0]

    def test_refused_provider_spends_a_turn(self) -> None:
        svc, loop = self._armed(None)
        assert self._run(svc, loop, None) is False

    def test_streak_floor_fires_and_resets(self) -> None:
        svc, loop = self._armed(None)
        floor = svc._judge_quiet_streak_floor()
        loop.judge_quiet_streak = floor - 1
        assert self._run(svc, loop, answers()) is False, "the floor delivers a turn"
        assert loop.judge_quiet_streak == 0

    def test_collector_failure_spends_a_turn(self) -> None:
        svc, loop = self._armed(None)

        async def boom(loop_: NudgeLoop) -> tuple[list[dict], int]:
            raise RuntimeError("slot read exploded")

        svc._collect_judge_evidence = boom
        assert self._run(svc, loop, answers()) is False


class TestTranscriptNotice:
    """A verdict that spends no turn still leaves one line on the session."""

    @staticmethod
    def _armed_with_sink() -> tuple[AutoNudgeService, NudgeLoop, list[str]]:
        svc = _service()
        sink: list[str] = []

        async def collect(loop: NudgeLoop) -> tuple[list[dict], int]:
            return [
                {
                    "source": "session:chat-2-2",
                    "kind": point.KIND_TRANSCRIPT_TAIL,
                    "age_s": 1.0,
                    "text": "WORKING: still building",
                }
            ], 0

        async def emit(loop: NudgeLoop, line: str) -> None:
            sink.append(line)

        svc._collect_judge_evidence = collect
        svc._emit_judge_notice = emit
        svc._persist_soon = lambda: None  # type: ignore[method-assign]
        return svc, _loop({"wake_when": "RULING", "quiet_when": "WORKING"}), sink

    def _run(self, svc: AutoNudgeService, loop: NudgeLoop, ans: Any) -> Any:
        async def decide(*args: Any, **kwargs: Any) -> Any:
            return ans

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.decide", decide),
        ):
            return asyncio.run(svc._judge_tick_is_quiet(loop))

    def test_quiet_verdict_emits_one_line(self) -> None:
        svc, loop, sink = self._armed_with_sink()
        assert self._run(svc, loop, answers()) is True, "quiet spends no turn"
        assert len(sink) == 1
        line = sink[0]
        assert "quiet" in line
        assert point.Q_NEEDS_OWNER in line and point.Q_OUTCOME in line
        assert "0.9" in line, "the probabilities are what tell a confident quiet apart"

    def test_wake_verdict_also_emits(self) -> None:
        svc, loop, sink = self._armed_with_sink()
        self._run(svc, loop, answers(outcome=point.OUTCOME_NEEDS_ACTION))
        assert len(sink) == 1 and "wake" in sink[0]

    def test_fallback_emits_with_no_readings(self) -> None:
        svc, loop, sink = self._armed_with_sink()
        self._run(svc, loop, None)
        assert len(sink) == 1 and "fallback" in sink[0]

    def test_notice_carries_no_evidence_text(self) -> None:
        """The state stays in the request; the transcript gets the verdict."""
        svc, loop, sink = self._armed_with_sink()
        self._run(svc, loop, answers())
        assert "still building" not in sink[0]

    def test_a_broken_renderer_does_not_cost_the_verdict(self) -> None:
        svc, loop, _sink = self._armed_with_sink()

        async def boom(loop_: NudgeLoop, line: str) -> None:
            raise RuntimeError("renderer exploded")

        svc._emit_judge_notice = boom
        assert self._run(svc, loop, answers()) is True

    def test_a_notice_row_is_never_evidence(self) -> None:
        """A judge must not read its own previous notice back as new evidence.

        The collector admits assistant rows only, so the exclusion is structural
        rather than a name check against this feature's own output.
        """
        rows = [
            {"role": "notice", "content": "Wake judge - quiet - 2 evidence item(s)", "ts": 100.0},
            {"role": "assistant", "content": "RULING: need a call", "ts": 101.0},
        ]
        items = judge.session_evidence(rows, "chat-2-2", now_ts=110.0)
        assert [i["text"] for i in items] == ["RULING: need a call"]


class TestStreakFloorConfig:
    """The floor's default and its hard ceiling."""

    def test_default_equals_the_shipped_probe_floor(self) -> None:
        assert _JUDGE_QUIET_STREAK_FLOOR_DEFAULT == _MAX_QUIET_STREAK

    def test_unreadable_config_is_the_default(self) -> None:
        assert _service()._judge_quiet_streak_floor() == _JUDGE_QUIET_STREAK_FLOOR_DEFAULT


class TestUpdatePathCarriesTheBrief:
    """``monitor_update`` must MOVE the brief, not answer success and drop it.

    Each hop is asserted separately because the defect this replaces was silent: the
    tool validated the field, returned success, and no layer beneath it ever received
    the value. A test against the service alone passed the whole time.
    """

    def test_the_tool_copies_a_validated_brief_into_the_patch(self) -> None:
        from kiro_crew.mcp_tools import control

        assert 'patch["judge"] = validate_judge_spec(args["judge"])' in inspect.getsource(control)

    def test_the_reader_hands_over_the_observation_time(self) -> None:
        """``last_observed_at`` is a sibling field, not a key inside the observation.

        The collector ages its row from ``observed_at`` on the mapping it is given,
        so a reader that copies only the canonical dict makes every pull-request row
        age 0.0 -- which is silent, and is exactly what defeats the drop-oldest-first
        bound the recency sort exists to serve.
        """
        from kiro_crew.slack import gateway

        source = inspect.getsource(gateway)
        assert 'payload["observed_at"] = float(at)' in source
        assert 'getattr(monitor, "last_observed_at", 0.0)' in source

    def test_the_session_read_is_bounded_to_what_the_collector_retains(self) -> None:
        """A wider page advances the cursor across rows that are then discarded.

        ``next_since`` follows the returned window and ``session_evidence`` keeps
        only the last ``MAX_ROWS_PER_TARGET``, so an unbounded read loses the oldest
        rows of any burst larger than that and never reads them again.
        """
        from kiro_crew.slack import gateway

        source = inspect.getsource(gateway)
        assert "limit=_judge.MAX_ROWS_PER_TARGET," in source

    def test_the_reader_checks_the_subject_before_answering(self) -> None:
        """One monitor answers every target, so the subject must be verified."""
        from kiro_crew.slack import gateway

        source = inspect.getsource(gateway)
        assert "_judge.pr_observation_is_about(" in source


class TestTheReaderOnlyAnswersForItsOwnSubject:
    """A brief may name a pull request the loop does not watch.

    The loop holds ONE monitor, so an unchecked reader returns the watched
    subject's state labelled with whatever target it was asked for -- and a judge
    could then rule quiet on a different pull request's facts.
    """

    WATCHED = "https://github.com/kirodotdev/KiroCrew/pull/12787"
    OTHER = "https://github.com/kirodotdev/KiroCrew/pull/12788"

    def _about(self, target: str, **kwargs: Any) -> bool:
        return judge.pr_observation_is_about(
            target,
            monitor_kind=kwargs.pop("monitor_kind", "github_pull_request"),
            monitor_target=kwargs.pop("monitor_target", self.WATCHED),
            **kwargs,
        )

    def test_the_watched_subject_is_accepted(self) -> None:
        assert self._about(self.WATCHED) is True

    def test_a_different_pull_request_is_refused(self) -> None:
        assert self._about(self.OTHER) is False

    def test_the_same_number_on_another_repository_is_refused(self) -> None:
        assert self._about("https://github.com/other/repo/pull/12787") is False

    def test_the_same_slug_on_another_host_is_refused(self) -> None:
        """One slug on two servers is two pull requests, so the host is identity."""
        assert self._about("https://github.example.com/kirodotdev/KiroCrew/pull/12787") is False

    def test_another_spelling_of_the_watched_subject_is_accepted(self) -> None:
        """Owner and monitor are spelled by different writers, so both are inferred."""
        assert self._about("https://github.com/kirodotdev/KiroCrew/pull/12787/files") is True

    def test_an_observation_labelled_with_another_subject_is_refused(self) -> None:
        """The reading's own label disagreeing with its record is not a guess to make."""
        assert (
            self._about(
                self.WATCHED,
                observation={
                    "target": "https://github.com/other/repo/pull/1",
                    "kind": "github_pull_request",
                },
            )
            is False
        )

    def test_an_observation_of_another_kind_is_refused(self) -> None:
        assert self._about(self.WATCHED, observation={"kind": "gitlab_merge_request"}) is False

    def test_a_matching_observation_label_is_accepted(self) -> None:
        assert (
            self._about(
                self.WATCHED,
                observation={"target": self.WATCHED, "kind": "github_pull_request"},
            )
            is True
        )

    def test_a_monitor_with_no_subject_is_refused(self) -> None:
        assert self._about(self.WATCHED, monitor_target="") is False
        assert self._about(self.WATCHED, monitor_kind="") is False

    def test_an_unparseable_request_that_does_not_match_exactly_is_refused(self) -> None:
        assert self._about("not a pull request") is False

    def test_an_empty_object_survives_as_a_clear(self) -> None:
        """``{}`` is falsy, so an ``if args.get(...)`` guard would silently eat it."""
        from kiro_crew.mcp_tools import control

        source = inspect.getsource(control)
        guard = source[: source.index('patch["judge"] = validate_judge_spec')].rsplit("if ", 1)[1]
        assert "is not None" in guard, "a truthiness guard would drop the clear"

    def test_every_hop_between_the_tool_and_the_loop_names_it(self) -> None:
        from kiro_crew import autonudge_authz
        from kiro_crew.autonudge import AutoNudgeService
        from kiro_crew.dashboard import session_directive_apply

        applier = inspect.getsource(session_directive_apply)
        assert 'judge=patch.get("judge")' in applier, "this applier names every kwarg"
        assert "judge" in inspect.signature(autonudge_authz.authorize_and_update_nudge).parameters
        assert "judge=judge" in inspect.getsource(autonudge_authz.authorize_and_update_nudge)
        assert "judge" in inspect.signature(AutoNudgeService.update).parameters


class TestAgeParsing:
    """Ages decide what gets DROPPED at the cap, so the real row shape must parse.

    Transcript rows carry an ISO 8601 string (verified against a live history file),
    while a probe observation carries an epoch float. Reading only the float made
    every session row age 0.0 -- and with all ages equal, which item lost its place at
    the cap was arbitrary, so a newer actionable row could be discarded before an
    older one.
    """

    def test_an_iso_string_is_a_real_age(self) -> None:
        from datetime import datetime, timedelta, timezone

        clock = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
        row = (clock - timedelta(seconds=90)).isoformat()
        assert judge._age_from_ts(row, clock.timestamp()) == pytest.approx(90.0, abs=0.01)

    def test_a_trailing_z_and_a_naive_stamp_both_read_as_utc(self) -> None:
        clock = 1_790_000_000.0
        zulu = judge._age_from_ts("2026-09-22T07:25:47+00:00", clock)
        assert judge._age_from_ts("2026-09-22T07:25:47Z", clock) == zulu
        assert judge._age_from_ts("2026-09-22T07:25:47", clock) == zulu

    def test_an_epoch_float_still_parses(self) -> None:
        assert judge._age_from_ts(1_000.0, 1_060.0) == pytest.approx(60.0)

    @pytest.mark.parametrize(
        "value", [None, True, False, "", "   ", "not a time", "2026-13-45T99:99:99", [], {}]
    )
    def test_an_unreadable_clock_keeps_the_item_rather_than_hiding_it(self, value: object) -> None:
        assert judge._age_from_ts(value, 1_000.0) == 0.0

    def test_a_future_stamp_is_zero_not_negative(self) -> None:
        assert judge._age_from_ts(2_000.0, 1_000.0) == 0.0


class TestArmPath:
    """A brief survives arming, revision, clearing and a store reload."""

    def test_round_trip(self, tmp_path: pathlib.Path) -> None:
        async def main() -> None:
            base = tmp_path / "nudges"
            base.mkdir()
            svc = AutoNudgeService(base_dir=base)
            brief = {"targets": ["chat-2-2"], "wake_when": "RULING"}
            loop = await svc.add("chat-1-1", "watch chat-2-2", idle_secs=60, judge=brief)
            assert loop.judge == brief

            loop.judge_quiet_streak = 3
            loop.judge_cursors = {"chat-2-2": 5}
            revised = await svc.update(loop.id, judge={"wake_when": "BLOCKED"})
            assert revised is not None
            assert revised.judge == {"wake_when": "BLOCKED"}
            assert revised.judge_quiet_streak == 0, "a new brief starts a new streak"
            assert revised.judge_cursors == {}

            cleared = await svc.update(loop.id, judge={})
            assert cleared is not None and cleared.judge == {}

            await svc.update(loop.id, judge=brief)
            untouched = await svc.update(loop.id, idle_secs=120)
            assert untouched is not None and untouched.judge == brief

            plain = await svc.add("chat-9-9", "no judge here", idle_secs=60)
            assert plain.judge == {}

        asyncio.run(main())


class TestTheBriefIsCredentialScrubbed:
    """A criterion is free text, and free text can name a secret.

    The brief reaches the loop store on disk and the loop serialization the dashboard
    reads. Allowlisting keys and clipping lengths bounds the SHAPE of what arrives; it
    reads every value as opaque, so a criterion naming a bearer token is kept whole and
    merely shorter. Each entrance is asserted on its own because they are independent: a
    brief from a tool call is published without ever passing the decode path.
    """

    SECRET = 'curl -H "Authorization: Bearer sk-live-AbCd1234567890abcdefghij" ok'

    def test_the_arm_entrance_scrubs(self, tmp_path: pathlib.Path) -> None:
        async def main() -> None:
            base = tmp_path / "nudges"
            base.mkdir()
            svc = AutoNudgeService(base_dir=base)
            loop = await svc.add(
                "chat-1-1", "watch it", idle_secs=60, judge={"wake_when": self.SECRET}
            )
            assert "sk-live-AbCd1234567890abcdefghij" not in loop.judge["wake_when"]
            assert "REDACTED" in loop.judge["wake_when"]

        asyncio.run(main())

    def test_the_update_entrance_scrubs(self, tmp_path: pathlib.Path) -> None:
        async def main() -> None:
            base = tmp_path / "nudges"
            base.mkdir()
            svc = AutoNudgeService(base_dir=base)
            loop = await svc.add("chat-1-1", "watch it", idle_secs=60)
            revised = await svc.update(loop.id, judge={"wake_when": self.SECRET})
            assert revised is not None
            assert "sk-live-AbCd1234567890abcdefghij" not in revised.judge["wake_when"]

        asyncio.run(main())

    def test_the_decode_entrance_scrubs(self) -> None:
        from kiro_crew import autonudge as _autonudge

        out = _autonudge._bounded_judge_spec({"wake_when": self.SECRET})
        assert "sk-live-AbCd1234567890abcdefghij" not in out["wake_when"]

    def test_a_target_carrying_a_credential_is_scrubbed(self) -> None:
        from kiro_crew import autonudge as _autonudge

        out = _autonudge.scrubbed_judge_spec(
            {"targets": ["https://api.example.com/x?token=ghp_AbCd1234567890abcdefghijklmn"]}
        )
        assert "ghp_AbCd1234567890abcdefghijklmn" not in out["targets"][0]

    def test_a_clean_forge_target_is_untouched(self) -> None:
        """The scrub cannot be allowed to break a legitimate watch."""
        from kiro_crew import autonudge as _autonudge

        url = "https://github.com/kirodotdev/KiroCrew/pull/12787"
        out = _autonudge.scrubbed_judge_spec({"targets": [url], "wake_when": "a check goes red"})
        assert out["targets"] == [url]
        assert out["wake_when"] == "a check goes red"

    def test_the_scrub_runs_before_the_clip(self) -> None:
        """A redaction marker can be longer than the secret it replaces.

        Clipping first would leave the marker free to land over the bound, so the order
        is load-bearing and the bound is asserted on the scrubbed value.
        """
        from kiro_crew import autonudge as _autonudge

        bound = _autonudge._JUDGE_MAX_CRITERION_CHARS
        raw = ("x" * (bound - 20)) + " Bearer sk-live-AbCd1234567890abcdefghij"
        out = _autonudge.scrubbed_judge_spec({"wake_when": raw})
        assert len(out["wake_when"]) <= bound
        assert "sk-live-AbCd1234567890abcdefghij" not in out["wake_when"]


class TestThePublishedKeysReachTheDashboardReader:
    """The list route's key names and the popover's reader must agree.

    These two sides are edited in different languages and different files, and
    nothing in either one refers to the other. A field renamed or dropped on the
    publishing side leaves the reader looking up a name nobody sends: that is silent,
    because the reader treats an absent brief as "this loop has no judge", which is
    also the honest answer for most loops. The popover then draws no judge line and
    every test on both sides still passes.

    So the names are asserted against each other here. The Python side is exercised
    through the REAL projection rather than a copied key list, because the projection
    is where a field is dropped.
    """

    _TS = pathlib.Path(__file__).resolve().parents[1] / "website/src/components/autoNudgeLoop.ts"

    @staticmethod
    def _published(**loop_kwargs: Any) -> dict:
        from kiro_crew.autonudge import NudgeLoop, new_goal_token
        from kiro_crew.dashboard.handlers.autonudge import _serialize_for_legacy_reader

        loop = NudgeLoop(
            id="l1",
            slot_key="chat-1-1",
            message="go",
            idle_secs=60,
            max_cycles=5,
            created_ts=1.0,
            goal_token=new_goal_token(),
            stop_sentinel_path="/x",
            max_runtime_secs=0,
            next_due_ts=61.0,
            **loop_kwargs,
        )
        loop.judge_quiet_streak = 3
        loop.judge_last_verdict = {"outcome": "quiet", "evidence_items": 4, "at": 1234.0}
        loop.judge_cursors = {"chat-2-2": 7}
        return _serialize_for_legacy_reader(loop)

    def test_every_judge_field_the_reader_declares_is_published(self) -> None:
        source = self._TS.read_text(encoding="utf-8")
        declared = set(re.findall(r"^\s{2}(judge[a-z_]*)\??:", source, re.MULTILINE))
        assert declared, "no judge field found; this test is looking in the wrong place"
        published = self._published(judge={"wake_when": "a line starts with RULING"})
        missing = sorted(name for name in declared if name not in published)
        assert not missing, f"the dashboard reads {missing}, which the list route does not publish"

    def test_the_criterion_the_reader_looks_up_is_the_one_sent(self) -> None:
        """``judgeReading`` keys into the brief, so the inner names matter too."""
        source = self._TS.read_text(encoding="utf-8")
        inner = set(re.findall(r"loop\?\.judge\?\.([a-z_]+)", source))
        assert inner, "no criterion lookup found in the reader"
        published = self._published(
            judge={"wake_when": "a line starts with RULING", "quiet_when": "WORKING"}
        )
        for name in inner:
            assert name in published["judge"], f"judge.{name} is read but not sent"

    def test_the_read_cursors_stay_unpublished(self) -> None:
        """The counterpart bound: the reader declares no cursor and must be sent none.

        A cursor names each watched target, so publishing it would disclose the
        subject list on a route with no owner gate.
        """
        published = self._published(judge={"wake_when": "x"})
        assert "judge_cursors" not in published
        assert "judge_cursors" not in self._TS.read_text(encoding="utf-8")


class TestNudgeWakeConfigSection:
    """``decisions.nudge_wake`` -- the two knobs the tick reads, and their bounds.

    The tick reads both through ``getattr`` chains with their own fallbacks, so it
    worked before this section existed. What these tests pin is that the section is
    now REACHABLE -- an operator who writes the key gets the behaviour -- and that
    registering it did not introduce a second spelling of the floor.
    """

    def test_the_shipped_default_is_the_lane_resolver_s_own_fallback(self) -> None:
        """An install that never wrote the key takes ``auto``."""
        from kiro_crew.config.sections import DecisionsConfig

        assert DecisionsConfig().nudge_wake.provider == "auto"
        assert DecisionsConfig.from_raw({}).nudge_wake.provider == "auto"

    def test_the_stored_floor_default_is_a_sentinel_not_a_copied_number(self) -> None:
        """0 is stored, and 0 is what the engine reads as "inherit".

        The floor is deliberately NOT spelled in the config section. The engine owns
        the number because its probe path answers the same question, and a copy here
        would be a second literal to keep in step.
        """
        from kiro_crew.config.sections import DecisionsConfig

        assert DecisionsConfig().nudge_wake.quiet_streak_floor == 0
        # The binding this section relies on: the engine's default IS the probe's.
        assert _JUDGE_QUIET_STREAK_FLOOR_DEFAULT == _MAX_QUIET_STREAK

    def test_the_engine_resolves_the_sentinel_to_the_shipped_floor(self) -> None:
        """A stored 0 becomes the probe's floor, through the engine's real reader."""
        from kiro_crew.config.sections import DecisionsConfig

        stored = DecisionsConfig.from_raw({}).nudge_wake

        class _Snapshot:
            decisions = DecisionsConfig(nudge_wake=stored)

        service = AutoNudgeService.__new__(AutoNudgeService)
        with patch("kiro_crew.config.live.snapshot", return_value=_Snapshot()):
            assert service._judge_quiet_streak_floor() == _MAX_QUIET_STREAK

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ({"provider": "jev"}, "jev"),
            ({"provider": "  Jev  "}, "jev"),
            ({"provider": "LLM"}, "llm"),
            # An unknown name folds to ``auto``. The lane vocabulary is closed, so a
            # typo must not read as a third lane, and it must not stop the gateway
            # booting either -- which is why it normalises rather than raising.
            ({"provider": "jevv"}, "auto"),
            ({"provider": ""}, "auto"),
            ({"provider": None}, "auto"),
            ({"provider": 7}, "auto"),
        ],
    )
    def test_the_lane_is_normalised_so_the_saved_config_says_what_is_in_force(
        self, raw: dict, expected: str
    ) -> None:
        from kiro_crew.config.sections import DecisionsConfig

        assert DecisionsConfig.from_raw({"nudge_wake": raw}).nudge_wake.provider == expected

    @pytest.mark.parametrize(
        "raw, stored",
        [
            ({"quiet_streak_floor": 3}, 3),
            ({"quiet_streak_floor": 0}, 0),
            # Negative and malformed both read as inherit rather than as unbounded.
            ({"quiet_streak_floor": -5}, 0),
            ({"quiet_streak_floor": "lots"}, 0),
            ({"quiet_streak_floor": None}, 0),
            # Stored as written; the engine clamps to its own ceiling on read,
            # because the ceiling is the engine's constant.
            ({"quiet_streak_floor": 9999}, 9999),
        ],
    )
    def test_the_floor_is_coerced_without_ever_reading_as_unbounded(
        self, raw: dict, stored: int
    ) -> None:
        from kiro_crew.config.sections import DecisionsConfig

        assert DecisionsConfig.from_raw({"nudge_wake": raw}).nudge_wake.quiet_streak_floor == stored

    def test_an_over_large_stored_floor_is_clamped_by_the_engine(self) -> None:
        """``config.json`` is agent-writable, so the ceiling holds on read."""
        from kiro_crew.autonudge import _JUDGE_QUIET_STREAK_FLOOR_MAX
        from kiro_crew.config.sections import DecisionsConfig

        stored = DecisionsConfig.from_raw({"nudge_wake": {"quiet_streak_floor": 9999}}).nudge_wake

        class _Snapshot:
            decisions = DecisionsConfig(nudge_wake=stored)

        service = AutoNudgeService.__new__(AutoNudgeService)
        with patch("kiro_crew.config.live.snapshot", return_value=_Snapshot()):
            assert service._judge_quiet_streak_floor() == _JUDGE_QUIET_STREAK_FLOOR_MAX

    @pytest.mark.parametrize("section", [None, "jev", 7, [], True])
    def test_a_non_object_section_reads_as_the_defaults(self, section: object) -> None:
        """A hand-edited config.json must not stop the gateway booting."""
        from kiro_crew.config.sections import DecisionsConfig

        got = DecisionsConfig.from_raw({"nudge_wake": section}).nudge_wake
        assert got.provider == "auto"
        assert got.quiet_streak_floor == 0

    def test_registering_the_section_did_not_disturb_the_rest_of_decisions(self) -> None:
        """The sibling keys still parse, including alongside a judge section."""
        from kiro_crew.config.sections import DecisionsConfig

        got = DecisionsConfig.from_raw(
            {
                "bucket": 42,
                "history_budget_chars": 1234,
                "provider": {"model": "some-model"},
                "nudge_wake": {"provider": "jev"},
            }
        )
        assert got.bucket == 42
        assert got.history_budget_chars == 1234
        assert got.provider.model == "some-model"
        assert got.nudge_wake.provider == "jev"


class TestTheHttpArmingRouteCarriesTheBrief:
    """``POST /api/autonudge`` -- the dashboard's plain-HTTP arming route.

    The MCP tool surface is not the only way a loop is armed, so this route has to
    carry a brief too. A route that read no ``judge`` and called
    ``authorize_and_add_nudge`` without one would answer 200 and tell the caller a
    loop was armed while the judge was not -- the exact failure
    ``session_directive_apply``'s own comment warns about, and the reason the route
    is tested here rather than only at the tool.
    """

    def _app(self, monkeypatch, fake_svc):
        from aiohttp import web

        from kiro_crew.dashboard.handlers import autonudge as _handler

        monkeypatch.setattr(_handler, "_autonudge_get", lambda: fake_svc)
        state = MagicMock()
        state._slots = {
            "chat-1-123": MagicMock(workspace="default", memory_mode="persistent", mode="chat")
        }
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/autonudge", _handler.api_autonudge_start)
        return app

    def _svc(self):
        svc = MagicMock()
        loop = NudgeLoop(id="loop-1", slot_key="chat-1-123", message="go")
        svc.add = AsyncMock(return_value=loop)
        svc.list_all = lambda: [loop]
        svc.get_by_id = lambda _id, _rows=[loop]: next(
            (r for r in _rows if getattr(r, "id", None) == _id), None
        )
        return svc

    @pytest.mark.asyncio
    async def test_a_body_carrying_a_judge_arms_a_loop_that_holds_the_spec(
        self, monkeypatch
    ) -> None:
        """The brief reaches the loop record rather than being dropped."""
        from aiohttp.test_utils import TestClient, TestServer

        brief = {
            "wake_when": "a worker line starts with RULING",
            "quiet_when": "workers report WORKING with no new status",
        }
        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge",
                json={"slot_key": "chat-1-123", "message": "go", "judge": brief},
            )
            assert resp.status == 200
        assert svc.add.await_args.kwargs["judge"] == brief

    @pytest.mark.asyncio
    async def test_an_invalid_judge_is_refused_with_400_and_arms_nothing(self, monkeypatch) -> None:
        """A refusal names the field, and no loop is armed carrying a bad brief.

        The refusal has to happen HERE: the chokepoint takes the brief through
        unchanged by design, so a route that forwarded an unchecked object would be
        the way around ``validate_judge_spec``'s bounds.
        """
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge",
                json={
                    "slot_key": "chat-1-123",
                    "message": "go",
                    "judge": {"wake_when": "x", "not_a_real_key": "y"},
                },
            )
            assert resp.status == 400
            payload = await resp.json()
            assert payload["code"] == "invalid_judge_spec"
            assert "not_a_real_key" in payload["error"]
        svc.add.assert_not_awaited(), "an invalid judge brief still armed the loop"

    @pytest.mark.asyncio
    async def test_a_body_with_no_judge_arms_a_loop_with_no_brief(self, monkeypatch) -> None:
        """Negative control: the route must not invent a brief for a caller.

        Without this, returning ``{}`` as a truthy-looking value would give every
        loop armed from the goal popover an empty judge and a judge tick it never
        asked for.
        """
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge",
                json={"slot_key": "chat-1-123", "message": "go"},
            )
            assert resp.status == 200
        assert "judge" not in svc.add.await_args.kwargs

    @pytest.mark.asyncio
    async def test_a_non_object_judge_is_400_not_500(self, monkeypatch) -> None:
        """A hand-written request body must not reach a traceback."""
        from aiohttp.test_utils import TestClient, TestServer

        svc = self._svc()
        async with TestClient(TestServer(self._app(monkeypatch, svc))) as client:
            resp = await client.post(
                "/api/autonudge",
                json={"slot_key": "chat-1-123", "message": "go", "judge": "jev"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_judge_spec"
        svc.add.assert_not_awaited()


class TestTheLoopPayloadPublishesNoEvidence:
    """What `GET /api/autonudge` may carry about a judge.

    The route has no per-owner gate -- its own docstring says it publishes presence,
    cadence, liveness and state, never what is being watched -- and `_serialize`
    is `asdict`, so a field joins these reads simply by existing on the dataclass.
    That is how `judge_cursors` reached them: not by a decision, but by being added.
    """

    def _payload(self, **judge_kw):
        from kiro_crew.dashboard.handlers.autonudge import _serialize_for_legacy_reader

        loop = NudgeLoop(id="l1", slot_key="chat-1-1", message="patrol chat-2-2", **judge_kw)
        return _serialize_for_legacy_reader(loop)

    def test_read_cursors_are_never_published(self) -> None:
        """They name every watched target and no reader can act on them."""
        payload = self._payload(judge_cursors={"chat-2-2": 41, "chat-3-3": 7})
        assert "judge_cursors" not in payload

    def test_the_verdict_carries_counts_and_never_evidence_text(self) -> None:
        """The stored verdict is text-free by construction, and stays that way.

        `verdict_record` builds it from the outcome, an item COUNT and a timestamp,
        so a transcript line the judge read cannot reach a reader of this list even
        though the verdict it produced can.
        """
        import json as _json

        secret = "SECRET-TRANSCRIPT-LINE-do-not-publish"
        verdict = judge.verdict_record(
            type("V", (), {"outcome": type("O", (), {"value": "progress_only"})()})(), 3
        )
        assert secret not in _json.dumps(verdict)
        assert set(verdict) == {"outcome", "evidence_items", "at"}
        assert verdict["evidence_items"] == 3

        payload = self._payload(judge_last_verdict=verdict, judge_quiet_streak=2)
        blob = _json.dumps(payload)
        assert secret not in blob
        # The two readings the popover needs DO survive, so withholding the cursor
        # is not a blanket refusal to report the judge.
        assert payload["judge_last_verdict"]["outcome"] == "progress_only"
        assert payload["judge_last_verdict"]["evidence_items"] == 3
        assert payload["judge_quiet_streak"] == 2

    def test_no_judge_field_carries_a_nested_evidence_list(self) -> None:
        """A shape check rather than a string check: the record must stay flat.

        A future verdict that carried its evidence for context would pass a
        substring test against this test's own literal while publishing every
        transcript line it read.
        """
        payload = self._payload(
            judge={"wake_when": "a RULING line", "quiet_when": "still WORKING"},
            judge_last_verdict=judge.verdict_record(
                type("V", (), {"outcome": type("O", (), {"value": "quiet"})()})(), 5
            ),
        )
        for key, value in payload.items():
            if not key.startswith("judge"):
                continue
            if isinstance(value, dict):
                assert not any(
                    isinstance(inner, (list, tuple)) for inner in value.values()
                ), f"{key} carries a nested sequence, which is how evidence would travel"


class TestTheTypedProbeObservesBeforeTheJudge:
    """On a monitor-backed loop the typed probe observes FIRST and a typed verdict wins.

    The order matters because the judge emits no terminal of its own. If it answered
    ahead of the monitor guard, a judged pull-request loop would return before
    ``irq.poll`` runs, nothing would be left to notice a merged or closed subject, and
    the watch would outlive the thing it watches, bounded only by its cycle cap.

    The judge therefore screens exactly one tick: the one the probe looked at and found
    nothing in. No typed signal can be suppressed there, because every outcome that
    means something -- terminal, wake, fallback, an interrupted poll, a drifted
    target -- has already returned by that line. What the judge adds is the evidence
    the probe cannot read, such as a review left in prose.
    """

    @staticmethod
    def _judged_pr_loop() -> NudgeLoop:
        """A loop that watches a pull request AND carries a judge brief.

        This pair is the configuration the defect needed, and it is first-class:
        ``monitor_start`` accepts a brief on any monitor, so nothing about it is rare.
        """
        loop = NudgeLoop(
            id="judged-pr",
            slot_key="chat-1-123",
            message="Watch https://github.com/acme/widgets/pull/42 until green",
            idle_secs=30,
            monitor=MonitorState(
                kind="gh-pr",
                target="acme/widgets#42",
                objective="review_ready",
                created_ts=1_000.0,
            ),
            gate=True,
        )
        loop.judge = {"wake_when": "a review asks for a change", "quiet_when": "nothing did"}
        return loop

    @staticmethod
    def _spy(service: AutoNudgeService, answer: bool | None) -> list[str]:
        """Record whether the judge was consulted at all, and with which loop."""
        calls: list[str] = []

        async def _judge(loop: NudgeLoop) -> bool | None:
            calls.append(loop.id)
            return answer

        service._judge_tick_is_quiet = _judge  # type: ignore[method-assign]
        return calls

    def _drive(self, tmp_path: Any, monkeypatch: Any, outcome: Outcome, answer: bool | None):
        """Run one tick with the probe pinned to *outcome* and the judge to *answer*."""
        import kiro_crew.autonudge as _an

        async def on_fire(loop: NudgeLoop) -> bool:
            return True

        monkeypatch.setattr(
            _an.irq,
            "poll",
            lambda identity, message, probe: _an.irq.Verdict(outcome, "pinned"),
        )
        service = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        loop = self._judged_pr_loop()
        service._loops[loop.id] = loop
        calls = self._spy(service, answer)
        try:
            quiet = asyncio.run(service._monitor_tick_is_quiet(loop))
        finally:
            service.stop()
        return quiet, loop, calls

    def test_a_merged_pull_request_deactivates_a_judged_loop(self, tmp_path, monkeypatch) -> None:
        """The defect, stated as a test: the terminal must still land on a JUDGED loop."""
        _quiet, loop, calls = self._drive(tmp_path, monkeypatch, Outcome.TERMINAL, True)
        assert loop.monitor is not None
        assert loop.monitor.outcome is not None, "a merged subject must be recorded terminal"
        assert calls == [], "a typed terminal wins outright -- the judge is not asked"

    def test_a_probe_actionable_tick_fires_without_asking_the_judge(
        self, tmp_path, monkeypatch
    ) -> None:
        """A wake is a typed signal, so it is not something a judge may screen away."""
        quiet, _loop, calls = self._drive(tmp_path, monkeypatch, Outcome.WAKE, True)
        assert quiet is False, "an actionable observation spends the turn"
        assert calls == [], "the judge must not be able to suppress a typed wake"

    def test_a_probe_quiet_tick_is_the_one_the_judge_screens(self, tmp_path, monkeypatch) -> None:
        """The probe found nothing, so the judge gets its say -- and here it fires."""
        quiet, loop, calls = self._drive(tmp_path, monkeypatch, Outcome.QUIET, False)
        assert calls == [loop.id], "a quiet observation is the tick the judge screens"
        assert quiet is False, "the judge may wake on evidence the probe cannot read"

    def test_a_quiet_tick_keeps_its_own_verdict_when_no_judge_answers(
        self, tmp_path, monkeypatch
    ) -> None:
        """``None`` is 'no judge here', which must leave the probe's verdict standing."""
        quiet, loop, calls = self._drive(tmp_path, monkeypatch, Outcome.QUIET, None)
        assert calls == [loop.id]
        assert quiet is True, "without a judge answer the probe's own quiet still holds"


class TestEachBriefBoundHasOneSpelling:
    """The brief's shape bounds resolve to ``validation``, not to copied literals.

    The arming surface refuses an oversized brief and this loader trims one. Two
    numbers would disagree silently in the worst direction: the arming call accepts
    a brief the loader then clips, so an owner's criterion is honoured at 500 chars
    on the way in and something else on the way out.
    """

    def test_the_target_count_is_spelled_once(self) -> None:
        from kiro_crew import autonudge as _autonudge
        from kiro_crew import validation as _validation

        assert judge.MAX_TARGETS is _validation.MAX_JUDGE_TARGETS
        assert _autonudge._JUDGE_MAX_TARGETS is _validation.MAX_JUDGE_TARGETS

    def test_the_criterion_length_is_spelled_once(self) -> None:
        from kiro_crew import autonudge as _autonudge
        from kiro_crew import validation as _validation

        assert point.MAX_CRITERION_CHARS is _validation.MAX_JUDGE_CRITERION_CHARS
        assert _autonudge._JUDGE_MAX_CRITERION_CHARS is _validation.MAX_JUDGE_CRITERION_CHARS

    def test_the_target_length_is_spelled_once(self) -> None:
        from kiro_crew import autonudge as _autonudge
        from kiro_crew import validation as _validation

        assert _autonudge._JUDGE_MAX_TARGET_CHARS is _validation.MAX_JUDGE_TARGET_CHARS


class TestEveryOutcomeHasALocalizedWord:
    """The popover renders a word per outcome, so the enum owes the catalog one.

    The verdict line is otherwise localized, and an outcome with no entry renders
    the point's own identifier inside it. Asserted against the enum rather than a
    hand-written list, so adding a fifth outcome fails here instead of shipping an
    English token into thirteen catalogs.
    """

    _EN = pathlib.Path(__file__).resolve().parents[1] / "website/src/i18n/locales/en.manual.json"

    def _words(self) -> dict[str, str]:
        import json

        section = json.loads(self._EN.read_text(encoding="utf-8"))["components"]["autoNudgePopover"]
        prefix = "judge_outcome_"
        return {
            key[len(prefix) :]: value for key, value in section.items() if key.startswith(prefix)
        }

    def test_every_outcome_value_has_a_word(self) -> None:
        words = self._words()
        missing = sorted(member.value for member in Outcome if member.value not in words)
        assert not missing, f"no localized word for outcome(s): {missing}"

    def test_an_unmapped_token_still_reads_as_a_word(self) -> None:
        """The verdict record can store ``unknown``, so that token needs a word too."""
        assert "unknown" in self._words()
