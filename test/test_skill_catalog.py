"""The skill directory snapshot: what a turn pays, and what it is told.

Every test here counts OPERATIONS -- builder invocations, rows retained, the
thread a walk ran on -- and never a duration. A latency assertion would pass on a
fast disk while the defect (a turn waiting for a walk) was still present, and
fail on a loaded CI box while it was not.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew import skill_catalog, skill_trust, skills
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_tools import skills as mcp_skill_tools

# The store is a module singleton -- that is what stops several loaders walking
# one tree -- so this file's tests share process state and stay on one worker.
pytestmark = pytest.mark.xdist_group(name="skill_catalog")

# Every barrier in this file carries a timeout and fails by NAME. A bare
# `event.wait()` turns a broken invariant into a hung shard with no verdict.
BARRIER_TIMEOUT = 10.0


@pytest.fixture(autouse=True)
def clean_store():
    """Give each test a process-level store of its own.

    The store is a module singleton on purpose -- that is what stops several
    loaders walking one tree -- so it has to be torn down between tests, and
    torn down AFTER the test too: a worker still walking would otherwise publish
    into the next test's store.
    """
    skill_catalog.shutdown(wait=True)
    yield
    skill_catalog.shutdown(wait=True)


class _Builder:
    """A fake walk that counts its calls and records the thread it ran on."""

    def __init__(self, rows=None, *, gate: threading.Event | None = None, fail: bool = False):
        self.rows = rows if rows is not None else []
        self.calls = 0
        self.threads: list[str] = []
        self.entered = threading.Event()
        self.gate = gate
        self.fail = fail
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
            self.threads.append(threading.current_thread().name)
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(BARRIER_TIMEOUT), "builder gate never released"
        if self.fail:
            raise OSError("root unreadable")
        return list(self.rows)


class _StreamBuilder:
    """A fake walk that YIELDS, so the store's pull count is observable.

    ``_Builder`` returns a list, which cannot distinguish "retained the cap" from
    "retained everything and trimmed it afterwards". This one counts the rows the
    store actually pulled.
    """

    def __init__(self, rows):
        self.rows = rows
        self.pulled = 0

    def __call__(self):
        def gen():
            for row in self.rows:
                self.pulled += 1
                yield row

        return gen()


def _rows(n: int, start: int = 0):
    return [
        (f"skill-{i}", Path(f"/tmp/skills/skill-{i}/SKILL.md"), None)
        for i in range(start, start + n)
    ]


def _key(project: str = "") -> skill_catalog.CorpusKey:
    return skill_catalog.corpus_key("/tmp/skills", ["/tmp/extra"], project)


def _await_first(key, builder) -> skill_catalog.CatalogSnapshot:
    snapshot = skill_catalog.wait_for_first(key, builder, BARRIER_TIMEOUT)
    assert snapshot is not None, "first build never published"
    return snapshot


def _await_idle(key) -> None:
    """Wait for the worker to finish with *key*, without tearing the store down.

    A test that asserts "the previous listing survived" has to read the store
    AFTER the losing walk finished. ``shutdown`` would answer that question by
    forgetting the corpus, which passes whether or not the guard works.
    """
    deadline = time.monotonic() + BARRIER_TIMEOUT
    while time.monotonic() < deadline:
        if not skill_catalog.corpus_state(key).get("refreshing"):
            return
        time.sleep(0.01)
    raise AssertionError("refresh never finished")


class TestAReadNeverWalks:
    def test_the_first_read_returns_nothing_and_does_not_walk_here(self):
        """Cold: the caller gets no rows and pays no walk.

        The evidence is that the builder never runs on this thread -- not that the
        call was quick. A walk on the caller would also be quick on a fast disk
        with three rows.
        """
        builder = _Builder(_rows(3))
        here = threading.current_thread().name

        assert skill_catalog.read(_key(), builder) is None

        assert builder.entered.wait(BARRIER_TIMEOUT), "no background build was scheduled"
        assert here not in builder.threads

    def test_a_published_listing_is_served_with_no_further_walk(self):
        builder = _Builder(_rows(3))
        _await_first(_key(), builder)
        walks_after_first = builder.calls

        for _ in range(5):
            snapshot = skill_catalog.read(_key(), builder)
            assert snapshot is not None
            assert len(snapshot.entries) == 3

        assert builder.calls == walks_after_first

    def test_an_unverified_listing_is_still_served(self, monkeypatch):
        """The timer decides when to REBUILD, never whether to SERVE.

        This is the regression the whole module exists for: the old cache was a
        deadline, so the turn that found it expired paid the walk. Here the
        expiry schedules a background walk and the caller still gets rows.
        """
        builder = _Builder(_rows(2))
        first = _await_first(_key(), builder)

        gate = threading.Event()
        slow = _Builder(_rows(9), gate=gate)
        monkeypatch.setattr(skill_catalog, "BACKGROUND_REVERIFY_SECS", -1.0)
        try:
            served = skill_catalog.read(_key(), slow)
            assert served is not None
            assert served.generation == first.generation
            assert len(served.entries) == 2
            assert slow.entered.wait(BARRIER_TIMEOUT), "expiry scheduled no rebuild"
        finally:
            gate.set()


class TestOneWalkPerCorpus:
    def test_concurrent_readers_join_one_walk(self):
        """Refresh work must not multiply with the number of sessions."""
        gate = threading.Event()
        builder = _Builder(_rows(4), gate=gate)
        errors: list[BaseException] = []

        def reader():
            try:
                skill_catalog.read(_key(), builder)
            except BaseException as exc:  # noqa: BLE001 -- reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(8)]
        try:
            for thread in threads:
                thread.start()
            assert builder.entered.wait(BARRIER_TIMEOUT), "no build started"
        finally:
            gate.set()
            for thread in threads:
                thread.join(BARRIER_TIMEOUT)

        assert not errors
        assert builder.calls == 1

    def test_two_loaders_over_one_corpus_share_the_listing(self):
        """The old cache lived on the loader, so each loader walked again."""
        builder = _Builder(_rows(5))
        _await_first(_key(), builder)
        walks = builder.calls

        # A second loader over the same roots asks the same question.
        snapshot = skill_catalog.read(_key(), builder)

        assert snapshot is not None
        assert len(snapshot.entries) == 5
        assert builder.calls == walks

    def test_a_different_project_is_a_different_corpus(self):
        """Trust is expressed by WHICH corpus is read, not by filtering one."""
        shared = _Builder(_rows(2))
        scoped = _Builder(_rows(7))
        _await_first(_key(""), shared)
        _await_first(_key("/repo/alpha"), scoped)

        assert len(skill_catalog.read(_key(""), shared).entries) == 2
        assert len(skill_catalog.read(_key("/repo/alpha"), scoped).entries) == 7


class TestAStaleWalkCannotWin:
    def test_a_walk_started_before_an_invalidate_publishes_nothing(self):
        """An in-app edit must not be overwritten by an older in-flight walk."""
        _await_first(_key(), _Builder(_rows(1)))

        gate = threading.Event()
        old = _Builder(_rows(1, start=100), gate=gate)
        skill_catalog.invalidate([_key()])
        skill_catalog.read(_key(), old)
        assert old.entered.wait(BARRIER_TIMEOUT), "no rebuild started"

        # The edit lands while that walk is still inside the builder.
        assert skill_catalog.rebuild_now(_key(), _Builder(_rows(4, start=200)))
        gate.set()
        _await_idle(_key())

        # Read AFTER the superseded walk finished: that is the only order in which
        # a missing generation guard shows up as different rows.
        winner = skill_catalog.read(_key(), _Builder(_rows(0)))
        assert winner is not None
        assert [row[0] for row in winner.entries] == [
            "skill-200",
            "skill-201",
            "skill-202",
            "skill-203",
        ]

    def test_a_mutation_is_visible_to_the_very_next_listing(self):
        _await_first(_key(), _Builder(_rows(1)))

        assert skill_catalog.rebuild_now(_key(), _Builder(_rows(3, start=50)))

        snapshot = skill_catalog.read(_key(), _Builder(_rows(0)))
        assert snapshot is not None
        assert [row[0] for row in snapshot.entries] == ["skill-50", "skill-51", "skill-52"]

    def test_an_invalidate_keeps_serving_the_previous_listing(self):
        first = _await_first(_key(), _Builder(_rows(2)))
        gate = threading.Event()
        slow = _Builder(_rows(6), gate=gate)

        skill_catalog.invalidate([_key()])
        try:
            served = skill_catalog.read(_key(), slow)
            assert served is not None
            assert served.generation == first.generation
            assert len(served.entries) == 2
        finally:
            gate.set()


class TestAFailedWalkCostsTheFreshnessNotTheListing:
    def test_a_raising_walk_leaves_the_previous_listing_in_place(self, monkeypatch):
        _await_first(_key(), _Builder(_rows(3)))
        broken = _Builder(fail=True)
        monkeypatch.setattr(skill_catalog, "BACKGROUND_REVERIFY_SECS", -1.0)

        skill_catalog.read(_key(), broken)
        assert broken.entered.wait(BARRIER_TIMEOUT), "no rebuild started"
        _await_idle(_key())

        # The listing, not just the worker, is what must survive: a failed refresh
        # costs a late discovery and nothing else.
        monkeypatch.setattr(skill_catalog, "BACKGROUND_REVERIFY_SECS", 60.0)
        served = skill_catalog.read(_key(), _Builder(_rows(0)))
        assert served is not None
        assert [row[0] for row in served.entries] == ["skill-0", "skill-1", "skill-2"]
        assert broken.calls == 1

    def test_a_failed_synchronous_rebuild_reports_false(self):
        _await_first(_key(), _Builder(_rows(3)))

        assert skill_catalog.rebuild_now(_key(), _Builder(fail=True)) is False

        snapshot = skill_catalog.read(_key(), _Builder(_rows(3)))
        assert snapshot is not None
        assert len(snapshot.entries) == 3


class TestTheCountBoundIsSaidOutLoud:
    def test_the_walk_stops_pulling_at_the_cap(self, monkeypatch):
        """The cap refuses the tail; it does not retain it and trim afterwards."""
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_SKILLS", 4)
        builder = _StreamBuilder(_rows(50))

        snapshot = _await_first(_key(), builder)

        assert len(snapshot.entries) == 4
        # One row past the cap is what proves a tail exists; 50 would mean the
        # whole population was pulled into memory first.
        assert builder.pulled == 5
        assert snapshot.truncated is True
        assert snapshot.complete is False

    def test_the_refused_tail_is_named_in_the_log(self, monkeypatch, caplog):
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_SKILLS", 4)
        with caplog.at_level("WARNING"):
            _await_first(_key(), _Builder(_rows(7)))
            # The warning is emitted after the snapshot is published, which is
            # after the waiter is released: read it once the worker is idle, or
            # the assertion races the log line.
            _await_idle(_key())

        assert any("refused" in record.getMessage() for record in caplog.records)

    def test_a_superseded_walk_does_not_announce_an_overflow(self, monkeypatch, caplog):
        """Only the published snapshot's overflow is worth an operator's attention."""
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_SKILLS", 4)
        _await_first(_key(), _Builder(_rows(1)))

        gate = threading.Event()
        loser = _Builder(_rows(40), gate=gate)
        skill_catalog.invalidate([_key()])
        skill_catalog.read(_key(), loser)
        assert loser.entered.wait(BARRIER_TIMEOUT), "no rebuild started"

        # A mutation lands while the oversized walk is still inside the builder.
        assert skill_catalog.rebuild_now(_key(), _Builder(_rows(2, start=300)))
        with caplog.at_level("WARNING"):
            gate.set()
            _await_idle(_key())

        assert not [r for r in caplog.records if "refused" in r.getMessage()]
        snapshot = skill_catalog.read(_key(), _Builder(_rows(0)))
        assert snapshot is not None
        assert [row[0] for row in snapshot.entries] == ["skill-300", "skill-301"]
        assert snapshot.truncated is False

    def test_a_listing_within_the_cap_reports_complete(self):
        snapshot = _await_first(_key(), _Builder(_rows(3)))

        assert snapshot.truncated is False
        assert snapshot.complete is True

    @pytest.mark.parametrize("field", ["name", "path", "project"])
    def test_an_overlong_row_is_refused_and_marks_the_snapshot_incomplete(self, monkeypatch, field):
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_NAME_CHARS", 64)
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_PATH_CHARS", 64)
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_PROJECT_KEY_CHARS", 64)
        row = {
            "name": ("x" * 65, Path("/s"), None),
            "path": ("ok", Path("/" + "x" * 65), None),
            "project": ("ok", Path("/s"), "x" * 65),
        }[field]
        builder = _StreamBuilder([row, ("later", Path("/s"), None)])

        snapshot = _await_first(_key(), builder)

        assert snapshot.entries == ()
        assert snapshot.truncated is True
        assert snapshot.complete is False
        assert builder.pulled == 1

    @pytest.mark.parametrize("field", ["root", "extra", "project"])
    def test_an_overlong_corpus_identity_is_refused(self, monkeypatch, field):
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_PATH_CHARS", 8)
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_PROJECT_KEY_CHARS", 8)
        args = {
            "root": ("x" * 9, (), ""),
            "extra": ("/s", ("x" * 9,), ""),
            "project": ("/s", (), "x" * 9),
        }[field]

        with pytest.raises(ValueError, match="filesystem path bound"):
            skill_catalog.corpus_key(*args)

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ({"action": "list", "offset": 1}, "cannot prove the list has ended"),
            ({"action": "search", "query": "missing"}, "absence is not conclusive"),
            ({"action": "read", "key": "missing"}, "UNKNOWN rather than absent"),
        ],
    )
    def test_a_truncated_catalog_never_reports_absence(self, tmp_path, monkeypatch, args, expected):
        root = tmp_path / "skills"
        for name in ("alpha", "beta"):
            path = root / name / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"---\nname: {name}\ndescription: {name}\n---\nbody\n",
                encoding="utf-8",
            )
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_SKILLS", 1)
        loader = skills.SkillsLoader(root, install_builtins=False, config=KiroCrewConfig())
        # Rows are served; what priming refuses is the claim they are all of them.
        assert loader.prime_catalog() is False
        assert loader.catalog_complete() is False
        audit = MagicMock()
        monkeypatch.setattr(
            mcp_skill_tools.mcp_core,
            "require_strict_session_key",
            lambda *_args, **_kwargs: (None, None),
        )
        monkeypatch.setattr(
            mcp_skill_tools.mcp_core,
            "SkillsLoader",
            lambda **_kwargs: loader,
        )
        monkeypatch.setattr(mcp_skill_tools.mcp_core, "sel", lambda: audit)

        result = mcp_skill_tools.skill_search("skill_search", args)

        assert expected in result
        assert audit.log_tool_invocation.call_count == 1


class TestTheTrackedCorporaAreBounded:
    """Rows per corpus is half the population; the other half is corpora.

    A gateway tracks one corpus per trusted project it opens, and no read path
    ever drops one, so the count needs its own bound and its own victim rule.
    """

    def test_the_least_recently_read_corpus_is_forgotten_at_the_cap(self, monkeypatch):
        monkeypatch.setattr(skill_catalog, "MAX_TRACKED_CORPORA", 3)
        for i in range(3):
            _await_first(_key(f"/tmp/p{i}"), _Builder(_rows(1)))
        # Re-read the two newer ones so the first is the oldest by last_read.
        for i in (1, 2):
            assert skill_catalog.read(_key(f"/tmp/p{i}"), _Builder(_rows(1))) is not None

        _await_first(_key("/tmp/p3"), _Builder(_rows(1)))

        assert skill_catalog.stats()["corpora"] == 3
        assert skill_catalog.corpus_state(_key("/tmp/p0"))["known"] is False
        for i in (1, 2, 3):
            assert skill_catalog.corpus_state(_key(f"/tmp/p{i}"))["known"] is True

    def test_a_forgotten_corpus_is_rebuilt_on_the_next_ask(self, monkeypatch):
        """Eviction costs one listing, never an answer: a snapshot is a cache."""
        monkeypatch.setattr(skill_catalog, "MAX_TRACKED_CORPORA", 1)
        _await_first(_key("/tmp/first"), _Builder(_rows(2)))
        _await_first(_key("/tmp/second"), _Builder(_rows(1)))
        assert skill_catalog.corpus_state(_key("/tmp/first"))["known"] is False

        again = _await_first(_key("/tmp/first"), _Builder(_rows(2)))

        assert len(again.entries) == 2

    def test_a_corpus_with_a_waiter_is_not_the_victim(self, monkeypatch):
        """Evicting a corpus somebody is parked on would strand that waiter.

        Its Event is only ever set by this module, so a dropped record means the
        waiter sits out its whole timeout for a listing nobody will publish.
        """
        monkeypatch.setattr(skill_catalog, "MAX_TRACKED_CORPORA", 1)
        gate = threading.Event()
        slow = _Builder(_rows(1), gate=gate)
        waited: list[object] = []

        def wait_cold():
            waited.append(skill_catalog.wait_for_first(_key("/tmp/parked"), slow, BARRIER_TIMEOUT))

        waiter = threading.Thread(target=wait_cold)
        waiter.start()
        try:
            assert slow.entered.wait(BARRIER_TIMEOUT), "cold walk never started"
            # A second corpus arrives while the first still has its waiter.
            skill_catalog.read(_key("/tmp/other"), _Builder(_rows(1)))
            assert skill_catalog.corpus_state(_key("/tmp/parked"))["known"] is True
        finally:
            gate.set()
            waiter.join(timeout=BARRIER_TIMEOUT)

        assert waited and waited[0] is not None, "the parked waiter got no listing"


class TestTheWalkerStopsRetainingAtTheCap:
    """The cap is the walker's too, which is where the memory would go.

    Bounding only at the store leaves the tree materialized once per root before
    anything trims it, so these address the walker directly and then through the
    loader, with the shared constant lowered.
    """

    def _tree(self, root: Path, count: int) -> None:
        for i in range(count):
            path = root / f"skill-{i}" / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"---\nname: skill-{i}\ndescription: d\n---\nbody\n", encoding="utf-8")

    def test_an_unconfined_walk_retains_at_most_the_limit(self, tmp_path):
        root = tmp_path / "skills"
        self._tree(root, 6)

        assert len(skills._iter_skill_files(root, limit=2)) == 2
        assert len(skills._iter_skill_files(root)) == 6

    def test_a_confined_walk_retains_at_most_the_limit(self, tmp_path):
        project = tmp_path / "repo"
        root = project / ".kiro" / "skills"
        self._tree(root, 5)
        confine = (str(project),)

        limited = skills._iter_skill_files(root, confine_to=confine, limit=2)
        complete = skills._iter_skill_files(root, confine_to=confine)
        if skill_trust.project_skill_traversal_supported():
            assert len(limited) == 2
            assert len(complete) == 5
        else:
            # Windows exposes no handle-relative, no-reparse directory walk.
            # Refusing every confined row is the fail-closed product contract:
            # probing these paths by name could initiate UNC authentication.
            assert limited == []
            assert complete == []

    def test_a_corpus_over_the_cap_is_published_short_and_says_so(self, tmp_path, monkeypatch):
        root = tmp_path / "skills"
        self._tree(root, 6)
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_SKILLS", 2)
        monkeypatch.setattr(skill_catalog, "COLD_BUILD_WAIT_SECS", BARRIER_TIMEOUT)
        loader = skills.SkillsLoader(root, install_builtins=False, config=KiroCrewConfig())

        # Priming is complete-or-not, never present-or-not: a short listing is a
        # LOWER BOUND on the corpus, so an ``always: true`` skill past the refused
        # row would be missing from a directory that called itself ready. The
        # rows are still served -- what is refused is the claim that they are all
        # of them.
        assert loader.prime_catalog() is False

        assert len(loader._iter()) == 2
        # Short is REPORTED, never passed off as the whole corpus: a caller that
        # cannot find a skill here must say unknown, not absent.
        assert loader.catalog_complete() is False


class TestTheOneBoundedWait:
    def test_a_timed_out_first_build_reports_no_listing(self):
        """A wait that does not finish must degrade, never raise or hang."""
        gate = threading.Event()
        slow = _Builder(_rows(2), gate=gate)
        try:
            assert skill_catalog.wait_for_first(_key(), slow, 0.05) is None
        finally:
            gate.set()

    def test_a_later_read_needs_no_wait(self):
        builder = _Builder(_rows(2))
        _await_first(_key(), builder)
        walks = builder.calls

        assert skill_catalog.read(_key(), builder) is not None
        assert builder.calls == walks


class TestExitIsNotAFault:
    """A refresh losing a race with interpreter exit is teardown, not a failure."""

    @pytest.mark.parametrize(
        "message",
        [
            # CPython raises both wordings from the same submit() site, and only
            # one appears on any given exit. The first version of this classifier
            # matched the narrower one and therefore caught neither.
            "cannot schedule new futures after shutdown",
            "cannot schedule new futures after interpreter shutdown",
        ],
    )
    def test_both_shutdown_wordings_are_recognised(self, message):
        assert skill_catalog._is_interpreter_teardown(RuntimeError(message)) is True

    def test_a_real_walk_failure_is_not_mistaken_for_exit(self):
        assert skill_catalog._is_interpreter_teardown(OSError("root unreadable")) is False
        assert skill_catalog._is_interpreter_teardown(RuntimeError("pool exploded")) is False

    def test_a_teardown_refresh_is_not_logged_as_a_failure(self, monkeypatch, caplog):
        """The listing survives and nothing warns."""
        _await_first(_key(), _Builder(_rows(3)))

        class _ExitingBuilder(_Builder):
            def __call__(self):
                super().__call__()
                raise RuntimeError("cannot schedule new futures after interpreter shutdown")

        exiting = _ExitingBuilder()
        monkeypatch.setattr(skill_catalog, "BACKGROUND_REVERIFY_SECS", -1.0)
        with caplog.at_level("WARNING"):
            skill_catalog.read(_key(), exiting)
            assert exiting.entered.wait(BARRIER_TIMEOUT), "no rebuild started"
            skill_catalog.shutdown(wait=True)

        assert not [r for r in caplog.records if "refresh of" in r.getMessage()]


class TestShutdownLeavesNothingRunning:
    def test_shutdown_releases_waiters_and_forgets_every_corpus(self):
        _await_first(_key(), _Builder(_rows(2)))
        assert skill_catalog.stats()["published"] == 1

        skill_catalog.shutdown(wait=True)

        stats = skill_catalog.stats()
        assert stats["corpora"] == 0
        assert stats["refreshing"] == 0

    def test_the_store_is_usable_again_after_shutdown(self):
        _await_first(_key(), _Builder(_rows(1)))
        skill_catalog.shutdown(wait=True)

        snapshot = _await_first(_key(), _Builder(_rows(2)))
        assert len(snapshot.entries) == 2


class TestATurnDoesNotWaitForAWalk:
    """The same contract, through the loader that chat turns actually call.

    The module tests above pin the store. These pin the seam: that
    ``SkillsLoader`` reads it instead of walking, that several loaders over one
    tree walk once, and that a cold listing which does not finish is REPORTED
    rather than passed off as a complete directory.
    """

    def _skill(self, root: Path, key: str, *, always: bool = False) -> None:
        path = root / key / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {key}\ndescription: {key} description\n"
            f"always: {'true' if always else 'false'}\n---\nInstruction for {key}\n",
            encoding="utf-8",
        )

    def _loader(self, root: Path):
        return skills.SkillsLoader(root, install_builtins=False, config=KiroCrewConfig())

    def test_a_read_past_the_reverify_window_serves_rows_and_walks_on_the_worker(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "skills"
        self._skill(root, "alpha")
        loader = self._loader(root)
        monkeypatch.setattr(skill_catalog, "COLD_BUILD_WAIT_SECS", BARRIER_TIMEOUT)
        assert loader.prime_catalog() is True

        # Due for re-verification, and the walk is held open. A turn that waited
        # for it would block here; one that serves the snapshot does not.
        monkeypatch.setattr(skill_catalog, "BACKGROUND_REVERIFY_SECS", -1.0)
        gate = threading.Event()
        walked_on: list[str] = []
        real_walk = skills._iter_skill_files

        def blocked(*args, **kwargs):
            walked_on.append(threading.current_thread().name)
            assert gate.wait(BARRIER_TIMEOUT), "walk gate never released"
            return real_walk(*args, **kwargs)

        monkeypatch.setattr(skills, "_iter_skill_files", blocked)
        try:
            names = {name for name, _, _ in loader._iter()}
        finally:
            gate.set()
            _await_idle(loader._corpus_key(""))

        assert names == {"alpha"}
        assert walked_on, "the refresh was never scheduled"
        assert all(name.startswith("skill-catalog") for name in walked_on), walked_on

    def test_a_second_loader_over_one_tree_does_not_walk(self, tmp_path, monkeypatch):
        root = tmp_path / "skills"
        self._skill(root, "alpha")
        first = self._loader(root)
        monkeypatch.setattr(skill_catalog, "COLD_BUILD_WAIT_SECS", BARRIER_TIMEOUT)
        assert first.prime_catalog() is True

        def refuse(*args, **kwargs):
            raise AssertionError("a second loader walked a tree already listed")

        monkeypatch.setattr(skills, "_iter_skill_files", refuse)
        second = self._loader(root)

        assert {name for name, _, _ in second._iter()} == {"alpha"}

    def test_a_cold_listing_that_does_not_finish_states_the_shortfall(self, tmp_path, monkeypatch):
        """An unfinished directory is declared, never rendered as a complete one."""
        root = tmp_path / "skills"
        self._skill(root, "pinned", always=True)
        loader = self._loader(root)

        gate = threading.Event()
        real_walk = skills._iter_skill_files

        def blocked(*args, **kwargs):
            assert gate.wait(BARRIER_TIMEOUT), "walk gate never released"
            return real_walk(*args, **kwargs)

        monkeypatch.setattr(skills, "_iter_skill_files", blocked)
        monkeypatch.setattr(skill_catalog, "COLD_BUILD_WAIT_SECS", 0.05)
        required: list[str] = []
        try:
            loader.get_context(budget=4950, discovery_only=True, required_parts_out=required)
            assert loader.catalog_complete() is False
        finally:
            gate.set()
            _await_idle(loader._corpus_key(""))

        notice = "".join(required)
        assert "not finished" in notice
        assert "UNKNOWN rather than absent" in notice
        # The required instruction is NOT claimed: saying the directory is short is
        # the honest answer, and the next turn serves the finished listing.
        assert "Instruction for pinned" not in notice

    def test_two_builds_on_the_event_loop_render_the_same_directory(self, tmp_path):
        """A coroutine's first build must not claim a shortfall it never measured.

        On the loop there is no thread to park, so the first listing is walked in
        place. Reporting "not finished" instead would put an incomplete-discovery
        notice in the first context of a process and not in the second, and two
        builds of the same session would then differ byte for byte.
        """
        root = tmp_path / "skills"
        self._skill(root, "alpha")
        self._skill(root, "pinned", always=True)
        loader = self._loader(root)

        async def build_twice():
            first: list[str] = []
            second: list[str] = []
            one = loader.get_context(budget=4950, discovery_only=True, required_parts_out=first)
            two = loader.get_context(budget=4950, discovery_only=True, required_parts_out=second)
            return "".join(first) + one, "".join(second) + two

        one, two = asyncio.run(build_twice())

        assert "not finished" not in one
        assert one == two
        assert "Instruction for pinned" in one

    def test_the_finished_listing_delivers_the_required_instruction(self, tmp_path):
        root = tmp_path / "skills"
        self._skill(root, "pinned", always=True)
        loader = self._loader(root)

        required: list[str] = []
        loader.get_context(budget=4950, discovery_only=True, required_parts_out=required)

        assert "Instruction for pinned" in "".join(required)
        assert loader.catalog_complete() is True

    def test_a_refused_tail_is_not_reported_as_a_primed_directory(self, tmp_path, monkeypatch):
        """A truncated listing must report the same shortfall an unfinished one does.

        The walk stops retaining at ``MAX_CATALOG_SKILLS``, so a corpus past the cap
        yields a listing that is present but is only a LOWER BOUND. An
        ``always: true`` skill sitting past the refused row is then absent from a
        directory that called itself ready, which drops a required instruction and
        says nothing -- the one outcome the prime/complete machinery exists to
        prevent. So priming is complete-or-not, never merely present-or-not.
        """
        root = tmp_path / "skills"
        for index in range(4):
            self._skill(root, f"s{index}")
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_SKILLS", 2)
        loader = self._loader(root)

        assert loader.catalog_complete() is False
        assert loader.prime_catalog() is False

        required: list[str] = []
        loader.get_context(budget=4950, discovery_only=True, required_parts_out=required)
        assert "incomplete" in "".join(required)

    def test_extra_roots_past_the_bound_are_refused_not_merged(self, tmp_path, monkeypatch):
        """Two different root sets must never share one corpus key.

        ``corpus_key`` refuses an over-long extra-root list rather than truncating
        it, because a dropped root would make two distinct root sets hash to one
        key and a corpus would then serve a listing built over roots the caller
        never asked about. The loader applies the bound where it ADOPTS the roots,
        so that refusal is an invariant guard rather than an error a turn can take.
        """
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_EXTRA_ROOTS", 2)
        with pytest.raises(ValueError, match="extra roots exceed"):
            skill_catalog.corpus_key(
                tmp_path / "skills", [tmp_path / "a", tmp_path / "b", tmp_path / "c"]
            )

    def test_a_loader_bounds_its_adopted_roots_so_a_turn_never_raises(self, tmp_path, monkeypatch):
        """An over-configured root list degrades to a shorter search, not a failure.

        Without the bound at adoption, every listing on such a host would raise
        inside ``corpus_key`` -- so the config could turn one operator typo into a
        broken turn. The roots past the bound are dropped and named in the log.
        """
        root = tmp_path / "skills"
        self._skill(root, "alpha")
        extras = []
        for index in range(5):
            extra = tmp_path / f"extra{index}"
            (extra / f"e{index}").mkdir(parents=True)
            (extra / f"e{index}" / "SKILL.md").write_text(
                f"---\nname: e{index}\ndescription: d\n---\nbody\n", encoding="utf-8"
            )
            extras.append(str(extra))
        monkeypatch.setattr(skill_catalog, "MAX_CATALOG_EXTRA_ROOTS", 2)
        cfg = KiroCrewConfig()
        cfg.skills.extra_paths = extras
        loader = skills.SkillsLoader(root, install_builtins=False, config=cfg)

        # The listing answers instead of raising, and the corpus key it built is
        # within the bound.
        assert loader.prime_catalog() is True
        assert len(loader._corpus_key()[1]) <= 2
