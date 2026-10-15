# Canonical S3 cloud storage

The user settled authority: text and canonical records live in S3; DynamoDB is a
rebuildable card catalog. Authoritative DynamoDB claims/counters are not approved.
Local SQLite remains supported. Cloud compositions must not open SQLite, create a
FileLease/local store, or silently fill a missing repository with native storage.

## Implemented local adapter slice

`S3Journal` publishes immutable transaction records through `S3Payloads`, then
conditionally advances a small canonical S3 head. Every transaction retains its
parent reference, revision, unique commit, namespace and immutable initialization
anchor. There is no DynamoDB read on an authoritative path. Byte, record, commit
and contention limits are explicit host configuration; evidence is never pruned
or truncated to fit them. This is a bounded per-run/per-classifier design, not an
account-wide document or a SQL-emulation service.

`S3BatchStorage` implements the complete shared-request domain: one CAS reserves
the charged attempt, budget slot, owner and generation; a separate successful CAS
claims dispatch. Stale owners cannot settle successors. Raw response JSON,
original operations, generations and paid-attempt history survive restart and
export/import. Failed work requires explicit retry. Claimed work is not stolen
by timeout. Unlike the native learning exception, cloud automatic cleanup cannot
abandon a claimed request and authorize another charge. Provider reconciliation
must explicitly settle it first. Unclaimed reservations can be abandoned safely.

`S3RuntimeStores` supplies core state/events, answers/history/rounds, learning
artifacts, feature/control stores, hypothesis/diagnosis, forward evaluation and
pending-artifact repositories. An explicit S3 owner/fence is stored with the
runtime records and checked on every read/mutation. No timeout takeover exists.
The runtime retains raw text and evidence order; pending artifacts use monotonic
operation ordinals. Uncertain writes poison the handle, preventing downstream
exception cleanup from mutating potentially stale state. A confirmed release and
fresh composition can reopen committed records; crashed/uncertain ownership may
require operator reconciliation.

`S3EmbeddedStorage` commits root source cursors and derived reviews/events/usage
together. Failed publication does not expose a partial transaction. Uncertain
root commits poison the handle. `S3CyclotronStores` composes root, shared ledger
and per-classifier runtimes behind the existing SDK. Both `DecisionFlywheel` and
`Cyclotron` accept an explicit complete `storage=` composition; neither fills
missing cloud repositories with SQLite. Cloud SDK usage is
`Cyclotron.open(None, definition, model, storage=stores, max_requests=ceiling)`.
The cloud shared-request ceiling is absolute across reopen, unlike the retained
native per-open additional authorization. The factory must already be bound to
the authorized account/workspace. Console roles do not grant publication access.

The existing public execution guard remains after ownership acquisition and
before operation state. Native unguarded learning windows remain compatible;
guarded passes retain ownership during provider calls. The native binary SQLite
snapshot API explicitly refuses cloud compositions.

## S3 consistency and recovery contract

The head uses `If-None-Match: *` at creation and `If-Match` with the current opaque
ETag thereafter. Explicit 409/412 conflicts cause a bounded fresh-authority retry
of pure domain operations. Other transport failures are uncertain, even when a
subsequent read sees the write. A reread of an already-claimed request never
grants permission to dispatch again.

SDK/proxy hidden write retries must be disabled. A real botocore client's
`Config(retries={'total_max_attempts': 1}, signature_version='s3v4')` is required;
the journal rejects real clients whose retry configuration permits more attempts.
Otherwise a successful PUT with a lost response could become a later 412 and
incorrectly look like a known contention failure for an append operation.
Injected synthetic clients implement the same single-attempt contract.

The initialization anchor deliberately precedes the first head. A crash or lost
receipt between them leaves the namespace unavailable; retry does not reset a
budget or replace the anchor. Missing heads, missing anchors with existing heads,
missing/corrupt immutable records and scope mismatches fail closed. Recovery must
inspect the retained S3 versions/transactions and provider outcome; there is no
online reset/lease-steal endpoint in this slice.

Required hosting policy: versioned regional S3 general-purpose bucket, fixed
expected bucket owner and authorized prefix, SigV4 temporary runtime credentials,
conditional-write enforcement, no runtime DeleteObject/DeleteObjectVersion or
unconditional head overwrite, and retention of anchors, heads and reachable
transactions. Lifecycle/GC must not delete reachable history. Restoring an old
head is an operator recovery affecting budgets and cannot run concurrently with
workers. S3 versioning and these policies are requirements for reviewed cloud
activation; this adapter work has not provisioned or verified them.

AWS references: [conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html),
[PutObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html).

## Concrete tradeoffs and remaining work

Each active run/classifier has one head and therefore serializes its own writes.
A transaction writes a full bounded immutable domain value, then advances a head;
this amplifies bytes and S3 operations as history grows. Failed CAS attempts can
leave safe orphan immutable objects. Reaching explicit limits stops work and
requires a reviewed continuation/compaction protocol; it never erases history or
starts a fresh budget. Production sizing, retention, repair tooling and service
latency need an actual bounded cloud trial. DDB indexing cannot remove this
coordination requirement or authorize an operation.

Root/shared/classifier initialization and ownership span multiple S3 objects.
A process failure midway can leave partial acquired ownership and requires
reconciliation; no unsupported multi-object transaction is claimed. Provider
exactly-once execution is not claimed. Optimizer transport paid-call reservation,
cloud console catalog/definition/run/command repositories, reviewer/evaluation
compositions, durable OAuth sessions, worker lifecycle/queue binding, portable
cross-backend migration and account-scoped priming composition remain release
work. An S3-only SDK proof does not make the hosted console cloud-ready.

No cloud resources, policies, OAuth clients, provider calls, public push or
hosting deployment were changed by these synthetic checks.


## Verification at the initial S3 SDK checkpoint

107 focused cloud SDK/native SDK/guard/learning-window/priming tests pass. The
actual publication owner contract harness passes 83 tests against this source.
An earlier core repository/regression set passed 63 tests (overlapping coverage).
Tests include independent-thread budget races, lost reservation/claim/settlement
receipts, restart, exact native ledger migration parity, raw JSON preservation,
conditional SDK parameter validation through botocore Stubber, ownership fencing,
limit rollback, uncertain-handle poisoning, priming and an absolute reopened
cloud request ceiling. Fake providers and injected S3 boundaries only; no real
provider, AWS write, heavy build or full suite is represented by these results.
