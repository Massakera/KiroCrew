"""The strip's two routes: who may call them, what a verdict may say, and that it APPENDS.

Three things are pinned, because each is a way a verdict route goes wrong.

Owner-only, both verbs. The feedback route WRITES the decision log, so an app
token that reached it could grow the file the operator reads and pollute the
record with verdicts nobody gave; the summary route reports how the operator's own
conversations were decided. Same gate as the consent pair in the same module.

Validated, not coerced. A row filed under a turn nobody can name, or carrying a
verdict outside the pair the reader folds, makes the summary WRONG rather than
incomplete -- so it is refused at the door.

Append-only, checked by reading the file. A verdict is a second event about a
turn, so two verdicts leave two rows with two timestamps and the decision row
they judge is byte-identical afterwards. That is what makes "when did they change
their mind" answerable, and it is asserted on the bytes rather than on the call.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers.decisions import (
    api_decisions_feedback,
    api_decisions_summary,
)
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions import summary as summary_mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the decision log directory into *tmp_path* and hand back the path."""
    directory = tmp_path / "decisions"
    monkeypatch.setattr(log_mod, "log_dir", lambda: directory)
    return directory


@pytest.fixture
def audit(monkeypatch):
    """Capture the SEL rows the handlers write."""
    import kiro_crew.dashboard.handlers as handlers_pkg

    rows: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: rows.append(kw)
    monkeypatch.setattr(handlers_pkg, "sel", lambda: fake)
    return rows


def _request(
    *, app: str = "", user: str = "owner-1", owner: str = "owner-1", body=None, query=None
):
    """A request shaped like a real DASHBOARD OWNER call (see test_decisions_consent.py)."""
    req = MagicMock()
    req.path = "/api/decisions/feedback"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    req.query = query or {}
    if isinstance(body, Exception):
        req.json = AsyncMock(side_effect=body)
    else:
        req.json = AsyncMock(return_value=body if body is not None else {})
    return req


def _body(**kw):
    base = {"turn_id": "turn-abc", "verdict": "right", "side": "jev"}
    base.update(kw)
    return base


def _rows(home):
    """Every row in today's day-file, in written order."""
    path = log_mod.log_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestFeedbackAuth:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"app": "some-app"},
            {"user": "someone-else"},
            {"user": ""},
        ],
    )
    async def test_only_the_dashboard_owner_may_record_a_verdict(self, home, audit, kwargs):
        resp = await api_decisions_feedback(_request(body=_body(), **kwargs))

        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "dashboard_owner_required"
        assert _rows(home) == []

    @pytest.mark.asyncio
    async def test_a_refusal_is_audited_as_denied(self, home, audit):
        await api_decisions_feedback(_request(body=_body(), app="some-app"))

        assert [r["outcome"] for r in audit] == ["denied"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs", [{"app": "some-app"}, {"user": "someone-else"}])
    async def test_only_the_dashboard_owner_may_read_the_summary(self, home, audit, kwargs):
        resp = await api_decisions_summary(_request(**kwargs))

        assert resp.status == 403


class TestFeedbackSchema:
    @pytest.mark.asyncio
    async def test_a_valid_verdict_is_accepted(self, home, audit):
        resp = await api_decisions_feedback(_request(body=_body()))

        assert resp.status == 200
        assert json.loads(resp.text) == {"ok": True}

    @pytest.mark.asyncio
    async def test_a_cleared_verdict_is_a_real_value(self, home, audit):
        """``null`` means the person took their verdict back, which the log must say."""
        resp = await api_decisions_feedback(_request(body=_body(verdict=None)))

        assert resp.status == 200
        assert _rows(home)[0]["verdict"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"verdict": "right", "side": "jev"},  # no turn
            {"turn_id": "", "verdict": "right", "side": "jev"},
            {"turn_id": "   ", "verdict": "right", "side": "jev"},
            {"turn_id": 7, "verdict": "right", "side": "jev"},
            {"turn_id": "t", "verdict": "maybe", "side": "jev"},
            {"turn_id": "t", "verdict": True, "side": "jev"},
            {"turn_id": "t", "verdict": "right"},  # no side
            {"turn_id": "t", "verdict": "right", "side": "both"},
            {"turn_id": "t", "verdict": "right", "side": None},
            [],
            "nope",
        ],
    )
    async def test_an_unusable_body_is_refused_and_writes_nothing(self, home, audit, body):
        resp = await api_decisions_feedback(_request(body=body))

        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "decisions_feedback_invalid_body"
        assert _rows(home) == []

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_is_refused(self, home, audit):
        resp = await api_decisions_feedback(_request(body=ValueError("bad json")))

        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "invalid_json"
        assert _rows(home) == []

    @pytest.mark.asyncio
    async def test_the_row_carries_exactly_the_five_fields_the_reader_folds(self, home, audit):
        await api_decisions_feedback(_request(body=_body(side="baseline", verdict="wrong")))

        row = _rows(home)[0]
        assert set(row) == {"ts", "kind", "turn_id", "verdict", "side"}
        assert row["kind"] == "feedback"
        assert row["turn_id"] == "turn-abc"
        assert row["verdict"] == "wrong"
        assert row["side"] == "baseline"

    @pytest.mark.asyncio
    async def test_an_overlong_turn_id_is_bounded(self, home, audit):
        """One malformed caller must not write an unbounded line into a shared file."""
        await api_decisions_feedback(_request(body=_body(turn_id="t" * 5000)))

        assert len(_rows(home)[0]["turn_id"]) == log_mod._MAX_TURN_ID_CHARS


class TestAppendOnly:
    @pytest.mark.asyncio
    async def test_a_changed_mind_leaves_two_rows_not_one_edit(self, home, audit):
        await api_decisions_feedback(_request(body=_body(verdict="right")))
        await api_decisions_feedback(_request(body=_body(verdict="wrong")))

        rows = _rows(home)
        assert [r["verdict"] for r in rows] == ["right", "wrong"]

    @pytest.mark.asyncio
    async def test_a_decision_row_already_in_the_file_is_untouched(self, home, audit):
        home.mkdir(parents=True, exist_ok=True)
        log_mod.append(log_mod.build_row(point="skills.select", session_key="s", latency_ms=12))
        before = log_mod.log_path().read_bytes()

        await api_decisions_feedback(_request(body=_body()))

        after = log_mod.log_path().read_bytes()
        assert after.startswith(before)
        assert len(_rows(home)) == 2

    @pytest.mark.asyncio
    async def test_the_verdict_is_audited_with_its_side(self, home, audit):
        await api_decisions_feedback(_request(body=_body(side="baseline")))

        allowed = [r for r in audit if r["outcome"] == "allowed"]
        assert len(allowed) == 1
        assert "side=baseline" in allowed[0]["resources"]


class TestSummaryRoute:
    @pytest.mark.asyncio
    async def test_an_empty_log_folds_to_zeros_not_an_error(self, home, audit):
        resp = await api_decisions_summary(_request())

        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["rows"] == 0
        assert body["agree_rate"] is None
        assert body["mean_p"] is None
        assert body["since_days"] == summary_mod.DEFAULT_SINCE_DAYS

    @pytest.mark.asyncio
    async def test_it_folds_decisions_and_verdicts_from_the_same_file(self, home, audit):
        home.mkdir(parents=True, exist_ok=True)
        log_mod.append({"ts": "x", "point": "skills.select", "agree": True, "p": 0.8})
        log_mod.append(
            {"ts": "x", "point": "skills.select", "agree": False, "p": 0.6, "tokens_saved": 100}
        )
        await api_decisions_feedback(_request(body=_body(verdict="right", side="jev")))
        await api_decisions_feedback(_request(body=_body(verdict="wrong", side="baseline")))

        body = json.loads((await api_decisions_summary(_request())).text)

        assert body["rows"] == 2
        assert body["agree_rate"] == 0.5
        assert body["mean_p"] == pytest.approx(0.7)
        assert body["tokens_saved"] == 100
        assert body["feedback"]["right"] == {"jev": 1, "baseline": 0, "total": 1}
        assert body["feedback"]["wrong"] == {"jev": 0, "baseline": 1, "total": 1}

    @pytest.mark.asyncio
    async def test_a_since_parameter_is_honoured_and_reported_back(self, home, audit):
        body = json.loads((await api_decisions_summary(_request(query={"since": "3d"}))).text)

        assert body["since_days"] == 3

    @pytest.mark.asyncio
    async def test_an_unusable_since_narrows_to_the_default(self, home, audit):
        body = json.loads((await api_decisions_summary(_request(query={"since": "999d"}))).text)

        assert body["since_days"] == summary_mod.DEFAULT_SINCE_DAYS


class TestSummaryReader:
    """The fold itself, without the route: the reader is the tolerant part."""

    def test_an_unparseable_line_is_counted_not_raised(self, home):
        home.mkdir(parents=True, exist_ok=True)
        log_mod.append({"ts": "x", "point": "skills.select", "agree": True})
        with log_mod.log_path().open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
            handle.write('"a string row"\n')

        folded = summary_mod.summarize(1)

        assert folded["rows"] == 1
        assert folded["unreadable"] == 2

    def test_a_row_with_no_agree_field_is_not_counted_as_disagreement(self, home):
        """A window that predates the field reports no rate, not a confident zero."""
        home.mkdir(parents=True, exist_ok=True)
        log_mod.append(log_mod.build_row(point="skills.select", session_key="s", latency_ms=5))

        folded = summary_mod.summarize(1)

        assert folded["rows"] == 1
        assert folded["agree_rate"] is None

    def test_p_is_read_from_the_gate_row_shape_too(self, home):
        home.mkdir(parents=True, exist_ok=True)
        log_mod.append({"ts": "x", "answers": {"skill": {"value": "brazil", "p": 0.5}}})

        assert summary_mod.summarize(1)["mean_p"] == 0.5

    def test_a_boolean_p_is_not_a_probability(self, home):
        home.mkdir(parents=True, exist_ok=True)
        log_mod.append({"ts": "x", "p": True})

        assert summary_mod.summarize(1)["mean_p"] is None

    def test_a_cleared_verdict_is_counted_apart_from_both_verdicts(self, home):
        home.mkdir(parents=True, exist_ok=True)
        log_mod.append(log_mod.build_feedback_row(turn_id="t", verdict=None, side="jev"))

        folded = summary_mod.summarize(1)

        assert folded["cleared"] == 1
        assert folded["feedback"]["right"]["total"] == 0
        assert folded["feedback"]["wrong"]["total"] == 0

    def test_a_day_outside_the_window_is_not_read(self, home):
        home.mkdir(parents=True, exist_ok=True)
        old = datetime.now(timezone.utc).date() - timedelta(days=3)
        log_mod.log_path(old).write_text(
            json.dumps({"ts": "x", "point": "skills.select", "agree": True}) + "\n",
            encoding="utf-8",
        )

        assert summary_mod.summarize(1)["rows"] == 0
        assert summary_mod.summarize(4)["rows"] == 1

    def test_a_day_file_that_cannot_be_read_is_skipped_not_raised(self, home, monkeypatch):
        home.mkdir(parents=True, exist_ok=True)
        log_mod.append({"ts": "x", "point": "skills.select", "agree": True})

        def _boom(*_args, **_kwargs):
            raise PermissionError("chmod-ed by an operator")

        monkeypatch.setattr(type(log_mod.log_path()), "read_text", _boom)

        assert summary_mod.summarize(1)["rows"] == 0

    def test_the_row_cap_bounds_the_fold(self, home, monkeypatch):
        home.mkdir(parents=True, exist_ok=True)
        for _ in range(5):
            log_mod.append({"ts": "x", "point": "skills.select", "agree": True})
        monkeypatch.setattr(summary_mod, "MAX_ROWS", 2)

        assert summary_mod.summarize(1)["rows"] == 2

    def test_errors_and_scrubs_are_counted_from_the_gate_row_shape(self, home):
        home.mkdir(parents=True, exist_ok=True)
        log_mod.append(
            log_mod.build_row(point="skills.select", session_key="s", latency_ms=3, error="timeout")
        )
        log_mod.append(
            log_mod.build_row(point="skills.select", session_key="s", latency_ms=3, scrubbed=True)
        )

        folded = summary_mod.summarize(1)

        assert folded["rows"] == 2
        assert folded["errors"] == 1
        assert folded["scrubbed"] == 1

    def test_several_answers_have_no_single_probability(self, home):
        """One of them is not "the" p, so none is picked for the row."""
        home.mkdir(parents=True, exist_ok=True)
        log_mod.append({"ts": "x", "answers": {"a": {"p": 0.2}, "b": {"p": 0.9}}})
        log_mod.append({"ts": "x", "answers": {"a": "not an object"}})

        assert summary_mod.summarize(1)["mean_p"] is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("7d", 7),
            ("7", 7),
            (" 3D ", 3),
            (3, 3),
            (14, 14),
            (15, summary_mod.DEFAULT_SINCE_DAYS),
            (0, summary_mod.DEFAULT_SINCE_DAYS),
            (-1, summary_mod.DEFAULT_SINCE_DAYS),
            ("week", summary_mod.DEFAULT_SINCE_DAYS),
            (None, summary_mod.DEFAULT_SINCE_DAYS),
            (True, summary_mod.DEFAULT_SINCE_DAYS),
        ],
    )
    def test_since_is_lenient_on_spelling_and_strict_on_range(self, raw, expected):
        assert summary_mod.parse_since(raw) == expected
