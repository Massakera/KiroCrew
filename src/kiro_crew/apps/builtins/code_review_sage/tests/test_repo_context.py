"""Tests for sage_lib/repo_context.py and the driver's use of it.

The git fixtures are real repositories in tmp_path: a stand-in "remote" and a
local clone, so the clone/fetch/checkout behaviour is exercised for real. The
network is never touched — ``_pr_shas`` and ``_remote_url`` are monkeypatched.
"""
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sage_lib import repo_context as RC  # noqa: E402
from sage_lib import results  # noqa: E402
from sage_lib import review_driver as D  # noqa: E402

GIT = shutil.which("git")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [GIT, "-C", str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=test", *args],
        check=True, capture_output=True, text=True, encoding="utf-8")
    return proc.stdout.strip()


def _refs(repo: Path) -> str:
    return _git(repo, "for-each-ref")


def _worktrees(repo: Path) -> str:
    return _git(repo, "worktree", "list", "--porcelain")


def _has_commit(repo: Path, sha: str) -> bool:
    return subprocess.run(
        [GIT, "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True, check=False).returncode == 0


class _RepoFixture:
    """A stand-in remote and a local clone, with base and head commits."""

    def __init__(self, root: Path, *, clone_at_head: bool) -> None:
        self.origin = root / "origin"
        self.local = root / "local"
        _git(root, "init", "-q", self.origin.name)
        (self.origin / "a.txt").write_text("base\n", encoding="utf-8")
        _git(self.origin, "add", "a.txt")
        _git(self.origin, "commit", "-q", "-m", "base")
        self.base_sha = _git(self.origin, "rev-parse", "HEAD")
        subprocess.run(
            [GIT, "clone", "-q", str(self.origin), str(self.local)],
            check=True, capture_output=True)
        if clone_at_head:
            self._advance_origin()
            _git(self.local, "pull", "-q")
        else:
            self._advance_origin()
        self.head_sha = _git(self.origin, "rev-parse", "HEAD")

    def _advance_origin(self) -> None:
        (self.origin / "b.txt").write_text("head\n", encoding="utf-8")
        _git(self.origin, "add", "b.txt")
        _git(self.origin, "commit", "-q", "-m", "head")
        # Mirror the server-side pull-request ref the fetch asks for.
        _git(self.origin, "update-ref", "refs/pull/7/head", "HEAD")


@unittest.skipUnless(GIT, "git is required")
class TestPrepare(unittest.TestCase):
    """``prepare`` against real git repositories in tmp space."""

    def setUp(self):
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "app"
        self.link = "https://github.com/o/r/pull/7"

    def _prepare(self, fx: _RepoFixture, **kwargs) -> RC.RepoContext:
        config = {"repo_checkouts": {"github.com/o/r": str(fx.local)}}
        with mock.patch.object(RC, "_pr_shas", return_value=(fx.head_sha, fx.base_sha)), \
             mock.patch.object(RC, "_remote_url", return_value=str(fx.origin)), \
             mock.patch.object(RC, "_credential_helper", return_value="!/bin/true"):
            return RC.prepare(self.link, change_id="GH-o-r-7", run_id="run1",
                              root=self.root, config=config, **kwargs)

    def test_head_present_locally(self):
        fx = _RepoFixture(self.tmp, clone_at_head=True)
        refs_before, trees_before = _refs(fx.local), _worktrees(fx.local)
        ctx = self._prepare(fx)
        self.assertEqual(ctx.status, RC.STATUS_USED)
        self.assertEqual(ctx.head_sha, fx.head_sha)
        self.assertEqual(ctx.base_sha, fx.base_sha)
        checkout = Path(ctx.path)
        self.assertTrue(checkout.is_dir())
        self.assertEqual(_git(checkout, "rev-parse", "HEAD"), fx.head_sha)
        # The user's clone is untouched: same refs, same worktrees, clean status.
        self.assertEqual(_refs(fx.local), refs_before)
        self.assertEqual(_worktrees(fx.local), trees_before)
        self.assertEqual(_git(fx.local, "status", "--porcelain"), "")
        RC.cleanup(ctx, self.root)
        self.assertFalse(checkout.exists())

    def test_missing_head_is_fetched_into_the_throwaway_only(self):
        fx = _RepoFixture(self.tmp, clone_at_head=False)
        ctx = self._prepare(fx)
        self.assertEqual(ctx.status, RC.STATUS_USED, ctx.reason)
        self.assertEqual(_git(Path(ctx.path), "rev-parse", "HEAD"), fx.head_sha)
        # The fetched objects went to the throwaway; the source clone never got
        # the head commit.
        self.assertFalse(_has_commit(fx.local, fx.head_sha))
        self.assertEqual(_git(fx.local, "status", "--porcelain"), "")
        RC.cleanup(ctx, self.root)
        self.assertFalse(Path(ctx.path).exists())

    def test_unmapped_repo_is_disabled(self):
        ctx = RC.prepare(self.link, change_id="GH-o-r-7", root=self.root,
                         config={"repo_checkouts": {}})
        self.assertEqual(ctx.status, RC.STATUS_DISABLED)
        self.assertFalse(RC.checkouts_root(self.root).exists())

    def test_unusable_mapping_is_unavailable(self):
        for value, why in [
            ("relative/path", "not absolute"),
            (str(self.tmp / "missing"), "not a git repository"),
            ("", "empty"),
        ]:
            ctx = RC.prepare(self.link, change_id="GH-o-r-7", root=self.root,
                             config={"repo_checkouts": {"github.com/o/r": value}})
            self.assertEqual(ctx.status, RC.STATUS_UNAVAILABLE, value)
            self.assertIn(why, ctx.reason)

    def test_failed_fetch_degrades_and_cleans_up(self):
        fx = _RepoFixture(self.tmp, clone_at_head=False)
        config = {"repo_checkouts": {"github.com/o/r": str(fx.local)}}
        with mock.patch.object(RC, "_pr_shas", return_value=(fx.head_sha, fx.base_sha)), \
             mock.patch.object(RC, "_remote_url", return_value=str(self.tmp / "gone")), \
             mock.patch.object(RC, "_credential_helper", return_value="!/bin/true"):
            ctx = RC.prepare(self.link, change_id="GH-o-r-7", root=self.root,
                             config=config)
        self.assertEqual(ctx.status, RC.STATUS_UNAVAILABLE)
        self.assertTrue(ctx.reason)
        leftovers = list(RC.checkouts_root(self.root).iterdir()) \
            if RC.checkouts_root(self.root).exists() else []
        self.assertEqual(leftovers, [])

    def test_never_raises(self):
        with mock.patch.object(RC, "_materialize", side_effect=RuntimeError("boom")):
            ctx = RC.prepare(self.link, change_id="GH-o-r-7", root=self.root,
                             config={"repo_checkouts": {"github.com/o/r": str(self.tmp)}})
        self.assertEqual(ctx.status, RC.STATUS_UNAVAILABLE)


class TestCleanupAndSweep(unittest.TestCase):
    def setUp(self):
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "app"

    def test_cleanup_refuses_a_path_outside_the_root(self):
        outside = self.tmp / "keepme"
        outside.mkdir()
        RC.cleanup(RC.RepoContext(RC.STATUS_USED, path=str(outside)), self.root)
        self.assertTrue(outside.exists())

    def test_sweep_removes_only_old_checkout_dirs(self):
        base = RC.checkouts_root(self.root)
        old = base / "old"
        young = base / "young"
        old.mkdir(parents=True)
        young.mkdir()
        a_file = base / "a-file"
        a_file.write_text("x", encoding="utf-8")
        os.utime(old, (time.time() - RC.STALE_AFTER_SEC - 10,) * 2)
        self.assertEqual(RC.sweep_stale(self.root), 1)
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        self.assertTrue(a_file.exists())

    def test_sweep_on_missing_root_is_a_noop(self):
        self.assertEqual(RC.sweep_stale(self.root), 0)


class TestDriverWiring(unittest.TestCase):
    """run_review hands the checkout to the prompt and stamps the record."""

    LINK = "https://github.com/o/r/pull/7"
    CID = "GH-o-r-7"

    def setUp(self):
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.prompts: list[str] = []
        self.cleaned: list[str] = []

    def _dispatch(self, task, timeout=0):
        self.prompts.append(task)
        if "SINGLE thorough pass" in task:
            results.write_result({
                "schema": "code-review-sage-result", "version": 1,
                "change_id": self.CID, "platform": "github",
                "repo_identity": "github.com/o/r", "revision": "1",
                "phase1": {"gate_verdict": "PASS", "design_risk": "low",
                           "criticality": "low"},
                "blast_radius": {"rating": "SMALL", "signals": {}},
                "counts": {"red": 0, "yellow": 0}, "findings": [],
                "deep_reviewed": True, "title": "t",
                "files_covered": ["f"], "coverage_complete": True,
            }, self.root)
        return {"ok": True, "output": "done", "error": ""}

    def _run(self, ctx: RC.RepoContext) -> dict:
        with mock.patch.object(D.repo_context, "prepare", return_value=ctx) as prep, \
             mock.patch.object(D.repo_context, "cleanup") as clean:
            clean.side_effect = lambda c, root=None: self.cleaned.append(c.path)
            out = D.run_review([self.LINK], dispatch=self._dispatch,
                               generate_report=False, root=self.root)
        prep.assert_called_once()
        return out

    def test_used_context_reaches_the_prompt_and_the_record(self):
        ctx = RC.RepoContext(RC.STATUS_USED, path=str(self.root / "co"),
                             base_sha="b" * 40, head_sha="c" * 40)
        out = self._run(ctx)
        self.assertEqual(out["deep_reviewed"], 1)
        review_prompt = next(p for p in self.prompts if "SINGLE thorough pass" in p)
        self.assertIn("REPOSITORY CONTEXT", review_prompt)
        self.assertIn(str(self.root / "co"), review_prompt)
        self.assertIn("EVIDENCE RULE", review_prompt)
        rec = results.read_result(self.CID, self.root)
        self.assertEqual(rec["repo_context"]["status"], RC.STATUS_USED)
        self.assertEqual(rec["repo_context"]["head_sha"], "c" * 40)
        self.assertEqual(out["per_change"][0]["repo_context"]["status"], RC.STATUS_USED)
        self.assertEqual(self.cleaned, [str(self.root / "co")])

    def test_disabled_context_leaves_the_prompt_unchanged(self):
        out = self._run(RC.RepoContext(RC.STATUS_DISABLED))
        self.assertEqual(out["deep_reviewed"], 1)
        review_prompt = next(p for p in self.prompts if "SINGLE thorough pass" in p)
        self.assertNotIn("REPOSITORY CONTEXT", review_prompt)
        self.assertIn("EVIDENCE RULE", review_prompt)
        self.assertEqual(
            results.read_result(self.CID, self.root)["repo_context"]["status"],
            RC.STATUS_DISABLED)

    def test_unavailable_context_still_reviews(self):
        ctx = RC.RepoContext(RC.STATUS_UNAVAILABLE, reason="no credentials")
        out = self._run(ctx)
        self.assertEqual(out["deep_reviewed"], 1)
        rec = results.read_result(self.CID, self.root)
        self.assertEqual(rec["repo_context"]["status"], RC.STATUS_UNAVAILABLE)
        self.assertEqual(rec["repo_context"]["reason"], "no credentials")

    def test_cleanup_runs_when_the_review_fails(self):
        def failing(task, timeout=0):
            return {"ok": False, "output": "", "error": "turn failed"}

        ctx = RC.RepoContext(RC.STATUS_USED, path=str(self.root / "co"),
                             base_sha="b" * 40, head_sha="c" * 40)
        with mock.patch.object(D.repo_context, "prepare", return_value=ctx), \
             mock.patch.object(D.repo_context, "cleanup") as clean:
            out = D.run_review([self.LINK], dispatch=failing,
                               generate_report=False, root=self.root)
        self.assertEqual(out["per_change"][0]["skipped_reason"], "review_failed")
        clean.assert_called_once()


class TestPromptText(unittest.TestCase):
    def test_context_step_quotes_base_revision_reads(self):
        ctx = RC.RepoContext(RC.STATUS_USED, path="/tmp/co",
                             base_sha="b" * 40, head_sha="c" * 40)
        step = D._repo_context_step(ctx)
        self.assertIn("git -C /tmp/co show " + "b" * 40 + ":<file>", step)
        self.assertIn("READ-ONLY", step)
        self.assertEqual(D._repo_context_step(RC.RepoContext(RC.STATUS_DISABLED)), "")
        self.assertEqual(D._repo_context_step(None), "")


if __name__ == "__main__":
    unittest.main()
