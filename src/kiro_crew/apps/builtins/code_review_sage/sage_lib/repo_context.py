#!/usr/bin/env python3
"""Per-review checkout of a pull request's head, for repository context.

A review sees only GitHub's per-file patch hunks. For a repository the user
maps to a local clone (``config.json:repo_checkouts``), the driver prepares a
throwaway checkout of the PR's exact head commit so the reviewer can read
callers, existing helpers, tests and project rules outside the diff.

Guarantees:

- The user's clone is never modified. The checkout is a ``git clone --shared``
  under ``data/tmp/checkouts/``; a head commit missing locally is fetched INTO
  that throwaway clone, never into the source.
- Git runs with no global or system config, a gh-scoped minimal environment
  and no terminal prompt, so the user's hooks, ``insteadOf`` rewrites, LFS
  filters or fsmonitor never run against content the PR author controls.
- Any failure degrades to a diff-only review: :func:`prepare` never raises; it
  returns a :class:`RepoContext` whose ``status`` says why context is missing.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sage_lib import adapters, store

try:
    from kiro_crew import github_runner
except ImportError:  # pragma: no cover - standalone fallback
    github_runner = None  # type: ignore

STATUS_USED = "used"
STATUS_UNAVAILABLE = "unavailable"
STATUS_DISABLED = "disabled"

GIT_TIMEOUT_SEC = 300.0
# Well above the longest review (90-minute turn plus one coverage pass), so the
# sweep never removes a checkout a running review still reads.
STALE_AFTER_SEC = 6 * 3600.0

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_REASON_MAX = 300


@dataclass(frozen=True)
class RepoContext:
    status: str
    path: str = ""
    base_sha: str = ""
    head_sha: str = ""
    reason: str = ""

    @property
    def used(self) -> bool:
        return self.status == STATUS_USED

    def as_record(self) -> dict:
        """The persisted form. The path is omitted: it is gone after the review."""
        return {
            "status": self.status,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "reason": self.reason,
        }


class _ContextError(RuntimeError):
    """A step failed; the message becomes the recorded reason."""


def checkouts_root(root: Path | None = None) -> Path:
    return store.data_dir(root) / "tmp" / "checkouts"


def configured_source(host: str, owner: str, repo: str,
                      config: dict | None) -> tuple[str, str]:
    """``(path, "")`` for a usable mapped clone, ``("", reason)`` for a mapped but
    unusable one, and ``("", "")`` when the repository is not mapped at all."""
    raw = (config or {}).get("repo_checkouts")
    if not isinstance(raw, dict):
        return "", ""
    want = f"{host}/{owner}/{repo}".lower()
    for key, value in raw.items():
        if str(key).strip().strip("/").lower() != want:
            continue
        if not isinstance(value, str) or not value.strip():
            return "", "configured checkout path is empty"
        path = Path(os.path.expanduser(value.strip()))
        if not path.is_absolute():
            return "", "configured checkout path is not absolute"
        if not path.is_dir() or not (path / ".git").exists():
            return "", "configured checkout path is not a git repository"
        return str(path), ""
    return "", ""


def _git_env() -> dict[str, str]:
    if github_runner is not None:
        env = dict(github_runner.gh_env())
    else:  # pragma: no cover - standalone fallback
        env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_LFS_SKIP_SMUDGE": "1",
    })
    return env


def _short(text: str) -> str:
    tail = " ".join((text or "").strip().splitlines()[-3:])
    return store.redact_text(tail)[:_REASON_MAX]


class _Git:
    """Runs git with the isolated environment; raises :class:`_ContextError`."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        exe = shutil.which("git", path=env.get("PATH"))
        if not exe:
            raise _ContextError("git is not installed on this host")
        self.exe = exe

    def __call__(self, *args: str, cwd: str | None = None,
                 check: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                [self.exe, *args], cwd=cwd, env=self.env, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=GIT_TIMEOUT_SEC, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _ContextError(f"git {args[0]} timed out") from exc
        except OSError as exc:
            raise _ContextError(f"git {args[0]} could not start: {type(exc).__name__}") from exc
        if check and proc.returncode != 0:
            raise _ContextError(f"git {args[0]} failed: {_short(proc.stderr)}")
        return proc

    def has_commit(self, repo: str, sha: str) -> bool:
        return self("cat-file", "-e", f"{sha}^{{commit}}", cwd=repo, check=False).returncode == 0


def _pr_shas(host: str, owner: str, repo: str, number: str) -> tuple[str, str]:
    from sage_lib import discovery

    try:
        rows = discovery.run_gh_json(
            f"repos/{owner}/{repo}/pulls/{number}",
            jq="{head: .head.sha, base: .base.sha}", host=host)
    except discovery.GhError as exc:
        raise _ContextError(f"could not read the pull request: {_short(str(exc))}") from exc
    row = rows[0] if rows else {}
    head, base = str(row.get("head") or ""), str(row.get("base") or "")
    if not (_SHA_RE.match(head) and _SHA_RE.match(base)):
        raise _ContextError("the pull request API returned no usable commit ids")
    return head, base


def _credential_helper() -> str:
    from sage_lib import discovery

    try:
        gh = discovery.gh_bin()
    except discovery.GhError as exc:
        raise _ContextError(f"gh is unavailable for fetching: {_short(str(exc))}") from exc
    return "!" + shlex.quote(gh) + " auth git-credential"


def _remote_url(host: str, owner: str, repo: str) -> str:
    """The HTTPS URL a PR head is fetched from. Module-level so tests can point
    the fetch at a local remote instead of the network."""
    return f"https://{host}/{owner}/{repo}.git"


def _fetch_missing(git: _Git, dest: str, host: str, owner: str, repo: str,
                   number: str, head: str, base: str) -> None:
    missing_head = not git.has_commit(dest, head)
    missing_base = not git.has_commit(dest, base)
    if not (missing_head or missing_base):
        return
    url = _remote_url(host, owner, repo)
    helper = ["-c", "credential.helper=", "-c", f"credential.helper={_credential_helper()}"]
    if missing_head:
        git(*helper, "fetch", "--no-tags", "--quiet", url,
            f"+refs/pull/{number}/head:refs/sage/head", cwd=dest)
        if not git.has_commit(dest, head):
            raise _ContextError("the pull request head moved while preparing the checkout")
    if missing_base and not git.has_commit(dest, base):
        git(*helper, "fetch", "--no-tags", "--quiet", url, base, cwd=dest)
        if not git.has_commit(dest, base):
            raise _ContextError("the base commit could not be fetched")


def _remove_tree(path: Path) -> None:
    def _retry_writable(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_retry_writable)


def _owned_checkout(path: str, root: Path | None) -> Path | None:
    """*path* when it is a direct child of the checkouts root, else None."""
    if not path:
        return None
    candidate = Path(path)
    base = checkouts_root(root)
    try:
        if candidate.resolve().parent != base.resolve() or candidate.is_symlink():
            return None
    except OSError:
        return None
    return candidate


def prepare(link: str, *, change_id: str, run_id: str | None = None,
            root: Path | None = None, config: dict | None = None) -> RepoContext:
    """Checkout of *link*'s head for a mapped repository. Never raises."""
    try:
        cfg = config if config is not None else store.read_config_quiet(root)
        try:
            host, owner, repo, number = adapters.github_pr_ref(link, config=cfg)
        except adapters.AdapterError:
            return RepoContext(STATUS_DISABLED)
        source, problem = configured_source(host, owner, repo, cfg)
        if problem:
            return RepoContext(STATUS_UNAVAILABLE, reason=problem)
        if not source:
            return RepoContext(STATUS_DISABLED)
        return _materialize(source, host, owner, repo, number,
                            change_id=change_id, run_id=run_id, root=root)
    except Exception as exc:  # defensive: context must never fail a review
        return RepoContext(STATUS_UNAVAILABLE,
                           reason=f"unexpected error: {type(exc).__name__}")


def _materialize(source: str, host: str, owner: str, repo: str, number: str, *,
                 change_id: str, run_id: str | None, root: Path | None) -> RepoContext:
    base_dir = checkouts_root(root)
    name = "-".join(
        _SAFE_NAME.sub("_", part)[:60]
        for part in (run_id or "adhoc", change_id, uuid.uuid4().hex[:8]))
    dest = base_dir / name
    head = base = ""
    try:
        head, base = _pr_shas(host, owner, repo, number)
        git = _Git(_git_env())
        base_dir.mkdir(parents=True, exist_ok=True)
        git("clone", "--shared", "--no-checkout", "--quiet", "--", source, str(dest))
        _fetch_missing(git, str(dest), host, owner, repo, number, head, base)
        git("-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", head,
            cwd=str(dest))
        actual = git("rev-parse", "HEAD", cwd=str(dest)).stdout.strip()
        if actual != head:
            raise _ContextError("the checkout is not at the pull request head")
    except _ContextError as exc:
        if dest.exists():
            _remove_tree(dest)
        return RepoContext(STATUS_UNAVAILABLE, base_sha=base, head_sha=head,
                           reason=str(exc)[:_REASON_MAX])
    except Exception:
        if dest.exists():
            _remove_tree(dest)
        raise
    return RepoContext(STATUS_USED, path=str(dest), base_sha=base, head_sha=head)


def cleanup(ctx: RepoContext | None, root: Path | None = None) -> None:
    """Remove the checkout *ctx* names. Refuses any path outside the checkouts root."""
    if ctx is None or not ctx.path:
        return
    owned = _owned_checkout(ctx.path, root)
    if owned is not None and owned.exists():
        _remove_tree(owned)


def sweep_stale(root: Path | None = None, max_age: float = STALE_AFTER_SEC) -> int:
    """Remove checkouts older than *max_age*, left behind by a crashed run."""
    base = checkouts_root(root)
    if not base.is_dir():
        return 0
    now = time.time()
    removed = 0
    for entry in base.iterdir():
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if now - entry.stat().st_mtime < max_age:
                continue
        except OSError:
            continue
        _remove_tree(entry)
        removed += 1
    return removed
