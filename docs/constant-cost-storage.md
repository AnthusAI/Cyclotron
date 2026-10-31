# Constant-cost storage: a decision costs the same on day 1 and day 1,000

Status: **design for review** (2026-10-28). Nothing here is built or deployed.

## The problem, measured

Standing run `f2425834` on the lab, cycles at 10:17–10:23Z on 2026-10-28. One worker invocation runs
about 700 s and decides about 6 items in series: **about 118 s per item.**

| Part of one decision | Time | Evidence |
|---|---|---|
| The Jev call | **0.11 s** | One `system_one` request answers all six outputs. The six runtime journals record the same latency for the same decision (111.17 ms, 112.55 ms), and the batch ledger has one attempt per item. |
| Whole-journal rewrites | about 28 s | About 45 S3 objects, about **70 MB**, written per decision (S3 object timestamps). |
| Silent window | about 90 s | 91–95 s in every cycle. No log lines and no writes, between the request reservation and the recorded reply. A fresh-handle read of one output's event history (3,500 records) took 36.1 s from a laptop. The decision path calls `events_of`, `all_events` and `rows()` over the whole log. |
| Catalog reads | about 3 s | `console_phase` logs: hits take 25–90 ms, misses about 320 ms. |

The model is not the bottleneck. Storage is, and its cost grows with history, so every decision is
slower than the one before.

## Why: whole-document journals

Each `S3Journal` is one JSON document. `head.json` points to a blob holding the journal's **entire
state**, and every commit writes a complete new blob, then moves the head to it. One decision commits to
nine of these:

| Journal | Size now | What grows with every decision | Commits per decision |
|---|---|---|---|
| shared batches | 7.6 MB | `batches`: 426 entries, about 18 KB each | 3 |
| catalog | 5.3 MB | `web_jobs` (2,763), `web_items` (1,547), `actors` (2,763), `claims` (1,669) | 3 |
| workspace | 2.8 MB | `decisions` (429), `events` (432), `items`, `shares`, `usage` | about 8 |
| runtime × 6 (one per output) | 0.15 MB each, plus 3,500 event records each | `answers`, `answer_history` | about 5 each |

Three faults compound:

1. **Every commit rewrites everything.** Writing a few KB of new facts costs megabytes, and the cost
   rises with each decision, so total work grows with the square of the history.
2. **Decisions scan history.** Review routing, calibration, triggers and prediction lookups read the
   entire event log through a fresh handle, one small S3 GET per record.
3. **One answer is stored nine times.** The single Jev reply is copied into the batch ledger, the
   workspace and six runtimes, and each copy is a whole-document rewrite.

## The rule

**What a decision reads and writes does not depend on how many decisions came before it.** A spec
enforces this: the bytes written and the S3 calls made by decision number 10 and by decision number
1,000 are identical.

## Design

The storage already has the two pieces this needs: small conditional heads (`IfMatch` and
`IfNoneMatch`) and create-only record lakes (`record_lake.py`). The change is to use them throughout.

**Heads hold only what must change together, and are small.** A head holds a few counters and indexes,
never history. A commit rewrites only the head, which is kilobytes.

**Facts are immutable records, written once, found by key.** Each job, item revision, batch, decision
and answer is its own create-only object at a key derived from its identity, for example
`jobs/<jobId>.json` or `batches/<batchId>.json`. A lookup is one GET by key, never a scan.

**Running summaries replace scans.** Whatever a decision needs from history is kept in the head and
updated as each event arrives, the way `CYCLE_MARK` already avoids scanning for the next cycle number.
This covers review-routing state, calibration counts, trigger state and the latest prediction per item.
Full-history reads remain only in audit and offline tools.

**One answer, stored once.** The Jev reply is one batch record. Each output records a small pointer to
it (batch id plus answer key), not a copy.

Per journal:

- **Catalog.** Jobs, items, actors and claims become records. The head keeps only the run's queue
  (ordered pending ids and the current claim) and per-run counters. A finished job leaves the head.
- **Shared batches.** One record per batch: request reference, attempt and reply reference. The head
  keeps only the last sequence. An idempotent retry looks up its batch by key.
- **Workspace.** One record per decision, with its event and usage. The head keeps counters and
  per-item pointers.
- **Runtimes.** Answers become records keyed by request, and history summaries move into the head. The
  decision path never iterates the event log.

**Concurrency.** Records are create-only, so two writers never collide on them; only the small heads
take conditional writes. That keeps today's safety and also makes running items in parallel cheap
later, because a conflict means retrying a few-KB head, not a 7 MB document.

**Audit and reproducibility.** These are unchanged: the selection policy, seed, source labels,
criteria fingerprints and request composition stay on every decision record.

## Expected result

Per decision: one Jev call (about 0.1 s) plus a few dozen small S3 operations. That should be **about
1–2 s instead of 118 s**, constant over time. The current backlog (about 1,050 items) would then clear
in well under an hour.

## Plan (each phase is a PR; each deploy is Ryan's call)

1. **Pin the silent 90 s and remove history scans from the decision path.** Add a phase log around each
   history read, deploy it, and confirm which scans fill the window. Then replace them with running
   summaries. This is the smallest change and the biggest win.
2. **Records for batches, workspace and runtimes**, with the one-answer-stored-once rule. This includes
   a conversion script in the style of `convert_to_record_lake.py`: dry-run first, then counts and
   fingerprints checked equal before and after.
3. **Records for the catalog**: jobs, items, actors and claims.

Specs come first, beside each module, as `AGENTS.md` requires. Each phase adds a "decision 10 costs the
same as decision 1,000" spec for the storage it changes.

## After this: the shared Cyclotron

The shared Cyclotron already makes **one Jev call per item for all six outputs**. What it does wrong is
everything after the call: it fans the answer out into nine whole-document journals. Phases 2 and 3
remove that. Once the backlog is clear, the remaining architecture work is to simplify the shared
Cyclotron's record model so one decision is one record serving every site.
