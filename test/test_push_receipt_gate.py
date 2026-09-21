"""Tests for the prepare-pr push-receipt gate.

The publish floor refuses a publish in an ENROLLED worktree whose current ``HEAD``
carries no receipt from the prepare-pr push guard. What each test here pins, and why
the split matters, is the gate's fail direction: a definite answer (no receipt, a
receipt for another commit, a receipt recorded against another worktree) refuses,
while anything the check could not judge (an unresolvable ``HEAD``, an unreadable
policy) allows -- because this runs on the global publish path, where a wrong refusal
wedges every repository the agent touches.

Every test drives the real ``is_denied`` and the real receipt store through an
isolated data home. No test runs ``git``: the reader resolves ``HEAD`` by reading the
files git itself writes, so a fixture that writes those files exercises the production
path exactly, and a subprocess would only add a binary dependency and a stall.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew import security
from kiro_crew.security import push_receipt

# "git pus" + "h" keeps a literal blocked command out of the test source.
PUSH = "git pus" + "h"
FEATURE_PUBLISH = f"{PUSH} origin my-feature"

SHA_A = "a" * 40
SHA_B = "b" * 40
#: Where the fixture's ``origin/main`` sits, and where it moves to when a test advances
#: the base. Distinct from the HEAD shas so a test cannot pass by confusing the two.
BASE_SHA = "c" * 40
MOVED_BASE_SHA = "d" * 40


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the data home at a temp directory for the whole check.

    ``config_dir()`` memoizes on the raw override value, so setting it to a path no
    previous test used is what makes the memo miss; no cache reaching is needed.
    """
    target = tmp_path / "crew-home"
    target.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(target))
    monkeypatch.delenv(push_receipt.OVERRIDE_ENV, raising=False)
    return target


@pytest.fixture()
def captured_sel_events(monkeypatch):
    """Capture SEL events without real I/O (isolate the ambient forensic log)."""
    events = []

    class _Capture:
        def log(self, event) -> None:
            events.append(event)

    monkeypatch.setattr(security, "SecurityEventLog", lambda: _Capture())
    return events


def make_worktree(root: Path, name: str = "wt", sha: str = SHA_A) -> Path:
    """A main working tree whose ``HEAD`` points at *sha* through a branch ref.

    It also carries ``refs/remotes/origin/main``, because the guard's verdict is about
    a pair -- this commit against that base -- and both halves of the receipt are read
    from the repository rather than taken on the writer's word.
    """
    worktree = root / name
    git_dir = worktree / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "remotes" / "origin").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/topic\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "topic").write_text(sha + "\n", encoding="utf-8")
    (git_dir / "refs" / "remotes" / "origin" / "main").write_text(BASE_SHA + "\n", encoding="utf-8")
    return worktree


def move_base(worktree: Path, sha: str) -> None:
    """Advance (or rewrite) the worktree's ``origin/main``, as a fetch would."""
    git_dir = worktree / ".git"
    if git_dir.is_file():
        git_dir = Path(git_dir.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip())
    ref = git_dir / "refs" / "remotes" / "origin" / "main"
    if not ref.parent.is_dir():
        ref = push_receipt._common_dir(git_dir) / "refs" / "remotes" / "origin" / "main"
    ref.write_text(sha + "\n", encoding="utf-8")


def make_linked_worktree(root: Path, main: Path, name: str, sha: str) -> Path:
    """A LINKED worktree of *main*: its own git dir, its own HEAD, shared refs."""
    git_dir = main / ".git" / "worktrees" / name
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text(sha + "\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = root / name
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    return worktree


def receipt_for(worktree: Path, *, base: str = "main", mode: str = "pre-squash") -> Path:
    """Write a receipt for *worktree* as the guard would: with the pair it just judged.

    The writer takes the revisions as arguments rather than re-reading refs, so a
    fixture has to supply them. Read here from the worktree itself, which is what the
    guard's own ``git rev-parse`` calls do.
    """
    git_dir = push_receipt.git_dir_for(worktree)
    return push_receipt.write_receipt(
        worktree,
        base=base,
        mode=mode,
        head=push_receipt.head_sha(git_dir),
        base_sha=push_receipt._resolve_ref(
            git_dir, push_receipt._common_dir(git_dir), push_receipt.base_ref_for(base)
        ),
    )


def enroll(*worktrees: Path) -> None:
    push_receipt.policy_path().write_text(
        json.dumps({"enrolled": [str(w) for w in worktrees]}), encoding="utf-8"
    )


class TestEnrolledPublishWithAReceipt:
    """The positive case: the gate ran on this commit, so the publish proceeds."""

    def test_a_receipt_for_the_current_head_allows_the_publish(
        self, home, tmp_path, captured_sel_events
    ) -> None:
        worktree = make_worktree(tmp_path)
        enroll(worktree)
        receipt_for(worktree, base="main", mode="pre-squash")

        assert security.is_denied(FEATURE_PUBLISH) is None
        assert [e for e in captured_sel_events if e.event_type == "push_allowed"]

    def test_the_receipt_records_the_head_the_guard_judged(self, home, tmp_path) -> None:
        worktree = make_worktree(tmp_path)
        path = receipt_for(worktree, base="main", mode="single-on-base")
        document = json.loads(path.read_text(encoding="utf-8"))

        assert document["sha"] == SHA_A
        assert document["base_ref"] == "refs/remotes/origin/main"
        assert document["mode"] == "single-on-base"
        assert document["version"] == push_receipt.RECEIPT_VERSION

    def test_a_linked_worktrees_receipt_does_not_authorize_its_sibling(
        self, home, tmp_path
    ) -> None:
        """Two worktrees of one repository sit on different commits.

        Identity is the git directory that holds ``HEAD``, which a linked worktree has
        of its own -- so receipting one leaves the other refused. Keying on the shared
        repository instead would let any sibling's gate run authorize this publish.
        """
        main = make_worktree(tmp_path, "main-tree", SHA_A)
        linked = make_linked_worktree(tmp_path, main, "linked-tree", SHA_B)
        receipt_for(main, base="main", mode="pre-squash")

        assert push_receipt.worktree_verdict(str(main))[0] == push_receipt.VERDICT_OK
        verdict, detail = push_receipt.worktree_verdict(str(linked))
        assert verdict == push_receipt.VERDICT_DENY
        assert "no prepare-pr push receipt" in detail


class TestDefiniteAnswersRefuse:
    """Absent, stale and untrusted receipts are answers, and all three refuse."""

    def test_an_absent_receipt_refuses_and_names_the_gate(
        self, home, tmp_path, captured_sel_events
    ) -> None:
        worktree = make_worktree(tmp_path)
        enroll(worktree)

        reason = security.is_denied(FEATURE_PUBLISH)

        assert reason is not None
        assert "push_guard.py" in reason
        assert str(worktree) in reason
        assert SHA_A[:12] in reason
        assert push_receipt.OVERRIDE_ENV in reason
        assert not [e for e in captured_sel_events if e.event_type == "push_allowed"]
        assert [e for e in captured_sel_events if e.event_type == "deny_event"]

    def test_a_receipt_for_another_commit_refuses_as_stale(self, home, tmp_path) -> None:
        """Amending after the gate must not inherit the old pass."""
        worktree = make_worktree(tmp_path, sha=SHA_A)
        enroll(worktree)
        receipt_for(worktree, base="main", mode="pre-squash")
        (worktree / ".git" / "refs" / "heads" / "topic").write_text(SHA_B + "\n", encoding="utf-8")

        reason = security.is_denied(FEATURE_PUBLISH)

        assert reason is not None
        assert SHA_A[:12] in reason
        assert SHA_B[:12] in reason

    def test_a_receipt_recorded_against_another_worktree_refuses(self, home, tmp_path) -> None:
        """A receipt copied into this worktree's slot is not this worktree's receipt.

        The file name is a digest, so a copy lands under the right name; the recorded
        git directory is what makes the swap detectable.
        """
        mine = make_worktree(tmp_path, "mine", SHA_A)
        theirs = make_worktree(tmp_path, "theirs", SHA_A)
        enroll(mine)
        foreign = receipt_for(theirs, base="main", mode="pre-squash")
        target = push_receipt.receipt_path(push_receipt.git_dir_for(mine))
        target.write_text(foreign.read_text(encoding="utf-8"), encoding="utf-8")

        reason = security.is_denied(FEATURE_PUBLISH)

        assert reason is not None
        assert "another git directory" in reason

    @pytest.mark.parametrize(
        ("body", "why"),
        [
            ("not json at all", "not JSON"),
            ("[]", "not a JSON object"),
            (json.dumps({"version": 99, "gitdir": "x", "sha": SHA_A}), "unknown receipt version"),
        ],
    )
    def test_a_receipt_that_is_not_a_receipt_refuses(self, home, tmp_path, body, why) -> None:
        """Readable-but-wrong content is a definite "no valid receipt", not an error.

        Treating it as unjudgeable would make writing garbage into the store the
        cheapest possible bypass of the gate.
        """
        worktree = make_worktree(tmp_path)
        enroll(worktree)
        path = push_receipt.receipt_path(push_receipt.git_dir_for(worktree))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

        verdict, detail = push_receipt.worktree_verdict(str(worktree))

        assert verdict == push_receipt.VERDICT_DENY
        assert why in detail


class TestNotOptedInCostsNothing:
    """A repository that did not enrol is not refused, and is not even looked up."""

    def test_no_policy_file_allows_the_publish(self, home, tmp_path, captured_sel_events) -> None:
        make_worktree(tmp_path)
        assert not push_receipt.policy_path().exists()

        assert security.is_denied(FEATURE_PUBLISH) is None
        assert [e for e in captured_sel_events if e.event_type == "push_allowed"]

    def test_no_policy_file_reaches_no_receipt_store(self, home, monkeypatch) -> None:
        """The store is never touched without an enrollment.

        A store read that happened anyway would be both a cost the opt-out promises not
        to pay and a place for the check to fail in a repository that never asked for
        it, so the probe raises rather than counting calls.
        """

        def _explode(*_args, **_kwargs):
            raise AssertionError("the receipt store was reached without an enrollment")

        monkeypatch.setattr(push_receipt, "receipt_path", _explode)
        monkeypatch.setattr(push_receipt, "git_dir_for", _explode)

        assert security.is_denied(FEATURE_PUBLISH) is None

    def test_a_command_that_is_not_a_publish_never_reaches_the_check(
        self, home, monkeypatch
    ) -> None:
        """The check hangs off the publish branch, so ordinary commands pay nothing."""

        def _explode():
            raise AssertionError("the receipt check ran for a non-publish command")

        monkeypatch.setattr(security, "_push_receipt_denial", _explode)

        assert security.is_denied("ls -la") is None
        assert security.is_denied("git status") is None
        assert security.is_denied("git stash push -m wip") is None


class TestInternalErrorsAllow:
    """Anything that is not an answer about a receipt allows the publish."""

    def test_an_unresolvable_head_allows_the_publish(self, home, tmp_path, caplog) -> None:
        """An enrolled path that is not a worktree cannot answer, so it must not refuse.

        This is the direction that matters most: a stale enrollment entry (a worktree
        the operator removed) would otherwise refuse every publish on the host.
        """
        missing = tmp_path / "gone"
        enroll(missing)

        with caplog.at_level("WARNING"):
            assert security.is_denied(FEATURE_PUBLISH) is None
        assert "could not judge" in caplog.text

    def test_an_unreadable_receipt_allows_the_publish(self, home, tmp_path, monkeypatch) -> None:
        worktree = make_worktree(tmp_path)
        enroll(worktree)
        real = push_receipt._read_text_capped

        def _fail(path, cap):
            if path.name.endswith(".json") and push_receipt.RECEIPT_DIR_NAME in str(path):
                raise PermissionError(13, "denied")
            return real(path, cap)

        monkeypatch.setattr(push_receipt, "_read_text_capped", _fail)

        assert push_receipt.worktree_verdict(str(worktree))[0] == push_receipt.VERDICT_UNKNOWN
        assert security.is_denied(FEATURE_PUBLISH) is None

    def test_a_malformed_policy_allows_the_publish(self, home, tmp_path, caplog) -> None:
        make_worktree(tmp_path)
        push_receipt.policy_path().write_text("{ not json", encoding="utf-8")

        with caplog.at_level("WARNING"):
            assert security.is_denied(FEATURE_PUBLISH) is None
        assert "policy could not be read" in caplog.text

    def test_an_unimportable_receipt_module_allows_the_publish(self, home, monkeypatch) -> None:
        """The wrapper's import failure is an internal error like any other."""
        monkeypatch.setitem(sys.modules, "kiro_crew.security.push_receipt", None)

        assert security._push_receipt_denial() is None


class TestAuditedOverride:
    """The escape hatch for when the receipt mechanism is itself the broken part."""

    def test_the_override_allows_the_publish_and_is_audited(
        self, home, tmp_path, monkeypatch, captured_sel_events
    ) -> None:
        worktree = make_worktree(tmp_path)
        enroll(worktree)
        monkeypatch.setenv(push_receipt.OVERRIDE_ENV, "store on a read-only volume")

        assert security.is_denied(FEATURE_PUBLISH) is None

        overrides = [
            e for e in captured_sel_events if e.event_type == push_receipt.OVERRIDE_EVENT_TYPE
        ]
        assert len(overrides) == 1
        assert overrides[0].outcome == "allowed"
        assert overrides[0].metadata["reason"] == "store on a read-only volume"
        assert overrides[0].metadata["mechanism"] == "PUSH_RECEIPT_OVERRIDE"

    def test_an_empty_override_is_no_override(
        self, home, tmp_path, monkeypatch, captured_sel_events
    ) -> None:
        """A blank value is not an explicit act, so it neither allows nor audits."""
        worktree = make_worktree(tmp_path)
        enroll(worktree)
        monkeypatch.setenv(push_receipt.OVERRIDE_ENV, "   ")

        assert security.is_denied(FEATURE_PUBLISH) is not None
        assert not [
            e for e in captured_sel_events if e.event_type == push_receipt.OVERRIDE_EVENT_TYPE
        ]

    def test_a_failed_override_audit_still_overrides(
        self, home, tmp_path, monkeypatch, caplog
    ) -> None:
        """An audit failure must not become a refusal: the operator asked explicitly."""
        worktree = make_worktree(tmp_path)
        enroll(worktree)
        monkeypatch.setenv(push_receipt.OVERRIDE_ENV, "why")

        def _fail():
            raise OSError("log is gone")

        monkeypatch.setattr(security, "SecurityEventLog", _fail)

        with caplog.at_level("WARNING"):
            assert push_receipt.denial_reason() is None
        assert push_receipt.OVERRIDE_EVENT_TYPE in caplog.text


class TestStorage:
    """Atomic write, strict permissions, and a name nothing else chooses."""

    def test_the_write_leaves_no_temporary_behind(self, home, tmp_path) -> None:
        worktree = make_worktree(tmp_path)
        receipt_for(worktree, base="main", mode="pre-squash")
        receipt_for(worktree, base="main", mode="pre-squash")

        leftovers = [p.name for p in push_receipt.receipt_dir().iterdir() if ".tmp" in p.name]
        assert leftovers == []
        assert len(list(push_receipt.receipt_dir().iterdir())) == 1

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
    def test_the_receipt_and_its_directory_are_not_group_or_world_readable(
        self, home, tmp_path
    ) -> None:
        worktree = make_worktree(tmp_path)
        path = receipt_for(worktree, base="main", mode="pre-squash")

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    def test_the_receipt_path_is_derived_from_the_git_directory(self, home, tmp_path) -> None:
        """Not from the worktree path: two paths to one git dir are one worktree."""
        worktree = make_worktree(tmp_path)
        git_dir = push_receipt.git_dir_for(worktree)

        assert push_receipt.receipt_path(git_dir).name.startswith(
            push_receipt.repo_identity(git_dir)
        )
        assert push_receipt.receipt_path(git_dir).parent == push_receipt.receipt_dir()


class TestHeadResolution:
    """``HEAD`` is read the way git writes it, including the shapes that must refuse."""

    def test_a_detached_head_resolves(self, home, tmp_path) -> None:
        worktree = make_worktree(tmp_path)
        (worktree / ".git" / "HEAD").write_text(SHA_B + "\n", encoding="utf-8")

        assert push_receipt.head_sha(push_receipt.git_dir_for(worktree)) == SHA_B

    def test_a_packed_ref_resolves(self, home, tmp_path) -> None:
        worktree = make_worktree(tmp_path)
        (worktree / ".git" / "refs" / "heads" / "topic").unlink()
        (worktree / ".git" / "packed-refs").write_text(
            f"# pack-refs with: peeled\n{SHA_B} refs/heads/topic\n", encoding="utf-8"
        )

        assert push_receipt.head_sha(push_receipt.git_dir_for(worktree)) == SHA_B

    @pytest.mark.parametrize(
        "head",
        [
            "ref: ../../../etc/passwd",
            "ref: refs/heads/../../../../etc/passwd",
            "not-a-sha",
            "",
        ],
    )
    def test_an_unusable_head_raises_rather_than_answering(self, home, tmp_path, head) -> None:
        """A planted ``HEAD`` must not turn a ref read into an arbitrary file read.

        Raising is what routes these to UNKNOWN, which allows -- correct, because a
        ``HEAD`` this reader cannot parse is not evidence about a receipt.
        """
        worktree = make_worktree(tmp_path)
        (worktree / ".git" / "HEAD").write_text(head, encoding="utf-8")

        with pytest.raises((OSError, ValueError)):
            push_receipt.head_sha(push_receipt.git_dir_for(worktree))


class TestPushGuardWritesWhatTheFloorReads:
    """The writer lives in a skill script and the reader in the floor: pin the seam."""

    @staticmethod
    def _guard():
        path = (
            Path(kiro_crew.__file__).resolve().parent
            / "builtin_skills"
            / "kirocrew-dev"
            / "prepare-pr"
            / "scripts"
            / "push_guard.py"
        )
        spec = importlib.util.spec_from_file_location("push_guard_under_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_guards_receipt_satisfies_the_floor(self, home, tmp_path, monkeypatch) -> None:
        """Drive the REAL writer and then the REAL reader; nothing is reimplemented."""
        worktree = make_worktree(tmp_path)
        enroll(worktree)
        monkeypatch.chdir(worktree)

        guard = self._guard()
        # The fixture is a git directory, not a repository a git binary would accept, so
        # the one git call the writer makes is answered here. The receipt itself is
        # still written by the real writer and judged by the real reader.
        monkeypatch.setattr(guard, "run", lambda args: (0, str(worktree), ""))
        guard._record_receipt("main", "pre-squash", SHA_A, BASE_SHA)

        assert push_receipt.worktree_verdict(str(worktree))[0] == push_receipt.VERDICT_OK
        assert security.is_denied(FEATURE_PUBLISH) is None

    def test_a_receipt_is_recorded_only_when_the_guard_passes(self, home, monkeypatch) -> None:
        """``main`` is the one place that records, and only on its own 0."""
        guard = self._guard()
        recorded: list[tuple[str, str]] = []
        monkeypatch.setattr(
            guard,
            "_record_receipt",
            lambda base, mode, head, base_sha: recorded.append((base, mode)),
        )
        monkeypatch.setattr(guard, "run", lambda args: (0, "true", ""))
        monkeypatch.setattr(guard, "_fetch_base", lambda base: 0)
        monkeypatch.setattr(guard, "_resolve_base", lambda arg: "main")
        monkeypatch.setattr(sys, "argv", ["push_guard.py"])

        monkeypatch.setattr(guard, "_check_pre_squash", lambda base, max_ahead: 40)
        assert guard.main() == 40
        assert recorded == []

        monkeypatch.setattr(guard, "_check_pre_squash", lambda base, max_ahead: 0)
        assert guard.main() == 0
        assert recorded == [("main", "pre-squash")]

    def test_a_receipt_write_failure_does_not_change_the_verdict(
        self, home, tmp_path, monkeypatch, capsys
    ) -> None:
        """The guard answers about the base; a store failure is reported, not fatal."""
        guard = self._guard()
        monkeypatch.chdir(tmp_path)

        guard._record_receipt("main", "pre-squash", SHA_A, BASE_SHA)

        assert "push receipt not recorded" in capsys.readouterr().err


class TestTheLabelIsOneString:
    def test_the_floors_label_matches_the_modules(self) -> None:
        """Two copies of one label drift silently; this is the pin that stops it."""
        assert security._PUSH_RECEIPT_DENY_LABEL == push_receipt.DENY_LABEL


class TestTheBaseTheGuardJudgedAgainst:
    """A receipt claims a PAIR -- this commit, that base -- so the reader re-reads both.

    Keyed on ``HEAD`` alone a receipt outlives the verdict it records: the guard passes,
    the base advances, a rerun refuses, and the first pass still authorizes the publish
    the refusal was about.
    """

    def test_the_receipt_records_the_base_ref_and_its_commit(self, home, tmp_path) -> None:
        worktree = make_worktree(tmp_path)

        path = receipt_for(worktree, base="main", mode="pre-squash")

        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["base_ref"] == "refs/remotes/origin/main"
        assert document["base_sha"] == BASE_SHA

    def test_a_base_that_has_moved_refuses(self, home, tmp_path) -> None:
        worktree = make_worktree(tmp_path)
        enroll(worktree)
        receipt_for(worktree, base="main", mode="pre-squash")

        move_base(worktree, MOVED_BASE_SHA)

        verdict, detail = push_receipt.worktree_verdict(str(worktree))
        assert verdict == push_receipt.VERDICT_DENY
        assert MOVED_BASE_SHA[:12] in detail
        assert push_receipt.GATE_COMMAND in detail
        assert security.is_denied(FEATURE_PUBLISH) is not None

    def test_a_base_that_has_not_moved_still_allows(self, home, tmp_path) -> None:
        """The complement: the new comparison must not refuse a receipt that is fine."""
        worktree = make_worktree(tmp_path)
        enroll(worktree)
        receipt_for(worktree, base="main", mode="pre-squash")

        move_base(worktree, BASE_SHA)

        assert push_receipt.worktree_verdict(str(worktree))[0] == push_receipt.VERDICT_OK
        assert security.is_denied(FEATURE_PUBLISH) is None

    def test_a_receipt_naming_no_base_ref_refuses(self, home, tmp_path) -> None:
        worktree = make_worktree(tmp_path)
        path = receipt_for(worktree, base="main", mode="pre-squash")
        document = json.loads(path.read_text(encoding="utf-8"))
        del document["base_ref"]
        path.write_text(json.dumps(document), encoding="utf-8")

        verdict, detail = push_receipt.worktree_verdict(str(worktree))
        assert verdict == push_receipt.VERDICT_DENY
        assert "records no base ref" in detail

    def test_a_receipt_whose_base_ref_escapes_refs_refuses(self, home, tmp_path) -> None:
        """The ref comes out of a file, so it is validated before it is resolved."""
        worktree = make_worktree(tmp_path)
        path = receipt_for(worktree, base="main", mode="pre-squash")
        document = json.loads(path.read_text(encoding="utf-8"))
        document["base_ref"] = "refs/remotes/../../../../etc/passwd"
        path.write_text(json.dumps(document), encoding="utf-8")

        verdict, detail = push_receipt.worktree_verdict(str(worktree))
        assert verdict == push_receipt.VERDICT_DENY
        assert "records no base ref" in detail

    def test_a_receipt_whose_base_ref_is_not_a_ref_refuses(self, home, tmp_path) -> None:
        """A malformed ref is a definite answer about the receipt, not a failed read."""
        worktree = make_worktree(tmp_path)
        path = receipt_for(worktree, base="main", mode="pre-squash")
        document = json.loads(path.read_text(encoding="utf-8"))
        document["base_ref"] = "heads/main"
        path.write_text(json.dumps(document), encoding="utf-8")

        verdict, detail = push_receipt.worktree_verdict(str(worktree))
        assert verdict == push_receipt.VERDICT_DENY
        assert "records no base ref" in detail

    def test_the_refusal_names_the_check_and_the_age_of_the_pass_it_supersedes(
        self, home, tmp_path
    ) -> None:
        """``mode`` and ``ts`` are read here; an operator needs both to choose a next step."""
        worktree = make_worktree(tmp_path)
        receipt_for(worktree, base="main", mode="single-on-base")

        move_base(worktree, MOVED_BASE_SHA)

        detail = push_receipt.worktree_verdict(str(worktree))[1]
        assert "by the single-on-base check" in detail
        assert "minute(s) ago" in detail

    def test_a_receipt_without_those_fields_still_refuses_cleanly(self, home, tmp_path) -> None:
        """The refusal must not fail while explaining itself: no clause, no crash."""
        worktree = make_worktree(tmp_path)
        path = receipt_for(worktree, base="main", mode="pre-squash")
        document = json.loads(path.read_text(encoding="utf-8"))
        del document["mode"]
        document["ts"] = "not-a-number"
        path.write_text(json.dumps(document), encoding="utf-8")
        move_base(worktree, MOVED_BASE_SHA)

        verdict, detail = push_receipt.worktree_verdict(str(worktree))
        assert verdict == push_receipt.VERDICT_DENY
        assert "recorded" not in detail
        assert push_receipt.GATE_COMMAND in detail

    def test_a_receipt_naming_no_base_commit_refuses(self, home, tmp_path) -> None:
        worktree = make_worktree(tmp_path)
        path = receipt_for(worktree, base="main", mode="pre-squash")
        document = json.loads(path.read_text(encoding="utf-8"))
        document["base_sha"] = "not-a-sha"
        path.write_text(json.dumps(document), encoding="utf-8")

        verdict, detail = push_receipt.worktree_verdict(str(worktree))
        assert verdict == push_receipt.VERDICT_DENY
        assert "records no base commit" in detail

    def test_a_base_ref_that_no_longer_resolves_is_not_an_answer(self, home, tmp_path) -> None:
        """Fail OPEN: a ref that cannot be read is a read failure, not a verdict."""
        worktree = make_worktree(tmp_path)
        enroll(worktree)
        receipt_for(worktree, base="main", mode="pre-squash")

        (worktree / ".git" / "refs" / "remotes" / "origin" / "main").unlink()

        verdict, detail = push_receipt.worktree_verdict(str(worktree))
        assert verdict == push_receipt.VERDICT_UNKNOWN
        assert "base ref unresolvable" in detail
        assert security.is_denied(FEATURE_PUBLISH) is None

    def test_a_revision_that_is_not_a_commit_refuses_to_write(self, home, tmp_path) -> None:
        """The writer is handed the pair; it refuses to store anything that is not one."""
        worktree = make_worktree(tmp_path)

        with pytest.raises(ValueError):
            push_receipt.write_receipt(
                worktree, base="main", mode="pre-squash", head="nope", base_sha=BASE_SHA
            )
        with pytest.raises(ValueError):
            push_receipt.write_receipt(
                worktree, base="main", mode="pre-squash", head=SHA_A, base_sha="nope"
            )

        assert not push_receipt.receipt_path(push_receipt.git_dir_for(worktree)).exists()

    def test_the_writer_records_the_pair_it_is_given_not_the_refs(self, home, tmp_path) -> None:
        """Re-reading the refs here is the defect: it records a state nothing checked."""
        worktree = make_worktree(tmp_path)

        path = push_receipt.write_receipt(
            worktree, base="main", mode="pre-squash", head=SHA_B, base_sha=MOVED_BASE_SHA
        )

        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["sha"] == SHA_B
        assert document["base_sha"] == MOVED_BASE_SHA


class TestTheGuardInvalidatesBeforeItJudges:
    """A run that refuses must not leave the previous run's pass behind."""

    @staticmethod
    def _guard():
        path = (
            Path(kiro_crew.__file__).resolve().parent
            / "builtin_skills"
            / "kirocrew-dev"
            / "prepare-pr"
            / "scripts"
            / "push_guard.py"
        )
        spec = importlib.util.spec_from_file_location("push_guard_clear_under_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_clear_receipt_removes_an_existing_receipt(self, home, tmp_path) -> None:
        worktree = make_worktree(tmp_path)
        path = receipt_for(worktree, base="main", mode="pre-squash")
        assert path.exists()

        push_receipt.clear_receipt(worktree)

        assert not path.exists()

    def test_clearing_a_worktree_with_no_receipt_is_success(self, home, tmp_path) -> None:
        """Absence is what the caller wanted; there is nothing to invalidate."""
        worktree = make_worktree(tmp_path)

        push_receipt.clear_receipt(worktree)

        assert not push_receipt.receipt_path(push_receipt.git_dir_for(worktree)).exists()

    def test_a_refused_run_leaves_no_receipt_from_the_previous_pass(
        self, home, tmp_path, monkeypatch
    ) -> None:
        """The whole property, driven through ``main``: refuse, and the pass is gone."""
        worktree = make_worktree(tmp_path)
        enroll(worktree)
        path = receipt_for(worktree, base="main", mode="pre-squash")
        assert path.exists()

        guard = self._guard()

        def fake_run(args):
            if args[:2] == ["git", "rev-parse"] and "--show-toplevel" in args:
                return 0, str(worktree), ""
            if args[:2] == ["git", "rev-parse"]:
                return 0, "true", ""
            if args[:2] == ["git", "fetch"]:
                return 1, "", "fatal: could not read from remote repository"
            return 0, "", ""

        monkeypatch.setattr(guard, "run", fake_run)
        monkeypatch.setattr(sys, "argv", ["push_guard.py", "--base", "main"])

        assert guard.main() != 0
        assert not path.exists()
        assert security.is_denied(FEATURE_PUBLISH) is not None

    def test_a_store_that_cannot_be_cleared_refuses_the_run(
        self, home, tmp_path, monkeypatch
    ) -> None:
        """Fail CLOSED here: a receipt that may still be there must not be assumed gone."""
        worktree = make_worktree(tmp_path)
        guard = self._guard()
        monkeypatch.setattr(guard, "run", lambda args: (0, str(worktree), ""))

        class Boom:
            @staticmethod
            def clear_receipt(_root):
                raise OSError("permission denied")

        monkeypatch.setattr(guard, "_receipt_module", lambda: Boom)

        assert guard._clear_receipt() == 2

    def test_an_unreachable_store_does_not_refuse_the_run(
        self, home, tmp_path, monkeypatch
    ) -> None:
        """The receipt import is optional; an absent store wrote nothing to invalidate."""
        worktree = make_worktree(tmp_path)
        guard = self._guard()
        monkeypatch.setattr(guard, "run", lambda args: (0, str(worktree), ""))

        def no_module():
            raise ImportError("no receipt writer beside this script")

        monkeypatch.setattr(guard, "_receipt_module", no_module)

        assert guard._clear_receipt() == 0

    def test_outside_a_worktree_the_run_is_refused(self, home, monkeypatch) -> None:
        guard = self._guard()
        monkeypatch.setattr(guard, "run", lambda args: (128, "", "not a git repository"))

        assert guard._clear_receipt() == 2

    def test_outside_a_worktree_nothing_is_recorded(self, home, monkeypatch, capsys) -> None:
        """The writer's own guard: no toplevel, no receipt, and it says so."""
        guard = self._guard()
        monkeypatch.setattr(guard, "run", lambda args: (128, "", "not a git repository"))

        guard._record_receipt("main", "pre-squash", SHA_A, BASE_SHA)

        assert "push receipt not recorded" in capsys.readouterr().err


class TestTheGuardRecordsOnlyWhatItChecked:
    """The receipt names the pair the check judged, or the run refuses to record."""

    @staticmethod
    def _guard():
        path = (
            Path(kiro_crew.__file__).resolve().parent
            / "builtin_skills"
            / "kirocrew-dev"
            / "prepare-pr"
            / "scripts"
            / "push_guard.py"
        )
        spec = importlib.util.spec_from_file_location("push_guard_pair_under_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _stub_git(self, guard, monkeypatch, worktree, heads):
        """Answer the guard's git calls; ``heads`` is popped per HEAD read."""

        def fake_run(args):
            if args[:2] == ["git", "rev-parse"] and "--show-toplevel" in args:
                return 0, str(worktree), ""
            if args[:3] == ["git", "rev-parse", "HEAD"]:
                return 0, heads.pop(0), ""
            if args[:2] == ["git", "rev-parse"]:
                return 0, BASE_SHA, ""
            return 0, "true", ""

        monkeypatch.setattr(guard, "run", fake_run)
        monkeypatch.setattr(guard, "_fetch_base", lambda base: 0)
        monkeypatch.setattr(guard, "_resolve_base", lambda arg: "main")
        monkeypatch.setattr(guard, "_check_pre_squash", lambda base, max_ahead: 0)
        monkeypatch.setattr(sys, "argv", ["push_guard.py"])

    def test_a_head_that_moved_across_the_check_records_nothing(
        self, home, tmp_path, monkeypatch, capsys
    ) -> None:
        """The verdict is about neither state, so there is nothing honest to record."""
        worktree = make_worktree(tmp_path)
        guard = self._guard()
        self._stub_git(guard, monkeypatch, worktree, [SHA_A, SHA_B])

        assert guard.main() == 2
        assert "moved while this guard was checking" in capsys.readouterr().err
        assert not push_receipt.receipt_path(push_receipt.git_dir_for(worktree)).exists()

    def test_a_stable_pair_is_recorded(self, home, tmp_path, monkeypatch) -> None:
        """The complement: an unchanged pair must still produce a receipt."""
        worktree = make_worktree(tmp_path)
        guard = self._guard()
        self._stub_git(guard, monkeypatch, worktree, [SHA_A, SHA_A])

        assert guard.main() == 0

        document = json.loads(
            push_receipt.receipt_path(push_receipt.git_dir_for(worktree)).read_text(
                encoding="utf-8"
            )
        )
        assert document["sha"] == SHA_A
        assert document["base_sha"] == BASE_SHA

    def test_a_receipt_the_floor_would_reject_is_reported_at_gate_time(
        self, home, tmp_path, monkeypatch, capsys
    ) -> None:
        """A write the reader cannot accept is silent otherwise until the publish fails."""
        worktree = make_worktree(tmp_path)
        guard = self._guard()
        monkeypatch.setattr(guard, "run", lambda args: (0, str(worktree), ""))
        real = guard._receipt_module()

        class Reader:
            write_receipt = staticmethod(real.write_receipt)

            @staticmethod
            def worktree_verdict(_root):
                return "deny", "this reader cannot judge that repository"

        monkeypatch.setattr(guard, "_receipt_module", lambda: Reader)

        guard._record_receipt("main", "pre-squash", SHA_A, BASE_SHA)

        assert "does not accept it" in capsys.readouterr().err

    def test_the_refusal_says_it_covers_every_repository(self, home, tmp_path) -> None:
        """The reader is not necessarily standing in the worktree the refusal names."""
        worktree = make_worktree(tmp_path)
        enroll(worktree)

        detail = security.is_denied(FEATURE_PUBLISH)

        assert detail is not None
        assert "every publish from this gateway" in detail
