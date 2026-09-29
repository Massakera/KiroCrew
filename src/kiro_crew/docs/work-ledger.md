# Work ledger (conductors and workers)

The work ledger is the shared record between a **conductor** session and the
**worker** sessions it dispatched. The conductor breaks a goal into items, stands
up one session per item, and then learns what each worker did by reading a
record — not by reading the worker's transcript.

That is the whole point. A transcript has to be interpreted, it grows without
bound, and it can carry an instruction. The ledger carries schema-bounded fields
instead: a worker writes a status, a summary, and pointers to what it produced,
and the conductor reads them as data.

## When you want a conductor

Use one when a goal is too large for a single session and splits into pieces
that can run at the same time — "clear the flaky-test backlog", "push these six
pull requests green". Each piece gets its own session, its own context, and its
own tab you can open and steer.

A piece qualifies as a work item only when all three hold:

1. **Independent** — it does not consume another item's output. Two pieces that
   hand off to each other are one item.
2. **Assertable** — you can name its completion condition before it starts.
3. **Long-running** — long enough that you would plausibly want to watch it.

Fewer than two qualifying items means one long session is the better shape, and
a conductor would only add overhead.

To start one, open a session on the **`kirocrew-conductor`** agent (see
[Agents](agents.md) for the per-session, per-thread and per-cron selectors) and
give it the goal. Its operating procedure ships as the `goal-conductor` skill.
`kirocrew-ledger-conductor` is a deprecated alias of the same spec, kept for one
release so an existing session or cron that names the old string keeps resolving.

## The item

An item is the unit of dispatch. It holds:

| Field | Written by | What it is |
|---|---|---|
| `title` | conductor | what the item is, up to 200 chars |
| `acceptance` | conductor | the completion condition, stored verbatim |
| `round` | conductor | which dispatch round the item belongs to |
| `decision` | conductor | what the conductor decided and why |
| `evidence` | gateway | the current acceptance evaluation, bound to the condition, the worker's latest report and the exact revision observed |
| `verdict` | gateway | `evidence`'s verdict, mirrored |
| `fails` | gateway | how many evaluations came back `fail` |
| `acceptance_proof` | gateway | on an `accepted` close: the evidence and revision it was decided on |
| `state` | conductor | `open`, or terminal: `accepted` / `rejected` / `abandoned` |
| `status` | worker | `progress` / `done` / `blocked` / `question` |
| `summary` | worker | the worker's own account, up to 500 chars |
| `artifacts` | worker | pointers to what it produced |
| `pr` | worker | a pull-request number it produced |

The conductor writes its half with `work_ledger_record` (one action per call:
`goal`, `create`, `bind`, `decide`, `evaluate`, `accept`, `close`) and reads the
whole ledger back with `work_ledger_read`. A worker writes its half with
`work_report` and reads its own item with `work_brief`. A conductor whose ledger
files read as damaged or missing rewrites them from the crew log with
`work_ledger_rebuild`: every accepted write was recorded there, so the files are a
cache of that record. It takes no arguments, acts only on the caller's own ledger,
and is refused when the crew log is off.

The two sets are disjoint, and that is enforced by the tools rather than by a
rule: the reporting tool takes no parameter that names a conductor field, so a
worker cannot write a verdict, a state, or its own acceptance condition.

## The crew log must be on

Every write here is recorded in the crew log, so the whole board depends on it:
without `KIROCREW_CREW_LOG=1` set at gateway start, `work_ledger_record` and
`work_report` both answer `409 crew_log_off`, and `work_ledger_rebuild` is refused
for the same reason. Reads are unaffected.

The flag is off by default until #10705 lands, which is an UPGRADE REQUIREMENT and
not merely a default: a deployment that ran conductors before this change has board
writes that worked without any flag, and they stop working on upgrade until an
operator sets it. Set it before the first conductor runs, rather than discovering
the refusal from a worker that cannot report.

**The acceptance condition is named before dispatch, not after.** It is one of
three kinds: `pr_checks` (every check on the pull request's head commit is green;
the condition names the repository as owner/name), `file` (a path exists inside
the worker session's project directory), or `human_approval` (you decide —
legitimate for a design review, never machine-evaluated, and not closable as
accepted through the ledger, because there is no authenticated way yet for the
ledger to record that you approved; the conductor records your answer and closes
the item as rejected or abandoned). There is deliberately no "run this command" kind,
so "the tests pass" is expressed as `pr_checks` on the pull request that carries
the work, and CI's verdict is the one that counts.

A condition may name a value that only exists once the item starts — a pull
request number is the common case. The conductor stores it as `TBD`, the worker
reports the real number, and the conductor promotes it into the condition by
hand after looking at it. A worker's claimed `pr` is never read as the bar,
because a worker that could fill in its own bar could point it at somebody
else's already-green pull request.

Limits: 32 items per conductor, and a conductor may dispatch a conductor only
once — depth is capped at 2, so a second-level conductor's own children are
workers. A worker holds one open item at a time.

## Dispatch order: create, bind, seed

The conductor mints the item, attaches the session, and only then sends the seed
prompt. That order is not cosmetic. Binding before seeding means a worker's
first `work_brief` always finds its item; the other order leaves a running
worker with no binding, which is neither visible in the ledger nor recoverable.
A bound item with no seed is visible, and gets seeded on the next cycle.

The seed is the worker's whole contract. A worker session inherits no context
from the conductor beyond it.

## `work_brief` — what a worker reads

A dispatched worker calls `work_brief` first. It takes no arguments: which item
you are bound to is resolved from your own session, never supplied. It returns
the item's `title` and `acceptance` — together, the definition of done — plus
the round, the conductor's latest `decision`, and your own last reported status.

**`decision` is the only field to read as an instruction.** Everything else it
returns is state. It does not return the conductor's goal or any sibling item: a
worker has neither.

A session that is not a dispatched worker gets `not_bound`, which is also how a
root conductor learns it has no parent.

## `work_report` — what a worker writes

One call, four statuses:

| `status` | Meaning | Who acts next |
|---|---|---|
| `progress` | moving, nothing needed from anyone | nobody |
| `blocked` | an external dependency stopped the work | the conductor clears or re-plans around it |
| `question` | a decision the conductor owns is needed | the conductor answers |
| `done` | the worker claims acceptance is met | the conductor verifies |

**`blocked` and `question` differ by who must act.** That is why they are
separate values and not one "stuck". A build the worker does not control is
`blocked`; a choice only the conductor can make is `question`.

Reports belong at real milestones, not on a timer.
`summary` is capped at 500 characters and is **refused rather than truncated**
when longer, so a report that lands is a report that landed whole. Evidence goes in `artifacts` as
pointers — a branch, a commit, a path, a pull request number.

## Why `done` is a claim, and who judges it

A worker's `done` never closes an item, and neither does anything the conductor
writes. The conductor asks the **gateway** to judge it: `work_ledger_record`
`action=evaluate`. The gateway reads the item's condition as stored — never one
supplied with the request — and observes the exact revision:

For a pull request it reads the head commit, then every check run and commit
status **of that commit**, and every row must name that commit. A pass needs a
complete, counted reading with at least one check that concluded success, and
none failing, pending or in a state the evaluator does not recognise. A cancelled
or stale attempt counts as failing unless a later attempt of the same check in the
same check suite replaced it. The revision it records is the commit plus a digest
of that exact set of checks, so a check that disappears, appears or re-runs before
the close is a different revision. For a file it reads only inside the worker's
project directory as it was when the item was bound — pinned by inode, so a
directory swapped or linked away afterwards reads nothing — refusing any link on
the way and sensitive paths, and records a digest of the bytes it read.

Only an item whose worker currently reports `done`, with a concrete condition, is
evaluated — a world-state check can pass on unfinished work (a stub written
early, a pull request green before its last commit). Before observing the target,
the gateway compares the cached criterion, report and worker binding with their
crew-log projection under the board lock, then releases that lock for the external
read. A cache-only change left by an interrupted write is refused with
`crew_log_incomplete`; a new evaluation cannot legitimise an input absent from
the record. Restore the missing record, or explicitly record the criterion/report
again before evaluating. This check does not discard cached data or mark missing
history safe to overwrite. An accepted close also requires its evidence in the
canonical projection, not just a cached acknowledgement stamp.

A `file` criterion is refused on platforms without descriptor-relative no-follow
reads; the gateway does not fall back to an unconfined reader.

The answer lands in the item's `evidence`:

- `pass` / `fail` — the condition holds or it does not, for that revision.
- `pending` — not true yet (checks running, or none reported yet); keep waiting.
- `refused` — the evaluator will not answer the condition as written, and no
  amount of waiting changes that: a condition that names a command, a path
  outside the worker's project, a pull request that already merged or closed, or
  a **draft** pull request whose checks have not finished (a draft is its author's
  own "not ready", so a `pr_checks` item is opened non-draft or marked ready
  before the worker reports).
- `error` — a broken condition or an unreadable target, including a bar still set
  to `TBD` or a check board that could not be read whole.

**`accepted` means that revision was accepted.** Closing an item `accepted` needs
the current evidence to be a `pass` that the crew log is confirmed to hold, and
the gateway observes the target once more at close: a new push, a re-run that is pending or failing, or changed file
bytes leave the item open (`target_changed`). A new worker report or a new
condition makes the earlier evidence stale (`evaluation_stale`), and the
conductor evaluates again. The item keeps the evidence it was accepted on in
`acceptance_proof`; nothing watches the pull request afterwards, so a later push
is not covered by that acceptance. `rejected` and `abandoned` remain the
conductor's own decisions.

A passing check is still only as independent as the check itself: CI that the
worker could edit measures the worker's claim. For work where that matters, the
condition should point at checks the worker cannot change.

Items accepted before gateway evaluation existed keep their state and their
recorded `verdict`; they carry no `evidence`, and none is invented for them.

A conductor stops when every item is accepted, when one item has failed
acceptance three times, when the round or time budget is spent, or when a
decision arrives that no acceptance condition can settle.

## Not the same as the session ledger, or subagents

Two records carry the word, and a third mechanism gets reached for instead of
either. Confusing them is the common mistake:

| | Work ledger | [Session ledger](session-ledger.md) | [Subagents](subagents.md) |
|---|---|---|---|
| Holds | work items shared by two sessions | one session's own goal, phase, next step | a task plus a retained transcript/result, but no shared acceptance ledger |
| Who writes | a conductor and its workers, disjoint field sets | the session itself | n/a |
| Survives | compaction, restart, and the worker's own session ending | compaction and restart | the retained conversation/result for its bounded grace window |
| Unit | an item with an acceptance condition | a phase and a next step | a task string |
| Completion | settled by the acceptance evaluator | the session marks its ledger finished | the parent reads the result |
| Steerable | yes — each worker is its own session you can open | n/a | yes while running via `spawn_steer`; follow-ups use `spawn_continue` while retained |

Reach for subagents for bounded background fan-out that needs no visible,
long-lived workstream or shared acceptance record. Reach for a conductor when
each piece needs its own long-lived session, its own acceptance bar, and a
record that outlives any one transcript.

A conductor keeps both ledgers: the work ledger for the items, and its own
session ledger for its goal, its current round, and the approaches it already
rejected. Items never go in the session ledger — two records that can disagree
is the failure that avoids.

## The `kirocrew-worker` agent

A conductor names `kirocrew-worker` for a leaf item. That spec is a **superset**
of your default agent, not a narrowed one: everything your default agent grants,
plus the two reporting tools. A worker writes files, runs builds and drives git,
so anything a narrowed spec withheld would be something some item needs.

Two things are subtracted. Cron **scheduling** grants, because a recurring job
would outlive the item, the session and the dispatch — the tools stay mounted,
they just no longer run unattended. And opt-in tool sets nobody assigned to the
worker itself, so a server you mounted on your own agent is not thereby handed
to every worker you dispatch.

Because the spec is derived from the default agent on disk, a server you mount,
a grant you add and the model you pick reach the worker at its next refresh. It
is re-derived every gateway start and re-checked before every worker session.
An explicit model pick on the worker file is the one thing carried across.

`work_brief` and `work_report` are auto-approved: a worker that must ask
permission to say it is blocked will not say it, and an unattended dispatch is
exactly the case the ledger exists for.

On the pi backend the ledger works only with `agent.pi_managed` on and with
`kirocrew-work` and `kirocrew-dashboard` (plus `kirocrew-core`) listed in
`mcp_gateway.stub_servers`. Ambient pi sessions do not receive these tools. pi
itself has no `allowedTools`, so Crew applies the spec's list for it, narrowly:
a managed pi session runs a Crew tool unprompted only when the spec's
`allowedTools` names it as an `@server` or `@server/tool` ref, the call came
through Crew's sealed tool bridge, and the governance ceiling permits it. The
worker's `work_brief` and `work_report` therefore run unattended, as they do on
kiro-cli. pi's own `read`, `edit`, `write` and `bash` still ask, whatever the
spec grants, and so does a Crew tool the spec does not pre-approve. An
unanswered prompt is declined when the approval window ends.

## No dashboard page

Like the session ledger, the work ledger is **storage the agents read and
write**, not a view you browse. To see where a goal stands, ask the conductor
session — it answers from the record.

## Cleaning up finished ledgers

Nothing reclaims the ledger store automatically — not on tab close, not at
gateway start, and no agent-reachable tool can delete it. Cleanup is one
operator command:

```bash
kirocrew ledger-sweep
```

That is a **dry run**: it lists the session and conductor-work ledgers that look
finished, with each one's kind, store directory, age, and why it qualified, and
deletes nothing. It prints the purge spelling for the window the report was
built with; running it is a second, explicit invocation.

Flags: `--purge` (irreversible), `--older-than-days N` (default 30) and
`--purge-unreadable`, which also removes records the sweep could not parse —
they are listed but kept without it.

The sweep is conservative by design. A conductor holding an open item is never a
candidate at any age, neither is one with no items at all, and each delete is
re-decided under the store's own lock, so a ledger that came back to life
between the report and the purge is refused rather than erased.

## Related docs

- [Agents](agents.md): the conductor and worker specs, and how to switch agents per session
- [Session ledger](session-ledger.md): one session's own durable record — a different ledger
- [Subagents](subagents.md): in-turn fan-out, for work that needs no supervision
- [Monitor loops](monitor-loops.md): the repeated-wake mechanism a conductor patrols with
- [Agent questions](agent-questions.md): how a conductor puts a decision that is not its own to you
