"""Launch observations use the acquired session and survive event replay."""

from types import SimpleNamespace

import pytest

import kiro_crew.dashboard.handlers  # noqa: F401
from kiro_crew.dashboard.subagent_workspace import capture_launch_workspace, workspace_event_fields
from kiro_crew.dashboard.ws import build_subagent_snapshot


@pytest.mark.asyncio
async def test_launch_observes_actual_directory_and_linked_worktree(tmp_path):
    worktree = tmp_path / "linked"
    worktree.mkdir()
    gitdir = tmp_path / "metadata"
    gitdir.mkdir()
    (gitdir / "HEAD").write_text("ref: refs/heads/feature/review\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    cwd = worktree / "src"
    cwd.mkdir()
    info = SimpleNamespace(_session_cwd=str(cwd), cwd="/requested-but-not-used", acp_backend="pi")
    await capture_launch_workspace(info)
    fields = workspace_event_fields(info)
    assert fields["workspace"] == {
        "cwd": str(cwd),
        "worktree": str(worktree),
        "branch": "feature/review",
    }
    assert fields["backend"] == "pi"
    (gitdir / "HEAD").write_text("ref: refs/heads/changed\n", encoding="utf-8")
    assert workspace_event_fields(info) == fields  # explicitly a launch snapshot


@pytest.mark.asyncio
async def test_detached_head_and_non_repository(tmp_path, monkeypatch):
    # The test temp root itself may live inside a host checkout.
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files._GIT_ROOT_WALK_LIMIT", 1)
    info = SimpleNamespace(_session_cwd=str(tmp_path))
    await capture_launch_workspace(info)
    assert info.launch_workspace == {"cwd": str(tmp_path)}
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
    await capture_launch_workspace(info)
    assert info.launch_workspace["head"] == "aaaaaaa"
    assert "branch" not in info.launch_workspace


@pytest.mark.asyncio
async def test_missing_actual_directory_does_not_claim_requested_cwd():
    info = SimpleNamespace(cwd="/requested")
    await capture_launch_workspace(info)
    assert workspace_event_fields(info) == {}


@pytest.mark.asyncio
async def test_git_failure_keeps_known_cwd(tmp_path, monkeypatch):
    def fail(_cwd):
        raise OSError("unreadable")

    monkeypatch.setattr("kiro_crew.dashboard.handlers.files._project_git_branch", fail)
    info = SimpleNamespace(_session_cwd=str(tmp_path))
    await capture_launch_workspace(info)
    assert info.launch_workspace == {"cwd": str(tmp_path)}


def test_snapshot_carries_same_redacted_context_as_lifecycle_events():
    info = SimpleNamespace(
        id="a1",
        parent_session_key="dashboard:chat-1",
        task="review",
        agent="reviewer",
        resolved_model="auto",
        requested_model="",
        acp_backend="pi",
        streaming_text="",
        last_tool="",
        tool_count=0,
        stalled=False,
        started=1,
        launch_workspace={
            "cwd": "/workspace",
            "branch": "AKIAIOSFODNN7EXAMPLE",
            "untrusted_extra": "omit",
        },
    )
    fields = workspace_event_fields(info)
    assert fields["workspace"]["branch"] != info.launch_workspace["branch"]
    assert "untrusted_extra" not in fields["workspace"]
    snapshot = build_subagent_snapshot(info)
    assert snapshot["workspace"] == fields["workspace"]
    assert snapshot["backend"] == "pi"
