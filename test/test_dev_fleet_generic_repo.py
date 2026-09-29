"""Dev Fleet managing an operator-configured repository that is not Kiro Crew.

The git-only surface (listing, creating, rebasing, pruning worktrees) runs in any
checkout the operator names; the Kiro-only surface (Pull+Build, pods, Make Live)
stays behind ``_kirocrew_repo()``. Also covers the shared worktree layout every
Kiro Crew worktree producer routes through.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew import git_coord, worktree_layout
from kiro_crew.apps.builtins.dev_fleet import repository, runtime
from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, encoding="utf-8")


def _repo_with_commit(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "development")
    (path / "f.txt").write_text("x", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def fresh_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "_DISCOVERY_DONE", False)
    monkeypatch.setattr(repository, "_DISCOVERY_LOCK", None)
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    monkeypatch.setattr(repository, "MAIN_REPO_INFERRED", False)
    monkeypatch.setattr(repository, "MAIN_REPO_KIROCREW", False)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "_LATCHED_CONFIGURED", "")
    monkeypatch.setattr(runtime, "_GIT_TRUSTED_HELPERS", {})

    async def _none() -> None:
        return None

    async def _origin() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_load_fallback_repos", _none)
    monkeypatch.setattr(repository, "_upstream_remote", _origin)
    monkeypatch.setattr(repository, "_repo_source_hint", lambda: "set dev_fleet.repo_path")


def _configure(monkeypatch: pytest.MonkeyPatch, path: str, cfg: dict | None = None) -> None:
    monkeypatch.setattr(repository, "_configured_main_repo_checked", lambda: (path, True))
    monkeypatch.setattr(repository, "_resolve_primary_checkout", lambda p: p)
    monkeypatch.setattr(repository, "_load_dev_fleet_cfg", lambda: dict(cfg or {}))


class TestKiroOnlyGate:
    def test_a_generic_checkout_is_refused_by_the_kiro_only_accessor(self, monkeypatch) -> None:
        monkeypatch.setattr(repository, "MAIN_REPO", "/work/lineart")
        monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
        monkeypatch.setattr(repository, "MAIN_REPO_KIROCREW", False)
        assert repository._repo() == "/work/lineart"
        with pytest.raises(repository.RepoNotKiroCrew):
            repository._kirocrew_repo()

    def test_a_kirocrew_checkout_passes_the_kiro_only_accessor(self, monkeypatch) -> None:
        monkeypatch.setattr(repository, "MAIN_REPO", "/work/kirocrew")
        monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
        monkeypatch.setattr(repository, "MAIN_REPO_KIROCREW", True)
        assert repository._kirocrew_repo() == "/work/kirocrew"

    @pytest.mark.asyncio
    async def test_make_live_refuses_a_generic_fleet(self, monkeypatch) -> None:
        from kiro_crew.apps.builtins.dev_fleet import live

        monkeypatch.setattr(live, "_POINTER_PROVIDER", None)
        monkeypatch.setattr(repository, "MAIN_REPO", "/work/lineart")
        monkeypatch.setattr(repository, "MAIN_REPO_KIROCREW", False)
        out = await live._make_live("/work/lineart-wt", dry_run=True)
        assert out["ok"] is False and out["code"] == "repo_not_kirocrew"

    def test_not_kirocrew_is_still_a_repo_unavailable(self) -> None:
        assert issubclass(repository.RepoNotKiroCrew, repository.RepoUnavailable)


class TestConfiguredGenericRepository:
    @pytest.mark.asyncio
    async def test_a_configured_git_checkout_resolves_valid_with_its_trunk(
        self, fresh_discovery, monkeypatch, tmp_path
    ) -> None:
        repo = _repo_with_commit(tmp_path / "lineart")
        _git(repo, "update-ref", "refs/remotes/origin/development", "HEAD")
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/development")
        _configure(monkeypatch, str(repo))
        await repository.ensure_main_repo_discovered()
        assert repository._REPO_INVALID_MSG is None
        assert repository.MAIN_REPO == str(repo)
        assert repository.MAIN_REPO_KIROCREW is False
        assert repository.BASE_BRANCH == "development"

    @pytest.mark.asyncio
    async def test_a_configured_base_branch_wins_over_detection(
        self, fresh_discovery, monkeypatch, tmp_path
    ) -> None:
        repo = _repo_with_commit(tmp_path / "lineart")
        _configure(monkeypatch, str(repo), {"base_branch": "release"})
        await repository.ensure_main_repo_discovered()
        assert repository.BASE_BRANCH == "release"

    @pytest.mark.asyncio
    async def test_no_origin_head_falls_back_to_main(
        self, fresh_discovery, monkeypatch, tmp_path
    ) -> None:
        repo = _repo_with_commit(tmp_path / "lineart")
        _configure(monkeypatch, str(repo))
        await repository.ensure_main_repo_discovered()
        assert repository.BASE_BRANCH == "main"

    @pytest.mark.asyncio
    async def test_a_configured_path_without_git_is_invalid(
        self, fresh_discovery, monkeypatch, tmp_path
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        _configure(monkeypatch, str(plain))
        await repository.ensure_main_repo_discovered()
        assert repository._REPO_INVALID_MSG is not None
        assert "not a git checkout" in repository._REPO_INVALID_MSG
        with pytest.raises(repository.RepoUnreadable):
            repository._repo()

    @pytest.mark.parametrize("raw", ["-rf", "a..b", "x.lock", "", 7, "trunk/"])
    def test_an_implausible_base_branch_is_ignored(self, monkeypatch, raw) -> None:
        monkeypatch.setattr(repository, "_load_dev_fleet_cfg", lambda: {"base_branch": raw})
        assert repository._configured_base_branch() == ""


class TestRecency:
    def test_a_checkout_counts_as_use(self, tmp_path) -> None:
        repo = _repo_with_commit(tmp_path / "r")
        head = repo / ".git" / "HEAD"
        os.utime(head, (2_000_000_000, 2_000_000_000))
        assert repository._git_dir_last_touched(str(repo / ".git")) == 2_000_000_000

    def test_a_missing_git_dir_has_no_activity(self, tmp_path) -> None:
        assert repository._git_dir_last_touched(str(tmp_path / "nope")) is None


class TestWorktreeLayout:
    def test_the_env_override_wins(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("KIROCREW_WORKTREES_ROOT", str(tmp_path / "wt"))
        assert worktree_layout.worktree_path("/src/lineart", "spec-x") == (
            tmp_path / "wt" / "lineart" / "spec-x"
        )

    def test_the_config_value_is_used_and_expanded(self, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_WORKTREES_ROOT", raising=False)
        monkeypatch.setattr(worktree_layout, "_configured_root", lambda: "~/trees")
        assert worktree_layout.worktrees_root() == Path("~/trees").expanduser()

    def test_the_default_is_home_worktrees(self, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_WORKTREES_ROOT", raising=False)
        monkeypatch.setattr(worktree_layout, "_configured_root", lambda: "")
        assert worktree_layout.worktrees_root() == Path("~/worktrees").expanduser()

    def test_a_relative_root_is_refused(self, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_WORKTREES_ROOT", raising=False)
        monkeypatch.setattr(worktree_layout, "_configured_root", lambda: "trees")
        assert worktree_layout.worktrees_root() == Path("~/worktrees").expanduser()

    def test_the_config_file_is_read(self, monkeypatch, tmp_path) -> None:
        home = Path(os.environ["KIROCREW_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(
            '{"dev_fleet": {"worktrees_root": "%s"}}' % (tmp_path / "cfg"), encoding="utf-8"
        )
        assert worktree_layout._configured_root() == str(tmp_path / "cfg")

    @pytest.mark.parametrize("name", ["..", ".hidden", "a/b", "", "x" * 200])
    def test_an_unsafe_segment_is_refused(self, name) -> None:
        with pytest.raises(ValueError):
            worktree_layout.worktree_path("/src/repo", name)


class TestProducers:
    @pytest.mark.asyncio
    async def test_a_task_run_worktree_groups_under_the_primary_repo(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("KIROCREW_WORKTREES_ROOT", str(tmp_path / "wt"))
        repo = _repo_with_commit(tmp_path / "lineart")
        linked = tmp_path / "elsewhere" / "feature"
        _git(repo, "worktree", "add", "-q", str(linked), "-b", "feature")
        got = await git_coord._run_worktree_dir(str(linked), "abc123")
        assert got == str(tmp_path / "wt" / "lineart" / "task-abc123")

    @pytest.mark.asyncio
    async def test_an_unsafe_task_id_falls_back_to_the_legacy_path(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("KIROCREW_WORKTREES_ROOT", str(tmp_path / "wt"))
        repo = _repo_with_commit(tmp_path / "lineart")
        got = await git_coord._run_worktree_dir(str(repo), "a/b")
        assert got == str(tmp_path / ".kirocrew-work" / "a/b")

    def test_an_issue_radar_crew_is_told_its_worktree_root(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("KIROCREW_WORKTREES_ROOT", str(tmp_path / "wt"))
        assert crew_runtime._crew_worktree_root({}, "lineart") == str(tmp_path / "wt" / "lineart")
        assert crew_runtime._crew_worktree_root({"worktree_root": "/own"}, "lineart") == "/own"
        nudge = crew_runtime.compose_nudge(
            {"name": "c", "owner": "o", "repo": "r", "worktree_root": "/wt/r"}
        )
        assert "Worktree root: /wt/r" in nudge
        assert "Worktree root" not in crew_runtime.compose_nudge({"name": "c"})
