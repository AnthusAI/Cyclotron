# Embedded, reviewer and frozen-evaluation source stores

The native embedded root, reviewer source and frozen-evaluation stores now have
explicit domain repositories. Public decision/review/status/monitoring behavior
and native SQLite files stay compatible. These changes do not select a cloud
coordinator or supply distributed execution ownership.

## Embedded root

`EmbeddedStorage` / `SQLiteEmbeddedStorage` own the existing eight tables: `meta`,
`items`, `decisions`, `reviews`, `events`, `learning_queue`, `usage`, `usage_shares`
and their native indexes.
The SDK uses named operations for metadata, decision history/current state,
reviews, ordered subscriptions, usage attribution and deferred learning.
`root_storage.transaction()` groups these domain operations without exposing SQL.

The SDK still owns validation, routing, learning policy and version tracking.
The public `execution_guard` runs after the existing execution lock and before
operation state access. Root transactions occur inside that ownership boundary.
Derived reviews/events/optimizer usage and each classifier's durable source cursor
commit together. If a page fails, its in-memory seen cursor and version tracker
also revert, allowing an in-process retry to derive it once. Completed earlier
pages remain committed. Observer acknowledgement is not a transaction and can be
redelivered/read through the durable subscription cursor.

`engine.db` remains native compatibility for the existing binary snapshot API;
SDK algorithms no longer issue SQL through it. `snapshot`/`restore` continue to
backup/restore root, shared requests and per-classifier files together. A root
repository snapshot alone is not a complete engine bundle. The native writer
lease and per-open-engine lock still govern this composition. No cloud lease,
claim, durable session or worker lifecycle is created by these interfaces.

Paid-attempt authority remains in the shared batch repository. Root usage is
attribution/observability, not a replacement budget counter. This extraction does
not promise recovery of an unpersisted in-memory usage batch after arbitrary
process death, or cancel an external request after authorization changes.

## Reviewer source

`ReviewStorage` / `SQLiteReviewStorage` own study metadata, immutable article
sources, displayed predictions and append-only human actions. `ReviewStore`
retains the same constructor and human eligibility/metrics algorithms, with an
optional `storage=` composition seam. Partition assignment, explanations,
pre-vote presentation identity, audit exclusions, skip and undo remain shared.
Legacy author/journal/presentation-column migrations remain native.

Study settings now bind atomically: a conflicting setting cannot leave a new
seed partially inserted. Vote/skip active-action checks and undo selection commit
with their new event under native execution ownership, avoiding simultaneous
active votes. Source import retains exact-content idempotency, legacy metadata
upgrade before review and preservation of the metadata a reviewer actually saw.
The reviewer flywheel's trigger reads runtime evidence through `RuntimeStateStorage`.

## Frozen evaluation

`EvaluationStorage` / `SQLiteEvaluationStorage` own the frozen per-plan clock and
stable acknowledged trace identities. Concurrent binds retain the first clock;
re-delivery retains the first event ID and original payload/timestamp. Native
AUTOINCREMENT gaps on ignored retries remain compatible and are preserved.

`evaluate_matched_runs` accepts `execution_storage=` and a trusted
`batch_storage_factory(side)` composition seam. Each factory call returns a fresh
handle to that side's same durable namespace; evaluation closes the handle.
Request totals and both endpoint caches use the existing `BatchStorage` contract,
not a parallel SQLite counter or new request cache. The default directory layout
is unchanged. Frozen model/head/plan validation and cumulative endpoint ceilings
remain in the shared evaluator. This does not make a distributed budget authority
choice or claim that a supplied partial cloud composition is deployable.

## Bounded native migrations

`domain_snapshots.py` supplies `embedded_codec()`, `review_codec()` and
`evaluation_codec()` over the shared `NativeDomainCodec`. Native storage adapters
expose `export_storage(max_records=..., max_bytes=...)` and
`restore_storage(snapshot, ...)`. Restore requires a fresh/empty compatible
native destination with no open transaction. Snapshots carry fixed raw columns,
physical `_rowid` and native sequence watermarks, never executable SQL.

Export reads one snapshot, enforces byte/record preflights and rejects unknown
schemas/views/triggers. Import copies/validates records first against trusted
schemas and native unique/reference/check constraints, then commits tables and
rows together. A failed restore rolls back newly created tables as well as rows.
Known reviewer column-order migrations are accepted; missing legacy constraints
are accepted only where records satisfy the current trusted schema. Added unknown
constraints are refused. Trace's AUTOINCREMENT watermark survives deleted rows
and acknowledgement retries; an already-used empty trace destination is refused.

`DomainArchive` reuses the existing immutable `CatalogArchive` File/S3 transport,
with each codec's fixed schema and sequence metadata. It introduces no runtime
head, membership authority, SQL shim or cloud store. These three snapshots must be
paired with the matching shared-cache/runtime/console snapshots under host
quiescence. Source cursors cannot be safely combined with unrelated classifier
history, and an evaluation clock/trace cannot substitute for its endpoint caches.

## Priming compatibility gate

The integration owner supplied existing core priming pins
`7a76d970995aabacec85400719cf4be17baecf8e` and callback increment
`ead4b382e2f7e0417a7848cb2151b9051d2249f4` in `task/cyclotron-current`.
Required shared inputs are `priming.py`, `priming_context.py`, `priming_files.py`,
`priming_drafts`, accepted item/feedback/retraction semantics, the embedded
`priming_examples` API/meta binding and frozen runtime/workspace priming context.
Papyrus accepted bootstrap is `ebdbff1`.

This branch's root migration preserves raw metadata (including an existing
`priming_examples` key), but that does **not** establish priming API/catalog
compatibility. The existing priming behavior must be incorporated through shared
interfaces and covered by migration tests before cloud enablement. Its owner
worktrees and publication host/UI files are untouched; no competing priming
implementation has been created.

## Focused verification

The combined source-store/embedded/reviewer/evaluation regression passed **174
tests**. The integration owner's read-only publication/Papyrus harness passed
**83 tests** against this core. Final migration/boundary tests passed **17 tests** in a separate rerun after
adding legacy reviewer-schema upgrades, simultaneous-vote exclusion, injected
evaluation clock/trace/batch factories, native restart and File/injected-S3
transport. These are overlapping focused suites, not a full build or cloud proof.
