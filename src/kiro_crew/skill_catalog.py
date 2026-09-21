"""Process-wide snapshot of the discovered skill directory.

Why this exists
---------------
A per-loader deadline cache over the directory walk serves the wrong turns, for
three reasons:

* ``SkillsLoader`` is not a singleton. A running gateway holds several
  long-lived loaders plus one short-lived loader per unsigned MCP call, and a
  cache on the instance gives each of them its own copy -- so the walk costs once
  per loader rather than once per corpus, and a wave of concurrent sessions
  multiplies it.
* a deadline makes whichever turn finds it expired pay the entire walk on its own
  caller thread. Chat messages arrive minutes apart, which is exactly the
  interval that misses a 60-second deadline.
* a fresh process starts empty, so the first turn after every restart pays the
  walk too.

So the deadline is not here. One snapshot per corpus -- the skills root, the
extra roots, and the trusted project root that contribute to it -- lives for the
whole process and is shared by every loader reading that corpus. A turn reads
whatever snapshot exists and never walks; one background worker does the walking
and publishes a newer snapshot when it finishes. The timer is SUBTRACTED rather
than relocated: there is no second directory cache to reconcile with this one.

:data:`BACKGROUND_REVERIFY_SECS` is not that timer in another coat. Nothing in
the read path consults it to decide whether a snapshot may be SERVED -- an
unverified snapshot is still served -- it only decides when the background worker
is asked to walk again. The distinction is the whole point: staleness costs a late
discovery of an out-of-band edit, never a turn's latency.

What a snapshot deliberately does NOT hold
------------------------------------------
Authorization. A row is a name, a path, and the project key the walker admitted
it under. Whether the CURRENT session may read that path is re-decided at every
read from live state: agent mapping, project trust, disabled apps, ``repo_scope``
and the sensitive-path fence all run against a snapshot's rows, never instead of
them. The corpus key carries the project key so that withdrawing trust selects a
different corpus rather than re-filtering
a retained one, so a revoke takes effect on the next read instead of whenever a
refresh happens to land.
"""

from __future__ import annotations

import atexit
import contextvars
import logging
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Count bound on the rows a corpus listing RETAINS, applied where they are
# retained: the walker stops adding rows past it, and the snapshot store stops
# pulling past it, so a runaway tree -- an app that installs a million
# directories -- is refused rather than retained and then trimmed. One constant is
# shared by both so they cannot disagree about where the tail begins. A real
# corpus is a few hundred rows (a dev desktop with AIM-installed package roots);
# this leaves room for a synthetic scale test.
MAX_CATALOG_SKILLS = 20_000

# Count bound on the CORPORA tracked at once, because rows-per-corpus is only
# half the population: a long-lived gateway visits one corpus per trusted project
# it opens, each holding the global tree plus that project's skills, and nothing
# in a read path ever drops one. A host works in a handful of repositories, so
# this is generous; past it the least recently READ corpus is dropped, which costs
# that corpus one listing the next time it is asked for and nothing else -- a
# snapshot is a cache, so forgetting one is always safe.
MAX_TRACKED_CORPORA = 32

# How long a published snapshot may go unverified before the background worker
# is asked to walk again. Read ONLY when scheduling, never when serving.
BACKGROUND_REVERIFY_SECS = 60.0

# Bound on the one wait this module permits: the FIRST listing of a corpus on a
# process that has never listed it, requested through
# :func:`wait_for_first`. Generous because the alternative is reporting that a
# required startup instruction was injected when it was not, and bounded because
# an unreadable or enormous root must degrade to "discovery incomplete" rather
# than hang the caller.
COLD_BUILD_WAIT_SECS = 30.0

# Character bounds on every string retained by a catalog row or corpus key.
# 32,767 is the largest realistic filesystem path on supported hosts (the
# Windows extended-path ceiling; POSIX path ceilings are smaller), so these
# bounds stop synthetic builders retaining arbitrary strings without refusing a
# path a real filesystem can name.
# Relative skill key retained in each CatalogEntry.
MAX_CATALOG_NAME_CHARS = 32_767
# SKILL.md path plus every skills/extra root retained in a CatalogEntry/CorpusKey.
MAX_CATALOG_PATH_CHARS = 32_767
# Canonical project identity retained in each confined row and CorpusKey.
MAX_CATALOG_PROJECT_KEY_CHARS = 32_767
# Count bound on the extra roots retained in a CorpusKey. The per-path character
# bounds above bound each element; this bounds how MANY there are, which is the
# other retained dimension -- ``skills.extra_paths`` comes from operator or
# edition config, so its length is as writable as its contents. Refused rather
# than truncated, for the same reason the character bounds refuse: dropping a
# root would make two different root sets share one key, and a corpus would then
# serve a listing built over roots the caller did not ask about. A real install
# configures a handful.
MAX_CATALOG_EXTRA_ROOTS = 256

CatalogEntry = tuple[str, Path, str | None]
"""``(name, skill_file, confined_project_key_or_None)`` -- the walker's row."""

CorpusKey = tuple[str, tuple[str, ...], str]
"""``(skills_root, extra_roots, trusted_project_key)``.

The project key is ``""`` when no project contributes, which is also what an
untrusted project resolves to, so trust is expressed by WHICH corpus is read.
"""


def corpus_key(
    skills_dir: Path | str,
    extra_paths: Sequence[Path | str],
    project_key: str = "",
) -> CorpusKey:
    """Build the corpus key for a set of roots.

    Extra roots are kept in order, not sorted: precedence is positional in the
    walker, so two orderings are genuinely different corpora. Every retained
    identity field is bounded before it becomes a dictionary key -- each field's
    length AND the number of extra roots -- because truncating either would merge
    distinct corpora, so an over-long or over-long-a-list identity is refused
    instead.
    """
    root = str(skills_dir)
    extras = tuple(str(p) for p in extra_paths)
    project = project_key or ""
    if len(extras) > MAX_CATALOG_EXTRA_ROOTS:
        raise ValueError(
            f"skill catalog extra roots exceed the retention bound "
            f"({len(extras)} > {MAX_CATALOG_EXTRA_ROOTS})"
        )
    if len(root) > MAX_CATALOG_PATH_CHARS or any(
        len(path) > MAX_CATALOG_PATH_CHARS for path in extras
    ):
        raise ValueError("skill catalog path exceeds the filesystem path bound")
    if len(project) > MAX_CATALOG_PROJECT_KEY_CHARS:
        raise ValueError("skill catalog project key exceeds the filesystem path bound")
    return (root, extras, project)


@dataclass(frozen=True)
class CatalogSnapshot:
    """An immutable directory listing, plus whether a tail was refused."""

    entries: tuple[CatalogEntry, ...]
    generation: int
    built_at: float
    truncated: bool = False

    @property
    def complete(self) -> bool:
        """True when the walk retained every row it found.

        A truncated snapshot is a lower bound on the corpus. Callers that report
        "no such skill" must say INCOMPLETE instead when this is false, so a
        refused count tail or over-long row never reads like a population that
        never named those skills.

        The flag is a BOOLEAN rather than a count on purpose. Retention stops at
        the first refused row, so nothing downstream knows the omitted population
        size -- and a number here would be a precision this code does not have.
        """
        return not self.truncated


@dataclass
class _Corpus:
    """Mutable per-corpus bookkeeping. Guarded by :data:`_LOCK`."""

    snapshot: CatalogSnapshot | None = None
    generation: int = 0
    refreshing: bool = False
    dirty: bool = False
    verified_at: float = 0.0
    first_build: threading.Event = field(default_factory=threading.Event)
    failures: int = 0
    # Eviction inputs. ``last_read`` orders the victims; ``waiters`` protects a
    # corpus somebody is parked on, whose event nobody would set after an
    # eviction, from becoming one.
    last_read: float = field(default_factory=time.monotonic)
    waiters: int = 0


_LOCK = threading.RLock()
_CORPORA: dict[CorpusKey, _Corpus] = {}
_EXECUTOR: ThreadPoolExecutor | None = None
_SHUT_DOWN = False


def _executor() -> ThreadPoolExecutor | None:
    """The single refresh worker, created on first use.

    ONE worker, for the whole process and every corpus. That is the contract
    that keeps refresh work from multiplying with concurrent sessions: a second
    session asking for the same corpus joins the in-flight walk (see
    :func:`_schedule`), and a different corpus queues behind it rather than
    starting a competing walk. Serialized refresh is the right trade because no
    turn is waiting on it.
    """
    global _EXECUTOR
    with _LOCK:
        if _SHUT_DOWN:
            return None
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="skill-catalog")
        return _EXECUTOR


def _row_within_bounds(row: CatalogEntry) -> bool:
    """Whether every retained string in *row* fits its named bound."""
    name, skill_file, project_key = row
    return (
        len(name) <= MAX_CATALOG_NAME_CHARS
        and len(str(skill_file)) <= MAX_CATALOG_PATH_CHARS
        and (project_key is None or len(project_key) <= MAX_CATALOG_PROJECT_KEY_CHARS)
    )


def _bound(rows: Iterable[CatalogEntry]) -> tuple[tuple[CatalogEntry, ...], bool]:
    """Consume bounded rows, reporting the first refused row as truncation.

    Streams: the iterable is pulled one row at a time and either the count cap or
    a named string cap stops the pull, so a runaway tree or synthetic oversized
    row is refused rather than retained and then trimmed. The walker applies the
    same count cap to its own per-root retention, so this sees at most one row
    past that cap -- that row is what proves the tail exists.
    """
    kept: list[CatalogEntry] = []
    truncated = False
    for row in rows:
        if len(kept) >= MAX_CATALOG_SKILLS or not _row_within_bounds(row):
            truncated = True
            break
        kept.append(row)
    return tuple(kept), truncated


def _corpus_for(key: CorpusKey) -> _Corpus:
    """Return *key*'s bookkeeping, making room first. Caller must hold the lock.

    The ONE place a corpus is created, so the population bound cannot be bypassed
    by a new call site. A tracked corpus is pure cache -- rows the walker can
    rebuild -- so the victim is simply forgotten: a walk already in flight for it
    publishes nothing (``_publish`` finds no corpus) and releases nothing that
    matters.

    A corpus with waiters is never the victim. Its waiters are parked on an Event
    only this module sets, and dropping the record would leave them waiting out
    their whole timeout for a listing nobody was going to publish.
    """
    corpus = _CORPORA.get(key)
    if corpus is not None:
        return corpus
    while len(_CORPORA) >= MAX_TRACKED_CORPORA:
        victims = [(c.last_read, k) for k, c in _CORPORA.items() if c.waiters == 0]
        if not victims:
            logger.warning(
                "skill-catalog: %d corpora tracked and every one has a waiter; "
                "tracking %s past the cap of %d",
                len(_CORPORA),
                key[0],
                MAX_TRACKED_CORPORA,
            )
            break
        _, oldest = min(victims)
        dropped = _CORPORA.pop(oldest, None)
        if dropped is not None:
            dropped.first_build.set()
        logger.debug("skill-catalog: forgot corpus %s to stay under the cap", oldest[0])
    corpus = _Corpus()
    _CORPORA[key] = corpus
    return corpus


def _reserve_generation(corpus: _Corpus) -> int:
    """Claim the next generation for a walk that is about to start.

    Claiming COMMITS the number to the corpus rather than handing out
    ``generation + 1`` and leaving it uncommitted. Two walks can be in flight at
    once -- a background refresh and the synchronous rebuild a mutation runs --
    and an uncommitted number gives both the same one, which makes the publish
    guard (``generation < corpus.generation``) admit the loser and overwrite the
    edit. Caller must hold :data:`_LOCK`.
    """
    corpus.generation += 1
    return corpus.generation


def _publish(
    key: CorpusKey,
    entries: tuple[CatalogEntry, ...],
    truncated: bool,
    generation: int,
) -> None:
    """Install a freshly built snapshot, unless a newer one already landed.

    Takes rows the caller already pulled through :func:`_bound`, on the thread
    that did the walking, so the cap stops the walk rather than trimming its
    result.

    The generation guard is what stops a slow walk from overwriting the result
    of an invalidation that happened while it ran: an in-app create bumps the
    generation, so the walk that started before it publishes nothing and the
    walk scheduled after it wins.
    """
    with _LOCK:
        corpus = _CORPORA.get(key)
        if corpus is None:
            return
        if generation < corpus.generation:
            logger.debug(
                "skill-catalog: discarding generation %d for %s; %d already published",
                generation,
                key[0],
                corpus.generation,
            )
            return
        corpus.snapshot = CatalogSnapshot(
            entries=entries,
            generation=generation,
            built_at=time.monotonic(),
            truncated=truncated,
        )
        corpus.generation = generation
        corpus.verified_at = time.monotonic()
        corpus.failures = 0
        corpus.first_build.set()
    if truncated:
        # Said out loud only once the snapshot is the published one. Logging at
        # the cap instead would announce an overflow for a superseded walk whose
        # rows nothing ever reads.
        logger.warning(
            "skill-catalog: corpus %s exceeded a catalog retention bound "
            "(at most %d rows; name/path/project fields at most %d/%d/%d "
            "characters); the refused row and tail are absent from discovery "
            "until the corpus is reduced",
            key[0],
            MAX_CATALOG_SKILLS,
            MAX_CATALOG_NAME_CHARS,
            MAX_CATALOG_PATH_CHARS,
            MAX_CATALOG_PROJECT_KEY_CHARS,
        )


def _is_interpreter_teardown(exc: BaseException) -> bool:
    """Whether *exc* is the walk losing a race with interpreter exit.

    ``concurrent.futures`` registers its own teardown through
    ``threading._register_atexit``, which runs BEFORE ordinary ``atexit``
    handlers, so the walker's pool can already be closed when a queued refresh
    reaches it -- and no hook this module can register runs early enough to
    prevent it. The outcome is correct either way (the previous listing survives,
    the worker lives), so this is logged as the teardown it is rather than as a
    fault, which is what it read like before: a warning and a full traceback for
    a process that was merely exiting.

    Matches on the shared PREFIX, because CPython raises two wordings from that
    one site -- ``after shutdown`` when the pool itself was closed and ``after
    interpreter shutdown`` at teardown -- and only one appears on any given exit.
    The first attempt at this matched the narrower wording and caught neither.
    """
    return isinstance(exc, RuntimeError) and "cannot schedule new futures" in str(exc)


def _run_refresh(
    key: CorpusKey,
    build: Callable[[], Iterable[CatalogEntry]],
    generation: int,
    ctx: contextvars.Context,
) -> None:
    """Walk the corpus on the worker thread and publish the result.

    Runs inside *ctx*, a copy of the scheduling thread's context, so anything
    the walker reads from a ContextVar (the platform context seam, the request's
    project scope) sees what the caller saw rather than the worker's empty
    default.

    Releases the single-flight mark in a ``finally`` for every outcome. A walk
    that raises leaves the previous snapshot in place -- a failed refresh must
    cost a late discovery, never the corpus -- and records the failure so a root
    that is permanently unreadable is visible in the log rather than silently
    retried forever at full volume.
    """
    try:
        entries, truncated = ctx.run(lambda: _bound(build()))
    except Exception as exc:  # noqa: BLE001 -- a failed walk must not kill the worker
        if _is_interpreter_teardown(exc):
            logger.debug("skill-catalog: refresh of %s abandoned at exit", key[0])
            return
        with _LOCK:
            corpus = _CORPORA.get(key)
            if corpus is not None:
                corpus.failures += 1
                failures = corpus.failures
                corpus.first_build.set()
            else:
                failures = 1
        logger.warning(
            "skill-catalog: refresh of %s failed (%d consecutive); serving the " "previous listing",
            key[0],
            failures,
            exc_info=True,
        )
    else:
        _publish(key, entries, truncated, generation)
    finally:
        _release_refresh_mark(key)


def _release_refresh_mark(key: CorpusKey) -> None:
    """Clear the single-flight mark, whatever the refresh's outcome was.

    Every exit from a scheduled or attempted refresh runs through here. A mark
    left set is worse than a missed refresh: no later read would ever schedule
    another walk for that corpus, so the listing would freeze for the process's
    lifetime.
    """
    with _LOCK:
        corpus = _CORPORA.get(key)
        if corpus is not None:
            corpus.refreshing = False


def _mark_for_rebuild(key: CorpusKey) -> None:
    """Ask the next read to schedule a fresh walk for *key*."""
    with _LOCK:
        corpus = _CORPORA.get(key)
        if corpus is not None:
            corpus.dirty = True
            corpus.first_build.set()


def _schedule(key: CorpusKey, build: Callable[[], Iterable[CatalogEntry]]) -> Future[None] | None:
    """Ask the worker to rebuild *key*, unless a rebuild is already in flight.

    Returns the future when this call started the walk, ``None`` when it joined
    one already running or the store is shut down. Callers must not treat
    ``None`` as failure: it is the single-flight contract working.
    """
    with _LOCK:
        if _SHUT_DOWN:
            return None
        corpus = _corpus_for(key)
        if corpus.refreshing:
            return None
        corpus.refreshing = True
        corpus.dirty = False
        generation = _reserve_generation(corpus)
    pool = _executor()
    if pool is None:
        _release_refresh_mark(key)
        return None
    ctx = contextvars.copy_context()
    try:
        return pool.submit(_run_refresh, key, build, generation, ctx)
    except RuntimeError:
        # The pool was shut down between the check and the submit.
        _release_refresh_mark(key)
        return None


def known_keys(skills_dir: Path | str, extra_paths: Sequence[Path | str]) -> list[CorpusKey]:
    """Every corpus this process has listed over exactly these roots.

    One root set spans several corpora: the global one plus one per trusted
    project that contributed its own skills root. A mutation to the global tree
    changes all of them, so a mutator that rebuilt only the global corpus would
    leave a project session's very next listing missing the skill just written --
    including a required ``always: true`` one.

    Returns the keys rather than acting on them, because only the loader knows how
    to walk each project key.
    """
    prefix = (str(skills_dir), tuple(str(p) for p in extra_paths))
    with _LOCK:
        return [key for key in _CORPORA if key[:2] == prefix]


def read(key: CorpusKey, build: Callable[[], Iterable[CatalogEntry]]) -> CatalogSnapshot | None:
    """Return the current snapshot for *key*, scheduling a refresh if due.

    NEVER walks on the calling thread. Returns ``None`` only when no snapshot
    for this corpus has ever been published -- the genuine cold case, which the
    caller must report as "still discovering", not as "no skills".
    """
    with _LOCK:
        corpus = _CORPORA.get(key)
        if corpus is not None:
            corpus.last_read = time.monotonic()
            snapshot = corpus.snapshot
            due = (
                corpus.dirty
                or snapshot is None
                or (time.monotonic() - corpus.verified_at) >= BACKGROUND_REVERIFY_SECS
            )
        else:
            snapshot, due = None, True
    if due:
        _schedule(key, build)
    return snapshot


def wait_for_first(
    key: CorpusKey,
    build: Callable[[], Iterable[CatalogEntry]],
    timeout: float,
) -> CatalogSnapshot | None:
    """Return the snapshot, waiting only for the FIRST build of this corpus.

    The one path that may block, and only on a process that has never listed
    this corpus and holds no persisted answer: required startup instructions
    cannot be discovered without knowing which skills declare them, and dropping
    a required instruction to save latency is not a trade this repo makes. Every
    later read goes through :func:`read` and waits for nothing.

    A timeout returns whatever is published (usually ``None``) rather than
    raising: the caller then reports discovery as incomplete, which is a true
    statement, where an exception would fail the turn.
    """
    # Registered as a waiter BEFORE the walk is scheduled. Marking afterwards
    # leaves a window in which another corpus arriving at the population cap picks
    # this one as its victim, and the listing the waiter is parked on is then never
    # published.
    with _LOCK:
        if _SHUT_DOWN:
            return None
        corpus = _corpus_for(key)
        corpus.waiters += 1
        event = corpus.first_build
    try:
        snapshot = read(key, build)
        if snapshot is not None:
            return snapshot
        event.wait(timeout)
    finally:
        with _LOCK:
            waiting = _CORPORA.get(key)
            if waiting is not None and waiting.waiters > 0:
                waiting.waiters -= 1
    with _LOCK:
        settled = _CORPORA.get(key)
        return settled.snapshot if settled is not None else None


def invalidate(keys: Iterable[CorpusKey] | None = None) -> None:
    """Mark corpora for rebuild after a mutation this process performed.

    Bumps the generation so a walk already in flight cannot publish over the
    edit, and marks the corpus dirty so the next read schedules a fresh walk.
    The existing snapshot is deliberately KEPT: the mutator's own path updates
    what it just wrote, and dropping the listing here would put the next turn
    back on a synchronous walk, which is the behaviour this module exists to
    remove.

    ``None`` invalidates every corpus -- what a root-set or config change needs,
    since it cannot know which corpora the change touched.
    """
    with _LOCK:
        targets = list(_CORPORA.keys()) if keys is None else list(keys)
        for key in targets:
            corpus = _CORPORA.get(key)
            if corpus is None:
                continue
            corpus.dirty = True
            corpus.generation += 1


def rebuild_now(key: CorpusKey, build: Callable[[], Iterable[CatalogEntry]]) -> bool:
    """Walk *key* on THIS thread and publish it, for a mutation just written.

    The one synchronous walk left, and it is not on a chat turn: its callers are
    ``create_skill`` / ``update_skill`` / ``delete_skill`` / ``refresh``, where the
    contract is that the write is visible to the very next listing. That
    contract cannot be met by a background rebuild, and it is not a regression to
    pay for it here -- before this module existed, a mutation cleared the cache
    and the next listing paid exactly this walk, on whichever thread happened to
    ask next.

    Bumps the generation first, so a background walk that started before the
    mutation cannot publish over the result.

    Returns whether a snapshot was published; a failed walk is logged and leaves
    the previous listing in place.
    """
    with _LOCK:
        if _SHUT_DOWN:
            return False
        corpus = _corpus_for(key)
        generation = _reserve_generation(corpus)
        corpus.dirty = False
    try:
        entries, truncated = _bound(build())
    except Exception:  # noqa: BLE001 -- a failed walk must not fail the mutation
        logger.warning(
            "skill-catalog: synchronous rebuild of %s failed; serving the previous "
            "listing until the next background refresh",
            key[0],
            exc_info=True,
        )
        _mark_for_rebuild(key)
        return False
    _publish(key, entries, truncated, generation)
    return True


def cold_listing(
    key: CorpusKey,
    build: Callable[[], Iterable[CatalogEntry]],
    *,
    may_wait: bool,
    timeout: float | None = None,
) -> CatalogSnapshot | None:
    """Produce the FIRST listing of a corpus, for a caller that needs rows now.

    Called only when :func:`read` returned ``None`` -- no listing for this corpus
    has ever been published in this process. Both branches end with a published
    snapshot, so either way this happens at most once per corpus per process and
    every later read waits for nothing.

    *may_wait* says whether the caller can be parked on a
    ``threading.Event``. Off the event loop it can, so the background worker's
    walk is joined rather than duplicated. ON the loop it cannot, so the walk
    happens right here -- which is precisely what the cache this module replaced
    did on every miss, so an on-loop cold listing is parity with the old
    behaviour rather than a new blocking site, and it is paid once instead of
    every sixty seconds.

    *timeout* defaults to :data:`COLD_BUILD_WAIT_SECS` read AT CALL TIME, so the
    bound a caller waits under is the module's current one rather than whatever it
    was when this function was defined.
    """
    if may_wait:
        return wait_for_first(key, build, COLD_BUILD_WAIT_SECS if timeout is None else timeout)
    rebuild_now(key, build)
    with _LOCK:
        corpus = _CORPORA.get(key)
        return corpus.snapshot if corpus is not None else None


def shutdown(wait: bool = False) -> None:
    """Stop the worker and forget every corpus.

    Idempotent, and safe to call while a walk is running: the walk touches the
    filesystem and this module only, never a loader's SQLite handle, so a late
    completion cannot write through a closed store. Tests call this to get a
    clean process-level state.
    """
    global _EXECUTOR, _SHUT_DOWN
    with _LOCK:
        pool, _EXECUTOR = _EXECUTOR, None
        _SHUT_DOWN = True
        for corpus in _CORPORA.values():
            corpus.first_build.set()
        _CORPORA.clear()
    if pool is not None:
        pool.shutdown(wait=wait, cancel_futures=True)
    with _LOCK:
        _SHUT_DOWN = False


def _stop_at_exit() -> None:
    """Stop scheduling walks once the interpreter is going down.

    Without this, a refresh still queued at exit runs while the walker's OWN
    thread pool is already shut down, and the walk dies on ``cannot schedule new
    futures after shutdown``. The refresh layer handles that correctly -- the
    previous listing survives and the worker lives -- but it logs a warning and a
    traceback for a process that was merely exiting, which reads like a fault and
    is not one. Found by the scale benchmark rather than by a test, because it
    only happens when a refresh is genuinely in flight at teardown.

    ``wait=False``: exit must not be held up by a walk nobody is waiting for.
    """
    shutdown(wait=False)


atexit.register(_stop_at_exit)


def corpus_state(key: CorpusKey) -> dict[str, object]:
    """One corpus's bookkeeping, for tests and diagnostics.

    ``known`` is False when this process has never been asked about the corpus at
    all, which is a different thing from ``published`` being False (asked, and
    the first walk has not landed yet).
    """
    with _LOCK:
        corpus = _CORPORA.get(key)
        if corpus is None:
            return {"known": False, "published": False, "dirty": False, "generation": 0}
        return {
            "known": True,
            "published": corpus.snapshot is not None,
            "dirty": corpus.dirty,
            "refreshing": corpus.refreshing,
            "generation": corpus.generation,
            "rows": len(corpus.snapshot.entries) if corpus.snapshot is not None else 0,
        }


def stats() -> dict[str, object]:
    """Operational counters, for tests and diagnostics."""
    with _LOCK:
        return {
            "corpora": len(_CORPORA),
            "refreshing": sum(1 for c in _CORPORA.values() if c.refreshing),
            "dirty": sum(1 for c in _CORPORA.values() if c.dirty),
            "published": sum(1 for c in _CORPORA.values() if c.snapshot is not None),
            "rows": sum(
                len(c.snapshot.entries) for c in _CORPORA.values() if c.snapshot is not None
            ),
        }
