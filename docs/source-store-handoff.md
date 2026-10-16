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

The existing priming modules and own API/meta/context delta are now ported through
`PrimingStorage` and a named transaction view. Its transaction commits the draft,
new catalog revisions, and explicit human feedback together. External HTTP calls
remain outside storage transactions; uncertain calls retain their existing
idempotency keys and late-response/version/cancellation checks. `catalog_priming`
reads current revision-pinned accepted evidence in one snapshot. A retracted
latest vote cannot revive an older label. Raw imported inputs and transformation
revision feedback remain separate from the final human label/rationale.

`PrimingService.examples(...)` and `Cyclotron.open(..., priming_examples=...)`
are supported. Accepted context is immutable for a created root and frozen run,
training only, excluded from operational feedback and scoring, and prohibited as
an operational target. Existing roots cannot be silently retrofitted. The native
root metadata snapshot retains the frozen examples; shared/runtime records must
still migrate as a matching quiescent bundle.

Catalog and full console snapshots now use v2 and include `priming_drafts`.
Known v1 snapshots upgrade explicitly to an empty draft table after structural
validation. `LegacyCatalogArchive` and `LegacyConsoleArchive` are explicit
read-only readers for old immutable v1 manifests; ordinary readers reject a
mismatched manifest schema. New publications contain all drafts, including raw
source, transformation feedback, accepted labels and provenance. There is no
fallback that silently omits draft evidence.

The original local GraphQL priming API is ported without changing auth/origin
controls. Its local default service is lazy to avoid making unrelated console
routes depend on optional file-format libraries. Hosted account mode rejects a
global priming service and fails closed before constructing one or touching its
catalog/transformer. An account-scoped priming host composition remains required
before hosted priming activation. This is distinct from the shared repository
conversion; no hosted priming, provider, publication UI or cloud storage authority
has been activated. Owner worktrees and publication host/UI files are untouched.

## Focused verification

The combined source-store/embedded/reviewer/evaluation regression passed **174
tests**. The integration owner's read-only publication/Papyrus harness passed
**83 tests** against this core. Final migration/boundary tests passed **17 tests** in a separate rerun after
adding legacy reviewer-schema upgrades, simultaneous-vote exclusion, injected
evaluation clock/trace/batch factories, native restart and File/injected-S3
transport. These are overlapping focused suites, not a full build or cloud proof.

## Current-core async learning parity

The storage branch now includes the existing async transport and safe learning
window behavior from `ac451753fa156a139c8a1e5a8012298afb89892e`,
`28acb77d143198e621ff3b985d3fce3b378af6b9`, and
`68fcb3ea2fe817cb62970ff28a4b3d676cc2f821`. Provider calls remain on the event
loop; observers remain on that thread. Unguarded deferred passes lend the native
lock during asynchronous optimizer calls, restore the complete sibling context,
and discard superseded proposals. Reviews arriving during a pass keep it queued;
each output runs at most once per learn call. The supersession count bounds
repeated lent windows. Per-task batch attribution, terminal cancellation traces,
and exclusion of other operations' pending work are retained.

A pass with an explicit `execution_guard` keeps ownership through provider calls.
Its guard runs before state access on each separately owned pass. It does not
lend ownership and then attempt partial authorization during exception cleanup.
This is intentionally more conservative concurrency for guarded calls; no guard
is retained globally, and the next queued operation runs its own current guard.

`PendingStorage` groups fixed derived-artifact operations. Native operation marks
and unchanged key/ordinal checks prevent cleanup of work created during a lent
interval. Shared batches use their charged attempt ordinals and fenced owner,
not the replaceable cache row ordinal. A stopped pass's own batch is settled as
interrupted with an explicit learning-abandonment marker after its audit event;
charged attempts, budgets, ownership and generation history remain intact. Only
that marker permits the existing learning-policy exception to repeat a paid
request. Other interrupted work still needs explicit retry or reconciliation.
This exception does not claim exactly-once external model execution.

Embedded root records now export `cyclotron-embedded-root/v2`, including the
`learning_queue.superseded` column. Opening a known old native root adds that
column with zero. Native restore explicitly validates and upgrades v1 snapshots;
v2 snapshots retain the exact count. Unknown schema remains rejected. Neither
this parity update nor the earlier repository extraction selects cloud authority.
Priming's own API/catalog schema delta is incorporated as described above; hosted composition remains gated.


## Priming verification checkpoint

The combined focused priming/catalog/console/root/learning/guard checks passed
122 tests. Local API/account checks passed 52 tests before final additional
fail-closed and explicit-storage-selection checks. The final focused safety rerun passed 10 tests; the publication owner harness passed 83 again. The Papyrus owner's actual
`priming_bootstrap_test.py` passed 10 tests loaded against this engine source:
accepted source/HTTP/file context, rationale, frozen snapshot and no-retrofit
behavior. This is synthetic cross-owner evidence, not a live provider or cloud
claim. No heavy build/full suite was run.


## S3 authority decision and implemented cloud slice

The user resolved the authority question: S3 contains text/canonical records and
DynamoDB remains the card catalog. [s3-cloud-storage.md](s3-cloud-storage.md)
documents the implemented S3 CAS journal, paid-request ledger, complete core
runtime repositories and injected S3 embedded SDK composition. Synthetic tests
block both SQLite connections and local-directory creation during actual SDK
predict/review/reopen/priming flows. This supersedes the earlier paused authority
choice; it does not activate the still-incomplete hosted console composition.
