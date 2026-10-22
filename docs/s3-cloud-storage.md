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
cross-backend migration remain release work. Account-scoped priming is composed
(see "Hosted priming" below). An S3-only SDK proof does not make the hosted console cloud-ready.

No cloud resources, policies, OAuth clients, provider calls, public push or
hosting deployment were changed by these synthetic checks.


## Record lake: history as write-once files (primitive; job results use it)

**In plain language.** Today each journal is a binder that is photocopied in full for every
change: to add one log line we copy the whole binder, file the old copy away, and move a sticky
note to the new one. That is fine for a small binder and wasteful for a big one, which is why the
4 MiB cap exists. The record lake is the alternative for anything that is only ever added to
(events, raw model replies, job results): each record is its own small page in a folder, written
once, never changed, numbered in order. Adding a line writes one page, not the binder.

**Why only control state needs all-or-nothing commits.** The few facts that must change one at a
time are who owns a job, how many paid calls were made, and which version is active. Those need the
sticky-note mechanism (the existing journal, unchanged): a conditional write that only one worker can
win. History pages do not compete: nobody edits page 993 while someone else edits page 994. So the
journal stays, but shrinks to a small control state, and history leaves it.

**Layout.** `<account>/<workspace>/records/<domain>/<kind>/<sequence, 12 digits>.json`, for example
`.../records/runtime/events/000000000993.json`. The fixed `records` segment lets the bucket policy
demand `If-None-Match: *` for the whole prefix, as it already does for `payloads/` and `anchor.json`.
Each page is a JSON envelope (schema, namespace, sequence, value). It is size-bounded per record
(`max_record_bytes`, explicit at construction), not per collection. A page whose envelope does not
match its position is corrupt and is refused, so a copied page never passes as another.

**Ordering: sequence numbers covered by the control state, not ULIDs.** The control state holds, per
lake, `{"last": N, "skipped": [..]}`. The next record is `last + 1`, written create-only. A reader trusts
exactly the sequences `1..last` minus `skipped`. ULIDs would need no allocation, but nothing small can
say which ULIDs count: an abandoned write with an older ULID would fall inside any watermark, so the
state would have to list every id and grow like the history it replaces. A sequence gives one small
fact, a deterministic order that depends on no clock, and listing that is a plain sorted range. If a
create finds a different record already at the sequence (an abandoned write, or a racing writer), the
writer skips it, takes the next, and the commit records the skipped number. Identical bytes already
there (a retry after a lost response) are adopted. Two racing writers are therefore safe even without
the owner/fence rule: the head compare-and-set picks one, and the loser's pages become orphans.

**Commit protocol: write the pages, then flip the head** (`commit_with_records`). Write the new records
first. Then commit ONE small journal change that stores the new coverage and whatever else changed
(counters, status, a digest of the last record). That commit is the only moment the records count.

- Failure before the commit (crash, lost head race, uncertain outcome, journal size limit): the pages exist
  but no control state covers them. Readers cannot see them. Nothing is rolled back and nothing is deleted.
- A reader holds the control state it read. It reads records by sequence (GET only, no list permission), so
  a head that moves meanwhile does not change what that reader sees.
- A covered record that is missing is an error (fail closed), never skipped.

**Orphans.** An orphan is a stored sequence the current coverage does not cover: a number above `last`
or listed in `skipped`. They stay in the same prefix, are listable (`RecordLake.orphans(coverage)`), and are
ignored by every reader. The application never deletes them (the bucket grants no delete). A later janitor
may report them, and may remove old ones only after a reviewed step that holds the writer's ownership so
a number above `last` is not simply an in-flight write. At most 64 skipped numbers are kept in one control
state; beyond that appends stop with an error to be reconciled rather than growing the state.

**Back ends.** `MemoryRecordLake` (tests), `FileRecordLake` (local directory, same layout, create-only via
link) and `S3RecordLake` (create-only `PutObject`, one retry for transient GETs through `s3_transient`,
and the same one-attempt SDK requirement as `S3Journal`). One contract suite
(`record_lake_contract_test.py`) runs against all three.

**First caller: catalog job results (S2c).** `S3ConsoleStorage(journal, results=lake)` writes each finished
job's result (completed, failed or interrupted) to the `console/job-results` lake and keeps in the catalog row
only a small reference (`cyclotron-job-result-ref/v1` with domain, kind, sequence, digest and length) in
place of the embedded JSON. The row, the claim `{owner, fence}` and the lake coverage (`records` in the
catalog value) stay in the catalog. `finish_command` checks the claim and fence, writes the record, then
flips the job to its terminal status with the reference in ONE `commit_with_records` commit. A worker that
loses its claim, or whose commit fails or is uncertain, leaves only an uncovered page that no read can see;
repeating the identical completion adopts that page. Claim, fence, one-worker-at-a-time and idempotent
re-finish behave exactly as before. Reads (`jobs`, `command_envelope`, `job_result`, the publication receipt
route) fetch the record by reference, check the job id and the digest, and refuse a reference the catalog
does not cover. `jobs(run, results=False)` lists without fetching, and `status_snapshot` uses it to fetch
only the one result it returns. A portable export embeds results again so a snapshot stays self-contained.
With no lake configured, or for results already embedded in an existing catalog, behaviour is unchanged
and embedded results keep reading; a later conversion step moves them. Measured on the synthetic job
(about 4 KB result), the catalog grows about 6.1 KB per completed job when embedded and about 0.57 KB
with the lake (the rest is the command payload and the reference), flat as jobs accumulate. Priming drafts,
items, lists and classifiers stay in the catalog. The hosted runtime and the operator recovery tool both
open the store with the lake. The existing `*/console/records/*` grants cover its objects, so no further
IAM or bucket-policy change is needed.

**Still to come.** Events and raw model replies move out in S2a and S2b, and S3 converts existing data.
Deploying the lake needs the `records/` prefix in the lab stack (`infra/lab/pending-changes.md`).

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

## Console domain adapter checkpoint

`S3ConsoleStorage` now supplies classifier/item revisions, feedback and retractions,
immutable displayed predictions, user definitions/activation, frozen run creation,
items/status/events/summaries, editions/checkpoints and priming transactions. The
classifier revision and every active definition advancement commit in one S3 CAS;
run inputs and definition pins commit together. Complete v2 native console
snapshots import/export through pure record validation with no SQLite connection.

The console value is bounded per authorized workspace. This initial adapter
rewrites its complete bounded record value per transaction, including history.
It is not a scalable unbounded account database: saturation stops with an error.
The same explicit head-contention/write-amplification and continuation limitations
apply. Sharded immutable record manifests are the proposed next scaling step;
DDB must remain an index and cannot become their activation authority.

Cloud command claims require an explicit UUID worker-operation owner. A confirmed
claim grants a process-local capability; a lost receipt or new process cannot
obtain execution permission by merely observing a running row. `for_command`
returns a limited per-call view bound to the command, run, owner and fence. Its
mutations test current running ownership inside the same head CAS as the data
change. Settlement requires the confirmed owner/fence and commits terminal
command state with run exhaustion. Stale views cannot write, and views cannot
escape through a raw store/SQL connection. Global startup timeout recovery is
refused. Reviewed scoped recovery and actual ephemeral worker binding remain
required; the legacy worker cannot silently run this adapter unfenced.

Priming acceptance uses the same transaction as its draft/catalog/human feedback.
A file draft whose creation committed but lost its receipt can retry the same
raw input from `starting` to preview. It cannot replace an accepted/cancelled or
newer draft and never implicitly accepts examples. External transformation calls
remain outside the transaction.

Synthetic proof at this checkpoint: 74 focused console/priming/account checks
pass, including 23 existing public native-domain contracts rerun against S3 with
SQLite and directory creation blocked. An actual hosted-mode GraphQL app with
synthetic verified sessions and central authorization switches two scoped S3
workspaces; logged-out, revoked, unknown-account and viewer-write requests are
denied before any S3 data access. This is not real Google/Cognito/IAM/browser or
deployment proof. Hosted priming was composed afterwards (see "Hosted priming").
Comparison command lifecycle, optimizer reservation, remaining auxiliary stores,
reviewed recovery, durable sessions, deployment and actual cloud verification
remain. No persistent IAM/S3 policy changes or hosted SQLite fallback occurred.

## Hosted priming

Priming runs on the account's `S3ConsoleStorage`, which is both the catalog and the
`PrimingStorage`: `transaction()` yields the store itself, so a draft write, the imported
item revisions and their human votes commit in one conditional write of the catalog journal.
`HostedRuntime.workspace()` composes `PrimingService(store, storage=store)` with a client
that refuses any transformer call, so hosted priming is file import only. There is no SQLite
fallback.

The bearer curation routes (`/api/newsroom/curation/priming`, see
`publication-deployment-contract.md`) wrap that storage in a publication-scoped view:

- draft identities are stored under `pub-<32 hex>:`, a digest of account and publication, so
  two publications never share or collide on a draft name; an identity that already looks
  namespaced is refused before storage is opened;
- the view exposes one configured list (`priming.listId` in the trusted publication target)
  and only the binding's classifier revisions; any other list, classifier or draft id is
  refused before a storage read;
- slice 1 caps one import at about 1 MB. Drafts, accepted items and votes share the account's
  single catalog journal (4 MiB by default, `max_records` 10000), so a large import can also be
  refused by the journal budget. Both refusals leave the catalog unchanged (`413`).

`accept` is idempotent: repeating the same acceptance returns the accepted draft and inserts
nothing; a different acceptance of the same draft is refused. The same behaviours run on
memory, SQLite and S3 in `priming_storage_contract_test.py`.

## Evaluation, reviewer and optimizer adapters

`S3EvaluationStorage` holds an explicit fenced owner for the entire paired
comparison, including both endpoint ledger reads and all budget reservations.
It preserves the first frozen clock, first acknowledged trace payload and native
sequence watermark/gaps. Native evaluation snapshots import/export with pure
record validation. The evaluator accepts a complete injected composition with
`directory=None`; it creates no local directory or SQLite fallback. The host
must acquire this owner and release it in `finally`; a crashed owner is not
stolen by timeout. Uncertain trace writes poison the handle; a confirmed release
and fresh owner can retain the original committed event on retry.

`S3ReviewStorage` retains native source snapshots, study seed/audit rates,
immutable article content, shown prediction identity, human rationale and
append-only vote/skip/undo actions. Study binding and human-action checks commit
atomically. A conflicting human-action transaction asks the caller to refresh,
never replays a form body automatically. Train/audit/eligibility algorithms remain
in the existing ReviewStore, which accepts `path=None` with explicit storage.
Raw native reviewer snapshots migrate without opening SQLite in cloud code.

`S3OptimizerTransport` wraps the existing sync/async completion callable with a
separate canonical S3 batch ledger and absolute paid-call ceiling. Each intentional
call is a fresh generation, preserving repeated-prompt search behavior. A pending
or uncertain generation blocks another call with that request fingerprint;
cancellation or a lost dispatch receipt never authorizes automatic repayment.
Raw replies/usage/tool calls persist in S3. Transport/model/settings identity,
request/reply byte budgets and credential redactions are explicit host inputs.
Provider SDK retries must remain disabled; this wrapper meters invocations and
cannot detect undisclosed physical retries inside an arbitrary custom callable.
The existing built-in provider setup already configures its retry policy.

Enabled optimization in an S3 runtime/SDK composition now requires this durable
paid-call wrapper; plain in-memory optimizer budgets are rejected before a cloud
SDK namespace is created. A disabled optimizer remains valid for prediction-only
flows. No paid provider was called in this verification.

## Measured access cost and owned snapshot optimization

A synthetic initialization + one decision + one human review + close, with one
classifier and no optimizer, initially issued 279 GETs and 75 PUTs. It wrote
121,697 bytes across PUTs; the largest object was 13,971 bytes. This is operation
and byte accounting against the injected client, not measured AWS latency.

The SDK now retains confirmed immutable root/runtime values within its exclusive
ownership. After the human/tenant execution guard succeeds, each separately owned
SDK operation refreshes root/runtime authority before state access. Every write
still uses the canonical conditional head, and a conflict/uncertain outcome
invalidates cached state. A failed authority refresh cannot fall back to an old
value. Only the SDK composition enables this mode; direct runtime adapters keep
fresh-read behavior unless the host explicitly owns its boundary. Shared paid
claims/counters continue to read fresh S3 authority.

The same synthetic flow now uses 90 GETs and 75 PUTs, with unchanged bytes and
results. This removes redundant reads, but write amplification remains material.
The concrete next scaling proposal is immutable per-record/page manifests with
a single small scoped activation head, preserving the same conditional commit
and budget/fence rules. Production sizing should follow a bounded real-cloud
trial; do not interpret synthetic timing as production readiness.

The complete focused adapter/native-boundary run passed 99 tests. Additional
uncertainty/limit/optimizer/guard checks passed 67 tests before the owned-snapshot
optimization; focused SDK/refresh checks cover fencing and denial after it.
Comparison command orchestration, ephemeral worker/session binding, reviewed
recovery/retention and real cloud access/build/browser verification still gate
hosted deployment. No always-on SQLite, authoritative DynamoDB coordinator, cloud
policy/resource change or paid live test was introduced.


Final auxiliary/owned-snapshot checkpoint: 102 focused cloud adapter and native
boundary checks pass; 66 focused core SDK/optimizer/authorization/window checks
passed during the cache change, followed by 12 targeted ownership-refresh tests.
The publication/Papyrus contract harness passes 83 tests against the final core source. These sets overlap and must not be added as distinct test counts.


Worker binding must claim inside the actual worker invocation; a dispatcher
cannot forward a claimed row to another process and treat observation as a
receipt. Human/account authorization and service ownership remain separate:
queue execution needs fresh central human/tenant authorization and, where
applicable, separate publication grants/current binding. Command claims alone
confer neither. Persisted trusted actor/session provenance and scoped session
continuation are still part of that worker/session integration, not supplied by
browser payloads or this storage adapter.

## Durable session and queued actor checkpoint

`S3OAuthSessions` now encrypts pending OAuth proof and access/CSRF tokens before
publishing bounded S3 journal records. KMS envelope encryption binds exact key,
application, kind and hashed reference. OAuth consumption requires a confirmed
CAS and remains one-use across processes. Logout cannot be undone by stale saves;
Person and original token are immutable once bound. Routes explicitly persist
updates and offload blocking S3/KMS calls from the event loop. Session expiry is
still bounded by the original access token and one hour: no refresh continuation
or worker reenrollment is introduced. Public quota contention/outage fails closed.

`QueuedAccountAuthorization` captures the original session reference, actor,
account, run, binding and operation in an integrity proof made with the retained
access token. No JWT is stored in job provenance. Each guard rereads the encrypted
session, verifies its JWT, checks central `/me`, authorizes the pinned account and
resolves the current binding. A tab's selected-account preference cannot retarget
work. `S3ConsoleStorage` v2 atomically commits job and actor; v1 upgrades contain
no invented actors and existing jobs cannot be adopted through idempotency retry.
Claim receipt fencing protects actor retrieval. `command_recovery` is read-only
scoped diagnostic evidence, not an automatic claim/paid-request reset.

### Reconciling a wedged command (operator)

A worker that claims a command and then fails without a terminal receipt leaves the job `running`. The
worker never takes it over, every redelivery fails closed and ends in the dead-letter queue, and the run
refuses new commands with `run_busy` (409) because it "has work in progress". The operator command ends
that one job, deliberately and with an audit trail:

```
python -m decision_flywheel.command_recovery diagnose --profile P --expect-aws-account ACCT \
    --books-bucket BUCKET --cyclotron-account UUID --job JOB
python -m decision_flywheel.command_recovery reconcile --profile P --expect-aws-account ACCT \
    --books-bucket BUCKET --cyclotron-account UUID --job JOB --owner OWNER --fence N \
    --as failed --reason "why" [--dlq-url URL] [--apply]
```

* `diagnose` is the read-only `command_recovery` evidence: status, claim owner and fence.
* `reconcile` is a **dry run unless `--apply` is given**. It asserts the AWS account first.
* The recorded owner and fence must match exactly (copy them from `diagnose`).
* It refuses when the job has a recorded result, when the job's item has a displayed or retained
  prediction, or when a run event names the job or its request id. These are the catalog's only traces of a
  completed provider call; a job that left none can only have died before a prediction was recorded. The
  provider's own ledger is not consulted, so the `--reason` should say why no paid call completed.
* On `--apply` the job becomes `failed` (or `interrupted`) and its result is the audit record: job id,
  owner, fence, prior and new status, the reason, the operator (the caller's IAM ARN) and the time. The status
  change and the audit record are one catalog commit. The run accepts commands again.
* With `--dlq-url`, only the dead-letter message naming that job is deleted (other messages are released
  untouched; a dry run deletes nothing). A terminal job needs no redrive: redelivery would just return its
  status.

It never claims a job, never resets a fence and never runs a provider call.

These seams are not yet a hosted worker composition. The actual S3 SDK tests show
that a guard denied after lock waiting touches no engine S3 state or provider.
Central cross-role policy is a separate pending owner change, not proven by these
same-principal synthetic tests. See `embedded-execution-authorization.md` for
the public execution-guard contract. Cross-role IAM policy and infrastructure
approval remain host-owned deployment work outside this source release.

## Hosted composition checkpoint (supersedes earlier release-work notes)

The earlier adapter checkpoints above are historical. Hosted worker/session
composition is now implemented in `hosted_runtime.py`, `hosted_console_service.py`
and `hosted_command_worker.py`, using `S3WorkspaceStores` with the existing native
WorkspaceSession and frozen priming. The worker claims the exact canonical job
inside its invocation; original human/session authorization and ownership are
rechecked before provider dispatch and business writes. Encrypted sessions remain
in S3/KMS; queue messages carry references only. Explicit bounded HTTP event
polling is freshly account-authorized per page, and native WebSockets are retained.

Separate BFF/worker delegation is locally tested against the actual central
native harness at e2bb8ff (15 checks). It is not activated centrally. The latest
focused hosted/session/transport backend set passes 89 tests; native workspace,
runtime, learning-window and priming regressions pass 74; cross-owner publication
compatibility passes 83. Sets may overlap with historical counts; do not sum them.
Actual packaged Linux Python 3.13 Gateway/S3/worker smoke tests use synthetic
providers, no network and forbidden SQLite/local persistence. Browser proof covers
synthetic owner feedback, viewer switching and logout.

Reviewable SAM resources, pinned Linux artifact and normal Amplify packaging are
prepared. Actual-cloud conditional-write trials, Google client/policy approval,
central activation, IAM provisioning, DNS and hosted browser proof remain gates.
Paid/provider commands are disabled in the first environment composition. No S3
claim implies unrestricted scale: whole-workspace bounded rewrites and per-poll
reads remain material. The protocol and recovery limits in this document and
`source-store-handoff.md` define the public storage boundary. Deployment proposals,
IAM provisioning and operational approval remain separate from this source release.


Optional priming explanation follow-up: Publish requires the configured human
label, while omitted/null rationale normalizes to an empty stored string. Empty
or whitespace-only comments contribute no `human_feedback` model-context key;
nonempty supplied text remains exact, and frozen runs cannot be retrofitted by
later catalog edits. Global `LabeledItem.context` validation remains unchanged.
43 focused priming/API/injected-storage/S3 frozen-workspace tests pass. Shared
PrimingPanel source is separately owned at 0123708a3ccb52e89bac5821a383b2643e530dcb;
this storage port does not copy the publication UI overlay or alter operational
publication review rules. No operator records or browser tabs were changed.
Shared UI consumer artifact supplied directly to Papyrus by its owner:
`cyclotron-0.2.1.tgz`, SHA256
`403e8d24d2e69f666159518c567f49c1484001eacf181d7f3c939a732cfcb3b7`.
The owner reports 31 native and 8 shared UI checks; these are separate from this
branch's 43 focused storage/API checks.
