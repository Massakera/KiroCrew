"""Conformance for gateway-owned acceptance on the conductor work ledger.

Pins the contract of the work-ledger acceptance spec (§7 table): evidence is
produced only by the gateway's evaluator, bound to the criterion and the worker
submission it was captured against (by version and by content) and to an exact
revision -- a head SHA plus the exact current check set, or a byte digest read
under a root pinned at bind; ``close(state="accepted")`` is decided in the store
against a fresh observation and only on evidence the crew log is confirmed to hold;
late or out-of-order results never publish as current; and the evidence survives a
rebuild. The review findings each have a named reproduction below. External sources
are simulated -- no real pull request, no personal ledger.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from conftest import requires_symlinks
from kiro_crew import pinned_fs
from kiro_crew import work_acceptance as wa
from kiro_crew import work_ledger as wl
from kiro_crew.crew_log import projection
from kiro_crew.dashboard.handlers import work_ledger as routes

CONDUCTOR = "chat-acc-conductor"
OTHER = "chat-acc-other"
WORKER = "chat-acc-worker"
REPO = "owner/name"
PR = 7
H1 = "a" * 40
H2 = "b" * 40


class _Slot:
    def __init__(self, created_by: str = "", project: str = "") -> None:
        self._created_by = created_by
        self.workspace = "default"
        self.running = False
        self.project = project


_SLOTS: dict[str, _Slot] = {}


def _run(
    name: str,
    conclusion: str | None = "success",
    *,
    run_id: int,
    suite: int = 1,
    app: str = "github-actions",
    status: str = "completed",
    head: str | None = None,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "head_sha": head,
        "app": {"slug": app},
        "check_suite": {"id": suite},
    }


class _GitHub:
    """A simulated forge: one pull request, its head, and that head's two boards."""

    def __init__(self) -> None:
        self.head = H1
        self.state = "OPEN"
        self.draft = False
        self.runs: list[dict[str, Any]] = [_run("ci / test", run_id=100)]
        self.statuses: list[dict[str, Any]] = []
        self.omit_total = False
        self.status_sha: str | None = None
        self.calls: list[Any] = []
        self.during: Any = None

    def _ok(self, payload: Any) -> Any:
        return SimpleNamespace(ok=True, stdout=json.dumps(payload))

    def call(self, args: list[str]) -> Any:
        self.calls.append(("call", tuple(args)))
        if self.during is not None:
            hook, self.during = self.during, None
            hook()
        return self._ok(
            {"state": self.state, "mergedAt": None, "isDraft": self.draft, "headRefOid": self.head}
        )

    def api(self, path: str, *, paginated_page: int = 0) -> Any:
        self.calls.append(("api", path, paginated_page))
        if "/check-runs" in path:
            rows = [dict(r, head_sha=r["head_sha"] or self.head) for r in self.runs]
            page: dict[str, Any] = {"check_runs": rows}
            if not self.omit_total:
                page["total_count"] = len(rows)
            return self._ok(page)
        return self._ok(
            {
                "sha": self.status_sha or self.head,
                "total_count": len(self.statuses),
                "statuses": self.statuses,
            }
        )

    @property
    def queried(self) -> list[Any]:
        return [c for c in self.calls if c[0] == "call"]


@pytest.fixture
def gh(monkeypatch) -> _GitHub:
    fake = _GitHub()
    monkeypatch.setattr(wa, "_default_gh", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _SLOTS.clear()
    routes._BOARD_LOCKS.clear()
    routes._EVAL_LOCKS.clear()

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(routes, "_reaches_a_channel", lambda request, sk: False)
    yield
    _SLOTS.clear()


@pytest.fixture
def recorded(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    _real_units(monkeypatch)
    append = routes.crew_log_emit.on_work_recorded

    def capture(unit, data):
        calls.append((unit, data))
        return append(unit, data)

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", capture)
    return calls


@pytest.fixture
def pinned_walk():
    if not pinned_fs.supports_pinned_walk():
        pytest.skip("the file reader requires descriptor-relative no-follow opens")


def _req(method: str, path: str, *, body: Any = ..., sk: str) -> web.Request:
    app = web.Application()
    state = MagicMock()
    state.get_slot = MagicMock(side_effect=lambda key: _SLOTS.get(key))
    app["state"] = state
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": sk})
    req["internal_auth"] = True
    if body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


async def _record(sk: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_ledger_record(
        _req("POST", "/api/work-ledger/record", body=body, sk=sk)
    )
    return resp.status, json.loads(resp.text)


async def _report(sk: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_report(_req("POST", "/api/work-ledger/report", body=body, sk=sk))
    return resp.status, json.loads(resp.text)


async def _read(sk: str) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_ledger_get(_req("GET", "/api/work-ledger", sk=sk))
    return resp.status, json.loads(resp.text)


def _pr_bar() -> dict[str, Any]:
    return {"kind": "pr_checks", "pr": PR, "repo": REPO}


async def _board(acceptance: dict[str, Any], *, project: str = "", done: bool = True) -> str:
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship", "round": 1})
    assert status == 200, body
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "one", "acceptance": acceptance}
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR, project=project)
    status, body = await _record(
        CONDUCTOR, {"action": "bind", "item_id": item_id, "worker_session_key": WORKER}
    )
    assert status == 200, body
    if done:
        status, body = await _report(WORKER, {"status": "done", "summary": "built", "pr": PR})
        assert status == 200, body
    return item_id


def _item(item_id: str) -> wl.WorkItem:
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    return item


async def _evaluate(item_id: str) -> tuple[int, dict[str, Any]]:
    return await _record(CONDUCTOR, {"action": "evaluate", "item_id": item_id})


async def _accept(item_id: str) -> tuple[int, dict[str, Any]]:
    return await _record(CONDUCTOR, {"action": "close", "item_id": item_id, "state": "accepted"})


def _store_board(acceptance: dict[str, Any]) -> str:
    """A board written through the store directly, for store-level properties."""
    wl.ensure_conductor(CONDUCTOR, goal="g")
    item = wl.apply_conductor_action(CONDUCTOR, "create", title="t", acceptance=acceptance)["item"]
    wl.apply_worker_report(CONDUCTOR, item.item_id, status="done", summary="s")
    return item.item_id


def _passing(revision: dict[str, Any]) -> dict[str, Any]:
    return wa.Observation(verdict="pass", policy=wa.POLICY_PR_CHECKS, revision=revision).to_dict()


# ── A1/A2: authority and the request carries no substitute ─────────────────


@pytest.mark.asyncio
async def test_a_worker_or_another_conductor_cannot_evaluate_or_close_the_item(recorded, gh):
    item_id = await _board(_pr_bar())
    before = wl.item_path(CONDUCTOR, item_id).read_bytes()
    for caller in (WORKER, OTHER):
        for body in (
            {"action": "evaluate", "item_id": item_id},
            {"action": "close", "item_id": item_id, "state": "accepted"},
        ):
            status, resp = await _record(caller, body)
            assert status == 404, (caller, body, resp)
            assert resp["code"] == wl.CODE_NO_LEDGER
    status, _ = await _record(OTHER, {"action": "goal", "goal": "mine", "round": 1})
    assert status == 200
    status, resp = await _record(OTHER, {"action": "evaluate", "item_id": item_id})
    assert (status, resp["code"]) == (404, wl.CODE_UNKNOWN_ITEM)
    assert wl.item_path(CONDUCTOR, item_id).read_bytes() == before
    assert gh.calls == []


@pytest.mark.asyncio
async def test_a_written_verdict_is_retired_and_never_authorises_acceptance(recorded, gh):
    item_id = await _board(_pr_bar())
    status, body = await _record(
        CONDUCTOR, {"action": "verdict", "item_id": item_id, "verdict": "pass"}
    )
    assert (status, body["code"]) == (400, "verdict_retired")
    assert _item(item_id).verdict is None
    status, body = await _accept(item_id)
    assert (status, body["code"]) == (409, wl.CODE_EVIDENCE_REQUIRED)
    assert _item(item_id).state == "open"


@pytest.mark.asyncio
async def test_a_fabricated_receipt_in_the_body_is_not_a_field(recorded, gh):
    item_id = await _board(_pr_bar())
    for extra in ("evidence", "acceptance_proof", "admitted_root", "evidence_recorded"):
        status, body = await _record(
            CONDUCTOR,
            {
                "action": "close",
                "item_id": item_id,
                "state": "accepted",
                extra: {"verdict": "pass", "revision": {"kind": "git_sha", "sha": H1}},
            },
        )
        assert status == 400, body
    assert _item(item_id).state == "open"


@pytest.mark.asyncio
async def test_evaluate_reads_the_persisted_criterion_not_the_request(recorded, gh):
    item_id = await _board(_pr_bar())
    status, body = await _record(
        CONDUCTOR,
        {
            "action": "evaluate",
            "item_id": item_id,
            "acceptance": {"kind": "pr_checks", "pr": 999, "repo": "evil/green"},
        },
    )
    assert status == 200, body
    assert gh.queried == [
        (
            "call",
            ("pr", "view", str(PR), "--repo", REPO, "--json", "state,mergedAt,isDraft,headRefOid"),
        )
    ]
    assert _item(item_id).acceptance == _pr_bar()


@pytest.mark.asyncio
async def test_a_worker_still_in_progress_yields_no_evidence_even_if_the_file_exists(
    recorded, tmp_path
):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "out.txt").write_text("stub", encoding="utf-8")
    item_id = await _board({"kind": "file", "path": "out.txt"}, project=str(project), done=False)
    status, _ = await _report(WORKER, {"status": "progress", "summary": "writing"})
    assert status == 200
    status, body = await _evaluate(item_id)
    assert (status, body["code"]) == (409, wl.CODE_NOT_EVALUABLE)
    assert _item(item_id).evidence is None


# ── A3/A4: verdicts and the guarded close ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_pass_on_the_head_that_is_still_the_head_is_accepted_with_its_proof(recorded, gh):
    item_id = await _board(_pr_bar())
    status, body = await _evaluate(item_id)
    assert status == 200, body
    evidence = body["item"]["evidence"]
    assert evidence["verdict"] == "pass"
    assert evidence["revision"]["sha"] == H1 and evidence["revision"]["checks"] == 1
    assert evidence["evaluator"] == wa.EVALUATOR_VERSION
    assert evidence["policy"] == wa.POLICY_PR_CHECKS
    assert _item(item_id).evidence_recorded == evidence["evidence_id"]
    _status, read = await _read(CONDUCTOR)
    assert read["items"][0]["evidence_current"] is True

    status, body = await _accept(item_id)
    assert status == 200, body
    item = _item(item_id)
    assert item.state == "accepted"
    assert item.acceptance_proof["evidence_id"] == evidence["evidence_id"]
    assert item.acceptance_proof["revision"] == evidence["revision"]
    assert item.acceptance_proof["closing_observation"]["revision"] == evidence["revision"]
    assert len(gh.queried) == 2  # the close re-read the forge
    evaluate_entry = next(d for _u, d in recorded if d.get("action") == "evaluate")
    close_entry = next(d for _u, d in recorded if d.get("action") == "close")
    assert evaluate_entry["evidence"]["evidence_id"] == evidence["evidence_id"]
    assert close_entry["acceptance_proof"] == item.acceptance_proof


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runs, state, draft, verdict",
    [
        ([_run("ci / test", "failure", run_id=1)], "OPEN", False, "fail"),
        ([_run("ci / test", None, run_id=1, status="in_progress")], "OPEN", False, "pending"),
        ([_run("ci / test", None, run_id=1, status="queued")], "OPEN", True, "refused"),
        ([], "OPEN", False, "pending"),
        ([_run("ci / test", "mystery", run_id=1)], "OPEN", False, "error"),
        ([_run("ci / test", "skipped", run_id=1)], "OPEN", False, "refused"),
        ([_run("ci / test", run_id=1)], "MERGED", False, "refused"),
    ],
)
async def test_nothing_but_a_complete_passing_board_authorises_acceptance(
    recorded, gh, runs, state, draft, verdict
):
    gh.runs, gh.state, gh.draft = runs, state, draft
    item_id = await _board(_pr_bar())
    code, body = await _evaluate(item_id)
    assert code == 200, body
    assert body["item"]["evidence"]["verdict"] == verdict
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_EVIDENCE_REQUIRED)
    assert _item(item_id).state == "open"


# ── review P1-2: a cancelled attempt is not noise ─────────────────────────


@pytest.mark.asyncio
async def test_a_cancelled_check_beside_a_green_one_is_not_a_pass(recorded, gh):
    """Tests=CANCELLED and Lint=SUCCESS on the same head used to pass."""
    gh.runs = [
        _run("Tests", "cancelled", run_id=10, suite=1),
        _run("Lint", "success", run_id=11, suite=2),
    ]
    item_id = await _board(_pr_bar())
    _code, body = await _evaluate(item_id)
    assert body["item"]["evidence"]["verdict"] == "fail"
    assert "cancelled" in body["item"]["evidence"]["diagnostic"]
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_EVIDENCE_REQUIRED)


@pytest.mark.parametrize("leftover", ["cancelled", "stale", "failure"])
def test_an_attempt_is_superseded_only_by_a_proven_replacement(leftover):
    """A later attempt of the same check in the same suite replaces the old one; the
    same name in ANOTHER suite is another check and still counts."""
    gh = _GitHub()
    gh.runs = [
        _run("Tests", leftover, run_id=10, suite=1),
        _run("Tests", "success", run_id=12, suite=1),
    ]
    seen = wa.observe(_pr_bar(), gh=gh)
    assert seen.verdict == "pass"
    assert [r["id"] for r in seen.sources["superseded"]["rows"]] == [10]
    gh.runs = [
        _run("Tests", leftover, run_id=10, suite=1),
        _run("Tests", "success", run_id=12, suite=2),
    ]
    assert wa.observe(_pr_bar(), gh=gh).verdict == "fail"


# ── review P1-4 / P2: the evaluated check set is part of the revision ──────


@pytest.mark.asyncio
async def test_a_check_that_vanishes_before_the_close_blocks_acceptance(recorded, gh):
    gh.runs = [_run("Tests", run_id=10, suite=1)]
    item_id = await _board(_pr_bar())
    await _evaluate(item_id)
    gh.runs = [_run("UnrelatedLint", run_id=20, suite=3)]
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_TARGET_CHANGED)
    assert _item(item_id).state == "open"


@pytest.mark.asyncio
async def test_a_check_that_appears_or_reruns_green_needs_a_new_evaluation(recorded, gh):
    gh.runs = [_run("Tests", run_id=10, suite=1)]
    item_id = await _board(_pr_bar())
    await _evaluate(item_id)
    gh.runs = [_run("Tests", run_id=10, suite=1), _run("Extra", run_id=30, suite=4)]
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_TARGET_CHANGED)
    gh.runs = [_run("Tests", run_id=11, suite=1)]
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_TARGET_CHANGED)


def test_the_receipt_names_each_attempt_its_source_and_its_suite():
    gh = _GitHub()
    gh.runs = [_run("Tests", run_id=10, suite=5, app="ci-app")]
    gh.statuses = [{"id": 77, "context": "deploy", "state": "success", "creator": {"login": "bot"}}]
    seen = wa.observe(_pr_bar(), gh=gh)
    assert seen.verdict == "pass"
    rows = {r["name"]: r for r in seen.sources["current"]["rows"]}
    assert rows["Tests"] == {
        "kind": "check_run",
        "source": "ci-app",
        "suite": 5,
        "name": "Tests",
        "id": 10,
        "result": "success",
        "state": "passing",
    }
    assert rows["deploy"]["kind"] == "status" and rows["deploy"]["id"] == 77
    assert rows["deploy"]["source"] == "bot"


# ── review P2: an uncounted or foreign board is never complete ────────────


@pytest.mark.parametrize("damage", ["no_total", "run_other_sha", "status_other_sha"])
def test_an_uncounted_or_foreign_board_is_an_error(damage):
    gh = _GitHub()
    if damage == "no_total":
        gh.omit_total = True
    elif damage == "run_other_sha":
        gh.runs = [_run("Tests", run_id=10, head=H2)]
    else:
        gh.status_sha = H2
    seen = wa.observe(_pr_bar(), gh=gh)
    assert seen.verdict == "error", seen


def test_pr_checks_without_a_repo_is_an_error_not_a_guess():
    seen = wa.observe({"kind": "pr_checks", "pr": PR}, gh=_GitHub())
    assert seen.verdict == "error"


@pytest.mark.asyncio
async def test_a_fail_counts_toward_fails(recorded, gh):
    gh.runs = [_run("ci / test", "failure", run_id=1)]
    item_id = await _board(_pr_bar())
    await _evaluate(item_id)
    await _evaluate(item_id)
    assert _item(item_id).fails == 2


@pytest.mark.asyncio
async def test_cmd_is_still_refused_and_runs_nothing(recorded, gh):
    item_id = await _board({"kind": "cmd", "argv": ["true"]})
    code, body = await _evaluate(item_id)
    assert code == 200, body
    assert body["item"]["evidence"]["verdict"] == "refused"
    assert gh.calls == []


@pytest.mark.asyncio
async def test_human_approval_stays_pending_and_cannot_be_accepted(recorded, gh):
    item_id = await _board({"kind": "human_approval"})
    _code, body = await _evaluate(item_id)
    assert body["item"]["evidence"]["verdict"] == "pending"
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_HUMAN_APPROVAL_UNSUPPORTED)
    code, body = await _record(
        CONDUCTOR, {"action": "close", "item_id": item_id, "state": "rejected"}
    )
    assert code == 200, body


# ── A4–A6: anything that moved retires the evidence ───────────────────────


@pytest.mark.asyncio
async def test_a_new_head_after_the_evaluation_is_not_accepted(recorded, gh):
    item_id = await _board(_pr_bar())
    await _evaluate(item_id)
    gh.head = H2
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_TARGET_CHANGED)
    assert _item(item_id).state == "open"
    assert _item(item_id).acceptance_proof is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rerun", [dict(conclusion=None, status="in_progress"), dict(conclusion="failure")]
)
async def test_a_rerun_on_the_same_head_that_is_not_green_retires_the_pass(recorded, gh, rerun):
    item_id = await _board(_pr_bar())
    await _evaluate(item_id)
    gh.runs = [_run("ci / test", run_id=101, **rerun)]
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_TARGET_CHANGED)


@pytest.mark.asyncio
async def test_promoting_a_new_criterion_retires_the_evidence(recorded, gh):
    item_id = await _board(_pr_bar())
    await _evaluate(item_id)
    code, _ = await _record(
        CONDUCTOR,
        {"action": "accept", "item_id": item_id, "acceptance": {**_pr_bar(), "pr": PR + 1}},
    )
    assert code == 200
    assert wl.evidence_is_current(_item(item_id)) is False
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_EVALUATION_STALE)


@pytest.mark.asyncio
async def test_a_criterion_changed_and_changed_back_does_not_revive_old_evidence(recorded, gh):
    """Review P2: A -> B -> A used to make the evidence taken under A current again."""
    item_id = await _board(_pr_bar())
    await _evaluate(item_id)
    for bar in ({**_pr_bar(), "pr": PR + 1}, _pr_bar()):
        code, _ = await _record(
            CONDUCTOR, {"action": "accept", "item_id": item_id, "acceptance": bar}
        )
        assert code == 200
    assert _item(item_id).acceptance == _pr_bar()
    assert wl.evidence_is_current(_item(item_id)) is False
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_EVALUATION_STALE)


@pytest.mark.asyncio
async def test_a_new_worker_report_retires_the_evidence(recorded, gh):
    item_id = await _board(_pr_bar())
    await _evaluate(item_id)
    code, _ = await _report(WORKER, {"status": "done", "summary": "one more commit", "pr": PR})
    assert code == 200
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_EVALUATION_STALE)


def test_an_identical_report_in_the_same_second_still_retires_the_evidence(monkeypatch):
    """Review P2: same words in the same second used to leave the old evidence current."""
    monkeypatch.setattr(wl, "_now_iso", lambda: "2026-09-28T07:00:00-03:00")
    item_id = _store_board(_pr_bar())
    wl.apply_evaluation(
        CONDUCTOR,
        item_id,
        context=wl.evaluation_context(_item(item_id)),
        observation=_passing({"kind": "git_sha", "sha": H1}),
    )
    assert wl.evidence_is_current(_item(item_id))
    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="s")
    assert wl.evidence_is_current(_item(item_id)) is False


# ── review P1-3: the file root is the one admitted at bind ────────────────


@pytest.mark.asyncio
async def test_changed_file_bytes_after_the_evaluation_are_not_accepted(
    recorded, tmp_path, pinned_walk
):
    project = tmp_path / "proj"
    project.mkdir()
    target = project / "out.txt"
    target.write_text("v1", encoding="utf-8")
    item_id = await _board({"kind": "file", "path": "out.txt"}, project=str(project))
    assert _item(item_id).admitted_root["path"] == os.path.realpath(project)
    code, body = await _evaluate(item_id)
    assert code == 200, body
    revision = body["item"]["evidence"]["revision"]
    assert revision["kind"] == "file_sha256" and revision["size"] == 2
    target.write_text("v2 changed", encoding="utf-8")
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_TARGET_CHANGED)
    target.write_text("v1", encoding="utf-8")
    code, body = await _accept(item_id)
    assert code == 200, body
    assert _item(item_id).acceptance_proof["revision"] == revision


@requires_symlinks
@pytest.mark.asyncio
async def test_a_project_root_swapped_for_a_link_after_bind_reads_nothing(
    recorded, tmp_path, pinned_walk
):
    """The worker renames its project and puts a link to another tree in its place:
    the evaluator used to adopt the link's target as the admitted root."""
    project = tmp_path / "proj"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "out.txt").write_text("not the worker's", encoding="utf-8")
    item_id = await _board({"kind": "file", "path": "out.txt"}, project=str(project))
    project.rename(tmp_path / "proj-moved")
    project.symlink_to(elsewhere, target_is_directory=True)
    code, body = await _evaluate(item_id)
    assert code == 200, body
    assert body["item"]["evidence"]["verdict"] == "refused"
    project.unlink()
    (tmp_path / "other").mkdir()
    (tmp_path / "other").rename(project)  # same path, different directory
    (project / "out.txt").write_text("still not it", encoding="utf-8")
    _code, body = await _evaluate(item_id)
    assert body["item"]["evidence"]["verdict"] == "refused"
    assert "not the one admitted" in body["item"]["evidence"]["diagnostic"]


@requires_symlinks
def test_file_reads_are_confined_to_the_admitted_root(tmp_path, pinned_walk):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    (project / "link").symlink_to(outside)
    (project / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    (project / "adir").mkdir()
    root = wa.admitted_root_for(str(project))
    for path in (str(outside), "../secret.txt", "link", "dirlink/secret.txt", "adir"):
        seen = wa.observe({"kind": "file", "path": path}, file_root=root)
        assert seen.verdict == "refused", (path, seen)
    assert wa.observe({"kind": "file", "path": "x"}, file_root=None).verdict == "refused"


def test_file_absence_is_observed_without_a_content_hash(tmp_path, pinned_walk):
    project = tmp_path / "proj"
    project.mkdir()
    root = wa.admitted_root_for(str(project))
    seen = wa.observe({"kind": "file", "path": "gone.txt", "exists": False}, file_root=root)
    assert seen.verdict == "pass"
    assert seen.revision["kind"] == "file_absent" and "sha256" not in seen.revision
    assert wa.observe({"kind": "file", "path": "gone.txt"}, file_root=root).verdict == "fail"


def test_file_evaluation_refuses_without_pinned_walk(tmp_path, monkeypatch):
    root = wa.admitted_root_for(str(tmp_path))
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    reader = MagicMock(side_effect=AssertionError("an unsupported reader must not run"))
    monkeypatch.setattr(wa, "_read_under_root", reader)
    for exists in (True, False):
        seen = wa.observe({"kind": "file", "path": "out.txt", "exists": exists}, file_root=root)
        assert seen.verdict == "refused"
        assert "this platform cannot confine" in seen.diagnostic
        assert seen.revision == {}
    reader.assert_not_called()


# ── A6/A7: late, out-of-order and concurrent results ──────────────────────


@pytest.mark.asyncio
async def test_a_result_that_lands_after_its_criterion_moved_is_not_published(recorded, gh):
    item_id = await _board(_pr_bar())
    gh.during = lambda: wl.apply_acceptance_update(
        CONDUCTOR, item_id, acceptance={**_pr_bar(), "pr": PR + 1}
    )
    code, body = await _evaluate(item_id)
    assert (code, body["code"]) == (409, wl.CODE_EVALUATION_STALE)
    assert _item(item_id).evidence is None


def test_an_older_evaluation_finishing_last_does_not_replace_the_current_one():
    item_id = _store_board(_pr_bar())
    captured = wl.evaluation_context(_item(item_id))
    newer = wa.Observation(verdict="fail", policy=wa.POLICY_PR_CHECKS).to_dict()
    wl.apply_evaluation(CONDUCTOR, item_id, context=captured, observation=newer)
    with pytest.raises(wl.WorkLedgerError) as exc:
        wl.apply_evaluation(
            CONDUCTOR, item_id, context=captured, observation=_passing({"kind": "git_sha"})
        )
    assert exc.value.code == wl.CODE_EVALUATION_STALE
    assert _item(item_id).evidence["verdict"] == "fail"


@pytest.mark.asyncio
async def test_an_abandon_during_the_close_observation_wins_and_is_not_reopened(recorded, gh):
    item_id = await _board(_pr_bar())
    await _evaluate(item_id)
    gh.during = lambda: wl.apply_conductor_action(
        CONDUCTOR, "close", item_id=item_id, state="abandoned"
    )
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_ITEM_CLOSED)
    assert _item(item_id).state == "abandoned"
    assert _item(item_id).acceptance_proof is None


def test_an_evaluation_after_rejection_does_not_reopen_the_item():
    item_id = _store_board(_pr_bar())
    captured = wl.evaluation_context(_item(item_id))
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="rejected")
    with pytest.raises(wl.WorkLedgerError) as exc:
        wl.apply_evaluation(
            CONDUCTOR, item_id, context=captured, observation=_passing({"kind": "git_sha"})
        )
    assert exc.value.code == wl.CODE_ITEM_CLOSED
    assert _item(item_id).state == "rejected"


def test_the_store_refuses_an_accepted_close_without_a_gateway_observation():
    item_id = _store_board(_pr_bar())
    passing = _passing({"kind": "git_sha", "sha": H1})
    wl.apply_evaluation(
        CONDUCTOR, item_id, context=wl.evaluation_context(_item(item_id)), observation=passing
    )
    evidence_id = _item(item_id).evidence["evidence_id"]
    wl.mark_evidence_recorded(CONDUCTOR, item_id, evidence_id)
    for kwargs in (
        {},
        {"expected_evidence_id": evidence_id},
        {"close_observation": passing, "expected_evidence_id": "0" * 16},
    ):
        with pytest.raises(wl.WorkLedgerError) as exc:
            wl.apply_conductor_action(
                CONDUCTOR, "close", item_id=item_id, state="accepted", **kwargs
            )
        assert exc.value.code in (wl.CODE_EVIDENCE_REQUIRED, wl.CODE_EVALUATION_STALE)
    assert _item(item_id).state == "open"


# ── review P1-1: only evidence the record holds can authorise acceptance ───


@pytest.mark.asyncio
async def test_evidence_the_crew_log_never_received_cannot_authorise_acceptance(recorded, gh):
    """A gateway that died between the cache commit and the append left a usable
    pass behind: the close accepted, and a rebuild then showed accepted with no
    evidence. Evidence committed to the cache alone must not close anything."""
    item_id = await _board(_pr_bar())
    seen = wa.observe(_pr_bar(), gh=gh)
    wl.apply_evaluation(
        CONDUCTOR,
        item_id,
        context=wl.evaluation_context(_item(item_id)),
        observation=seen.to_dict(),
    )
    assert _item(item_id).evidence["verdict"] == "pass"
    assert _item(item_id).evidence_recorded == ""
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_EVIDENCE_REQUIRED)
    assert _item(item_id).state == "open"
    assert not any(d.get("action") == "close" for _u, d in recorded)
    # A missing evaluation does not prevent replacing it when its inputs are recorded.
    assert (await _evaluate(item_id))[0] == 200
    assert (await _accept(item_id))[0] == 200


def test_a_newer_unrecorded_evaluation_does_not_inherit_the_old_stamp():
    item_id = _store_board(_pr_bar())
    passing = _passing({"kind": "git_sha", "sha": H1})
    wl.apply_evaluation(
        CONDUCTOR, item_id, context=wl.evaluation_context(_item(item_id)), observation=passing
    )
    first = _item(item_id).evidence["evidence_id"]
    assert wl.mark_evidence_recorded(CONDUCTOR, item_id, first) is True
    wl.apply_evaluation(
        CONDUCTOR, item_id, context=wl.evaluation_context(_item(item_id)), observation=passing
    )
    assert _item(item_id).evidence_recorded == ""
    assert wl.mark_evidence_recorded(CONDUCTOR, item_id, first) is False
    assert wl.accepted_close_refusal(_item(item_id)).code == wl.CODE_EVIDENCE_REQUIRED


@pytest.mark.asyncio
async def test_crew_log_off_refuses_evaluate_and_records_nothing(monkeypatch, gh):
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)
    item_id = await _board(_pr_bar())
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: False)
    code, body = await _evaluate(item_id)
    assert (code, body["code"]) == (409, "crew_log_off")
    assert _item(item_id).evidence is None
    assert gh.calls == []


@pytest.mark.asyncio
async def test_an_unrecorded_evaluation_or_accept_is_undone(monkeypatch, gh):
    _real_units(monkeypatch)
    append = routes.crew_log_emit.on_work_recorded
    lands = {"ok": True}
    monkeypatch.setattr(
        routes.crew_log_emit,
        "on_work_recorded",
        lambda unit, data: append(unit, data) if lands["ok"] else False,
    )
    item_id = await _board(_pr_bar())
    lands["ok"] = False
    code, body = await _evaluate(item_id)
    assert (code, body["code"]) == (503, "crew_log_unrecorded")
    assert _item(item_id).evidence is None
    lands["ok"] = True
    code, _ = await _evaluate(item_id)
    assert code == 200
    lands["ok"] = False
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (503, "crew_log_unrecorded")
    assert _item(item_id).state == "open" and _item(item_id).acceptance_proof is None


@pytest.mark.asyncio
async def test_a_repeated_close_after_acceptance_changes_nothing(recorded, gh):
    item_id = await _board(_pr_bar())
    await _evaluate(item_id)
    code, _ = await _accept(item_id)
    assert code == 200
    before = wl.item_path(CONDUCTOR, item_id).read_bytes()
    gh.head = H2
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_ITEM_CLOSED)
    assert wl.item_path(CONDUCTOR, item_id).read_bytes() == before


# ── A7/A8: the record, the rebuild, and legacy history ────────────────────


def _real_units(monkeypatch) -> None:
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    units = {CONDUCTOR: "u-conductor", WORKER: "u-worker", OTHER: "u-other"}
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: units[key])
    for slot, unit in units.items():
        CrewLog.create(lg.KIND_SESSION, unit, owner="o", agent="kirocrew", slot=slot)

    def _append(unit: str, data: dict[str, Any]) -> bool:
        projection.open_session_log(unit).append("work/recorded", data, src="gateway")
        return True

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _append)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["accept", "report"])
async def test_an_unrecorded_input_cannot_be_legitimised_by_evaluation(monkeypatch, gh, mutation):
    _real_units(monkeypatch)
    original = {"kind": "human_approval"} if mutation == "accept" else _pr_bar()
    item_id = await _board(original, done=mutation == "accept")
    if mutation == "accept":
        wl.apply_acceptance_update(CONDUCTOR, item_id, acceptance=_pr_bar())
    else:
        code, body = await _report(WORKER, {"status": "progress", "summary": "building"})
        assert code == 200, body
        wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="built", pr=PR)
    # The cache commit survives, but its append does not: the files a death leaves.
    routes._BOARD_LOCKS.clear()
    routes._EVAL_LOCKS.clear()
    before = wl.item_path(CONDUCTOR, item_id).read_bytes()
    code, body = await _evaluate(item_id)
    assert (code, body["code"]) == (409, wl.CODE_CREW_LOG_INCOMPLETE)
    assert gh.calls == []
    assert wl.item_path(CONDUCTOR, item_id).read_bytes() == before
    assert wl.cache_dirty(CONDUCTOR) is None  # no destructive repair on a read

    # Re-recording the input establishes its canonical version without dropping data.
    if mutation == "accept":
        code, body = await _record(
            CONDUCTOR, {"action": "accept", "item_id": item_id, "acceptance": _pr_bar()}
        )
    else:
        code, body = await _report(WORKER, {"status": "done", "summary": "built", "pr": PR})
    assert code == 200, body
    code, body = await _evaluate(item_id)
    assert code == 200, body
    code, body = await _accept(item_id)
    assert code == 200, body
    accepted = _item(item_id)
    wl.item_path(CONDUCTOR, item_id).unlink()
    wl.rebuild_from_projection(CONDUCTOR)
    rebuilt = _item(item_id)
    assert rebuilt.state == "accepted" and rebuilt.acceptance == _pr_bar()
    assert rebuilt.evidence == accepted.evidence and wl.evidence_is_current(rebuilt)
    assert (rebuilt.criterion_version, rebuilt.submission_version) == (
        accepted.criterion_version,
        accepted.submission_version,
    )


@pytest.mark.asyncio
async def test_recorded_evidence_cannot_cover_an_unrecorded_criterion(monkeypatch, gh):
    _real_units(monkeypatch)
    item_id = await _board({"kind": "human_approval"})
    wl.apply_acceptance_update(CONDUCTOR, item_id, acceptance=_pr_bar())
    # A stored receipt may itself be canonical while its predecessor is missing.
    item = wl.apply_evaluation(
        CONDUCTOR,
        item_id,
        context=wl.evaluation_context(_item(item_id)),
        observation=wa.observe(_pr_bar(), gh=gh).to_dict(),
    )["item"]
    header = wl.read_conductor(CONDUCTOR)
    assert header is not None
    projection.open_session_log("u-conductor").append(
        "work/recorded",
        routes._work_entry(
            CONDUCTOR,
            actor="conductor",
            by=CONDUCTOR,
            action="evaluate",
            item_id=item_id,
            generation=header.generation,
            evidence=item.evidence,
            verdict=item.verdict,
            fails=item.fails,
        ),
        src="gateway",
    )
    wl.mark_evidence_recorded(CONDUCTOR, item_id, item.evidence["evidence_id"])
    gh.calls.clear()
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_CREW_LOG_INCOMPLETE)
    assert gh.calls == [] and _item(item_id).state == "open"


@pytest.mark.asyncio
async def test_an_evidence_stamp_does_not_replace_the_canonical_receipt(recorded, gh):
    item_id = await _board(_pr_bar())
    item = wl.apply_evaluation(
        CONDUCTOR,
        item_id,
        context=wl.evaluation_context(_item(item_id)),
        observation=wa.observe(_pr_bar(), gh=gh).to_dict(),
    )["item"]
    # The stamp can outlive a log unit; the remaining canonical record has no receipt.
    wl.mark_evidence_recorded(CONDUCTOR, item_id, item.evidence["evidence_id"])
    gh.calls.clear()
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_CREW_LOG_INCOMPLETE)
    assert gh.calls == [] and _item(item_id).state == "open"


@pytest.mark.asyncio
async def test_evaluation_waits_for_a_predecessor_append(recorded, gh, monkeypatch):
    item_id = await _board(_pr_bar())
    board_lock = routes._board_lock(CONDUCTOR)
    waiting = asyncio.Event()

    def traced_lock(key):
        assert key == CONDUCTOR
        waiting.set()
        return board_lock

    def observe_unlocked():
        assert not board_lock.locked(), "CI must not hold the board lock"

    gh.during = observe_unlocked
    monkeypatch.setattr(routes, "_board_lock", traced_lock)
    await board_lock.acquire()
    task = None
    try:
        wl.apply_acceptance_update(CONDUCTOR, item_id, acceptance={**_pr_bar(), "pr": PR + 1})
        task = asyncio.create_task(_evaluate(item_id))
        await asyncio.wait_for(waiting.wait(), timeout=5)
        assert gh.calls == [], "the predecessor's append must finish before observing CI"
        assert not task.done()
        item = _item(item_id)
        header = wl.read_conductor(CONDUCTOR)
        projection.open_session_log("u-conductor").append(
            "work/recorded",
            routes._work_entry(
                CONDUCTOR,
                actor="conductor",
                by=CONDUCTOR,
                action="accept",
                item_id=item_id,
                generation=header.generation,
                acceptance=item.acceptance,
                criterion_version=item.criterion_version,
            ),
            src="gateway",
        )
    finally:
        board_lock.release()
        if task is not None:
            result = await asyncio.wait_for(task, timeout=10)
    code, body = result
    assert code == 200, body
    assert body["item"]["evidence"]["target"]["pr"] == PR + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["evaluate", "close"])
async def test_an_unreadable_canonical_record_refuses_before_observation(
    recorded, gh, monkeypatch, action
):
    item_id = await _board(_pr_bar())
    if action == "close":
        assert (await _evaluate(item_id))[0] == 200
    gh.calls.clear()
    before = wl.item_path(CONDUCTOR, item_id).read_bytes()
    monkeypatch.setattr(
        projection, "read_slot_projection", MagicMock(side_effect=OSError("unreadable"))
    )
    code, body = await (_accept(item_id) if action == "close" else _evaluate(item_id))
    assert (code, body["code"]) == (409, "crew_log_unreadable")
    assert gh.calls == [] and wl.item_path(CONDUCTOR, item_id).read_bytes() == before


@pytest.mark.asyncio
async def test_an_accepted_item_rebuilds_with_the_same_evidence_and_proof(
    monkeypatch, gh, tmp_path
):
    _real_units(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    item_id = await _board(_pr_bar(), project=str(project))
    await _evaluate(item_id)
    code, body = await _accept(item_id)
    assert code == 200, body
    before = _item(item_id)
    os.remove(wl.item_path(CONDUCTOR, item_id))
    counts = wl.rebuild_from_projection(CONDUCTOR)
    assert counts["items"] == 1
    after = _item(item_id)
    assert after.state == "accepted"
    assert after.evidence is not None and after.evidence == before.evidence
    assert after.acceptance_proof == before.acceptance_proof
    assert (after.criterion_version, after.submission_version) == (
        before.criterion_version,
        before.submission_version,
    )
    assert after.admitted_root == before.admitted_root
    assert wl.evidence_is_current(after)


@pytest.mark.asyncio
async def test_a_rebuild_stamps_only_the_evidence_the_log_holds(monkeypatch, gh):
    """Evidence that landed but whose stamp was lost is restored and stamped by a
    rebuild; the item can then be accepted on a fresh observation."""
    _real_units(monkeypatch)
    item_id = await _board(_pr_bar())
    monkeypatch.setattr(routes, "_mark_evidence_recorded", lambda *a: None)
    code, _ = await _evaluate(item_id)
    assert code == 200
    assert _item(item_id).evidence_recorded == ""
    code, body = await _accept(item_id)
    assert (code, body["code"]) == (409, wl.CODE_EVIDENCE_REQUIRED)
    wl.rebuild_from_projection(CONDUCTOR)
    assert _item(item_id).evidence_recorded == _item(item_id).evidence["evidence_id"]
    code, body = await _accept(item_id)
    assert code == 200, body


def test_a_legacy_verdict_is_never_promoted_to_evidence_by_the_fold():
    item = projection._work_new_item("it_0000abcd", 0)
    projection._work_apply(
        item,
        {
            "actor": "conductor",
            "action": "verdict",
            "verdict": "pass",
            "evidence": {"verdict": "pass", "revision": {"kind": "git_sha", "sha": H1}},
        },
        0,
    )
    assert item["verdict"] == "pass"
    assert item["evidence"] is None
    projection._work_apply(
        item,
        {
            "actor": "conductor",
            "action": "close",
            "state": "accepted",
            "evidence": {"verdict": "pass"},
        },
        0,
    )
    assert item["evidence"] is None and item["acceptance_proof"] is None


def test_a_legacy_accepted_item_reads_as_history_with_no_evidence():
    wl.ensure_conductor(CONDUCTOR, goal="g")
    item = wl.apply_conductor_action(CONDUCTOR, "create", title="t", acceptance=_pr_bar())["item"]
    raw = json.loads(wl.item_path(CONDUCTOR, item.item_id).read_text(encoding="utf-8"))
    raw.update({"state": "accepted", "verdict": "pass"})
    for name in ("evidence", "acceptance_proof", "evidence_recorded"):
        raw.pop(name, None)
    wl.item_path(CONDUCTOR, item.item_id).write_text(json.dumps(raw), encoding="utf-8")
    legacy = _item(item.item_id)
    assert (legacy.state, legacy.verdict) == ("accepted", "pass")
    assert legacy.evidence is None and legacy.acceptance_proof is None
    assert wl.evidence_is_current(legacy) is False
