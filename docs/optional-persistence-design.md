# Optional persistence and execution: proposed boundaries

Superseded by Ryan's explicit full SQLite + DynamoDB/S3 portability direction.
See [current inventory and implementation boundary](storage-portability.md).
The checkpoint proposal below is historical research, not the current plan.

Research checkpoint, 2026-10-14. This is a proposal, not an implemented adapter
or a cloud deployment. Hosting implementation is paused while this boundary is
reviewed. The existing local console and embedded Python engine remain the base.

## Recommendation

Keep SQLite and the local worker as the default, with no AWS installation,
credentials, Cognito account, or Prospectus connection required for recorded
playback and synthetic local use. Keep provider adapters optional. Add portable
**checkpoints first**, using the embedded engine's existing snapshot/restore
boundary. Design full console persistence and durable execution separately.

Use the actual Virtuus object-store semantics where they fit. Do not represent
Cyclotron decisions, labels, command receipts, or optimizer state as Kanbus
issues. Do not replace SQLite connections with an object-store-shaped SQL shim.

## Evidence inspected

| Source | Exact checkpoint | What it provides |
| --- | --- | --- |
| [Virtuus PR 33](https://github.com/AnthusAI/Virtuus/pull/33) | `f94d7e20771ef2a718bae4d97f773be969204ed2` | Rust `virtuus-store`: filesystem/S3 raw objects, conditional revisions, journal recovery, rebuildable catalog |
| [Kanbus PR 399](https://github.com/AnthusAI/Kanbus/pull/399) | `2541091158c6a8fd7eac50153524948a5d603c62` | Domain API over that store, local filesystem or optional AWS; pins Virtuus `639f873a73ebf1f68add2ae4b93f5fd117a539dc` |
| Cyclotron console integration | `909de6d` on `codex/prospectus-console`, based on `2a45f44` | Existing Python/SQLite console plus optional central account boundary |

The two PRs were open when inspected. Their clean local product worktrees matched
these heads; concurrent dirty worktrees were left untouched. This review did not
rerun their cloud proof scripts or claim their reported tests as our own.

Virtuus `rust/crates/virtuus-store/src/lib.rs` defines bounded object reads,
bounded listing, absent/revision conditional writes, opaque revisions, operation
identity, and distinct `Conflict`, definitely-not-applied `Unavailable`, and
possibly-applied `Uncertain` outcomes. Its default Cargo features contain no AWS.
Raw files are canonical; DynamoDB card summaries are **supplemental and
rebuildable**. S3 publication plus history/catalog repair is not a transaction.
The journal intentionally retains uncertain ownership instead of assuming that
a lease timeout can stop a late S3 request.

The current Python Virtuus exports `Table`, `Database`, and service APIs. No
Python binding for the new `ObjectStore` was found in the inspected tree. Adding
`virtuus` to Cyclotron's Python dependencies would not expose this Rust store.
Reusing it needs an explicit, optional binding or service boundary. The generic
object layer can hold arbitrary bytes; the journal/catalog adds JSON projections
and issue-oriented conventions that a binary checkpoint does not need.

Kanbus `services/kanbus-api/README.md` explicitly rejects hierarchy and relation
mutations because per-object revisions cannot enforce related-record invariants.
Its idempotent receipts and honest `catalog_pending` response are useful patterns;
its issue API and authorizer are not Cyclotron's data model or tenant authority.

## What Cyclotron actually persists

| Boundary | Existing implementation | Constraint for an adapter |
| --- | --- | --- |
| Console catalog, runs, items, command queue, API events | `web_store.py`, `workspace_catalog.py`, `cyclotrons.py`, `cyclotron_definitions.py` | Related records and frozen run inputs are committed together; mixins use SQLite directly |
| Learning engine | `flywheel.py` | SQLite runtime state, events, answers, answer history and optimization rounds |
| Multi-classifier console session | `workspace_session.py` | Per-classifier SQLite files plus `shared.sqlite3`; not covered by replacing `WebStore` |
| Reviewer and evaluation applications | `reviewer_store.py`, `matched_run_evaluation.py` | Additional independent databases and recovery rules |
| Embedded Cyclotron | `embedded_cyclotron.py` | `cyclotron.sqlite3`, `shared.sqlite3`, classifier databases; explicit single-writer lease |
| Engine checkpoint | `Cyclotron.snapshot()` / `Cyclotron.restore()` | SQLite backup, manifest/checksums, archive; caller must invoke between operations |
| Application execution | `application_runtime.WorkspaceRuntime`, `web_worker.WebWorker` | Existing normalize/open-session/execute composition seam; worker owns a thread and in-memory sessions |

`WebStore.command()` uses `BEGIN IMMEDIATE` to combine active-version checking,
request identity/content comparison, the one-in-flight-command rule and enqueue.
`claim_command()` atomically selects pending work and marks it running.
`finish_command()` can commit command completion and terminal run state together.
`recover_interrupted()` preserves pending work but marks running work interrupted;
it deliberately does not replay possibly paid calls. These are domain invariants,
not secondary indexes that can be reconstructed after a lost mutation.

The current worker has no distributed attempt token or fenced completion. Its
SQLite error retry applies to claiming work, not model calls. A cloud storage
adapter alone therefore does not make this worker safe for several replicas or
ephemeral functions. `WorkspaceRuntime` is a useful existing execution seam, but
does not promise a serializable/resumable Python session.

The existing `VectorStore` protocol is independent retrieval configuration.
`S3VectorsStore` and `DynamoDBVectorStore` are explicitly unimplemented design
stubs. They are not evidence of an existing workspace cloud backend.

## Smallest proposed storage contract

Start with a narrow `CheckpointRepository`, composing raw conditional object
storage, with a filesystem implementation and an explicitly selected optional
cloud implementation. The names below describe the proposed contract only:

```text
read_head(scope) -> None | {revision, checkpoint_id, digest, operation_id}
publish(scope, archive, expected_head, operation_id, request_digest)
    -> {head_revision, checkpoint_id, digest}
fetch(scope, checkpoint_id, max_bytes, destination) -> verified_manifest
```

`expected_head` is either absent or an opaque revision. `operation_id` is stable
across retries; the request digest binds scope, expected head and archive digest.
Changed input under the same operation ID conflicts. Exact replay returns the
original outcome, even after a newer head exists. These receipt semantics must
be specified and tested in the adapter; raw object CAS alone does not supply them.

Publication uploads an immutable checkpoint before conditionally publishing its
head. Each publication has a unique ID, including when archive content repeats.
A losing publisher leaves an unreferenced checkpoint, never a broken head.
Garbage collection and deletion are outside the first contract. A head is
authoritative; an optional listing/catalog is only a rebuildable convenience.
Do not acknowledge cloud durability while only a local working copy is current.

Bound archive bytes and manifest/file counts before full allocation or extraction;
verify digests and schema, reject traversal, duplicates and unexpected entries,
then restore into a temporary empty directory before atomically promoting it.
Budget values need agreement before implementation. Current embedded restore
provides some validation but is not yet this complete untrusted-archive boundary.

Keep conflict, definitely-not-applied failure and uncertain publication distinct.
After an uncertain head write, inspect its operation identity/receipt and reconcile;
do not blindly retry or roll the head backward. This follows Virtuus's recovery
discipline without importing its Kanbus issue projection into Cyclotron.

For reuse, prefer a thin optional Python binding exposing Virtuus's bounded raw
object operations and errors, with no Kanbus API, catalog or Rust requirement for
default local use. Confirm ownership and packaging before writing that binding.
If a service is chosen instead, its deployment/authentication becomes an explicit
additional dependency; it must not become mandatory for clone-and-run use.

The first checkpoint milestone covers the embedded `Cyclotron` store only.
Its existing archive does **not** include the console catalog, reviewer databases,
all workspace sessions or evaluation databases. A full console checkpoint needs
a versioned inventory and an application-wide quiescence barrier across those
stores. Copying live SQLite/WAL files or independently backing up each database
while commands continue cannot promise a consistent workspace checkpoint.

## Later boundaries: transactional repository and runner

If the goal becomes a fully cloud-backed console, introduce a separate
`WorkspaceRepository` at domain operations, preserving the local `WebStore`
adapter. Begin with the operations already used by one console slice rather than
rewriting all persistence at once:

- Create a run together with immutable classifier/item/definition pins and initial
  events; append events with stable source identity and ordered bounded cursors.
- Submit an idempotent command with the active-revision and one-in-flight checks;
  claim an attempt conditionally; finish it only for the current attempt.
- Commit completion, its result and affected console state under an explicit
  atomicity/recovery contract; reject stale revisions and changed request content.

Provider errors and records must not expose `sqlite3.Connection` or boto clients
to callers. DynamoDB conditional/transactional records are one possible cloud
authority for these operations, but that would be a **canonical transactional
store**, unlike Virtuus's disposable card catalog. Its key design, transaction
limits and large-payload placement require a separate review. Replacing only the
console repository still leaves the core learning-engine SQLite state to solve.

Keep a separate runner contract: claim a command attempt, acquire the required
workspace ownership, open the existing application runtime, execute within its
budget, durably settle the attempt, and release resources. Local thread execution
remains the default. A future queue/process/workflow runner must specify fencing,
checkpoint recovery, cancellation, paid-call ambiguity and acknowledgement order.
Timeout alone cannot revoke a provider call already in flight. Uncertain paid
work remains interrupted for explicit recovery, rather than automatic retry.

Central Prospectus continues to own human membership and entitlements. Validated
account IDs select scoped stores only after authorization; object prefixes and
storage credentials do not establish human authority. Local standalone use does
not require central identity. The optional accounts code remains separately
installed and does not make AWS part of the default engine.

## Phased acceptance tests

| Phase | Required proof before claiming completion |
| --- | --- |
| 0: preserve local | Fresh environment without AWS SDK/credentials or central config; recorded console opens; synthetic engine can decide/review/restart; no network or model calls in specs |
| 1: filesystem checkpoint | Same repository contract tests: publish/read/fetch; missing head; exact retry vs changed request; stale CAS; concurrent publisher; corrupted/oversized/path-unsafe archive; restore status/events/cache parity and no repeated synthetic model call |
| 2: optional cloud checkpoint | Run the same contract against injected fake clients, then a separately authorized disposable namespace; prove conditional race behavior, bounded reads, upload-before-head ordering and uncertain-write reconciliation; export to a cloud-free local install and continue |
| 3: console checkpoint | Inventory every authoritative database/artifact; stop writes across all sessions; checkpoint/restore catalog, frozen inputs, current prediction, feedback, budgets and event cursors; pending stays pending, interrupted work is not replayed; no torn multi-store generation |
| 4: transactional repository | Same local/cloud domain scenarios for run creation, version conflict, request idempotency, one active command, crash points and event cursors; no partially acknowledged related-record state |
| 5: durable runner | Duplicate deliveries, process death before/after external call and checkpoint/ack, stale attempt completion, ownership loss and shutdown; synthetic counters prove no automatic replay of uncertain paid work |
| 6: hosted accounts | Signed synthetic identities; denied tenant and viewer writes; account switching and stale-tab requests; logged-out HTTP/WS/timeline access; then approved live Google/callback/runtime-role/browser proof |

Only existing local behavior was tested in this design checkpoint:
`embedded_store_test.py` covers single-writer refusal, application leases,
snapshot/restore status/events/cache parity, corruption rejection, redaction and
crash recovery. The table lists future acceptance, not new passing tests.

## Decisions needed before implementation

Agree that the next implementation milestone is embedded checkpoint portability,
or explicitly choose the larger console-repository migration. Decide who owns the
optional Virtuus Python bridge and its publication/versioning. Set archive limits
and checkpoint acknowledgement/recovery semantics. Choose a cloud runner and
hosting only after the required persistence guarantees are concrete.

No adapter, runtime, OAuth client, role, policy, table, bucket or DNS record was
created for this proposal. Live Google sign-in and `lab.cyclotron.anth.us`
deployment remain unverified. See [central accounts](central-accounts.md) for the
separate implemented local integration and its remaining deployment prerequisites.
