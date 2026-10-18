# Publication contract: embedded and remote integration slice

`publication_lab.py` supplies `cyclotron-publication/v1` over an **injected,
already-open Cyclotron facade**. It owns no storage adapter, provider factory,
credentials, cloud configuration, lease, deployment or background worker. An
embedded Papyrus host invokes `EmbeddedPublicationClient`; a remote host can
expose `create_publication_app(lab, authorize=...)` and use
`HTTPPublicationClient` with its own authenticated HTTP client. Both execute the
same actual multi-output SDK and return the existing decision/review/status
shapes for the real shared UI components.

## Host integration

Construct `PublicationLab(engine, account_id=..., bindings=[...])`. A
`PublicationBinding(publication_id, outputs, revision)` pins an opaque,
host-configured publication identity to explicit classifier IDs. More than one
output can serve a publication, but distinct publications may not silently share
one feedback output. Multiple deployments of the *same* publication may consume
its same binding. No DNS/URL-to-publication inference or identity registry is
introduced. Canonical item IDs and values come from the existing application;
this module does not scrape, summarize or deduplicate references.

The host supplies a verified `PublicationActor(account_id, actor_id, grants)`.
Grants are explicit `(publication_id, capability)` pairs, where this slice uses
`decide`, `review`, and `read`. An actor/body or public string is never a credential.
The required HTTP authorizer may be synchronous or asynchronous and must return
a verified actor; no default authority, shared lab API key, login implementation,
IAM grant or service-role federation is supplied. All operations check account,
publication and capability before engine execution. Tenant/account membership
and deployment identity must come from the owning authorization service; an
injected scope is not proof of permission.

HTTP: `POST /publications/{publication}/operations` with
`{contractVersion: 'cyclotron-publication/v1', operation, payload}`. The HTTP
client escapes opaque publication identities and refuses redirects; the host
owns endpoint/TLS and transport credentials. There are no automatic retries.
Browser Origin must match Host; a same-origin BFF must validate its incoming
origin and preserve a consistent upstream Origin/Host. Do not use an open proxy.

Operations:

- `decide`: `{bindingRevision, item: {id, values}}`. The common item is evaluated
  by the existing multi-output engine; returned predictions and routing are
  projected to the publication's bound outputs. Predictions are never labels.
- `review`: `{bindingRevision, decisionId, classifier, predictionVersion,
  predictionFingerprint, requestId, label, explanation?, reasonCode}`. An explicit
  configured label is required; the human explanation is optional. The server
  derives reviewer
  identity from the verified actor, checks the displayed prediction pin and
  binding, and namespaces request identities by account/publication/actor. It
  normalizes an omitted/null explanation to an empty string before dispatch and
  rejects supplied non-text or explanations over 4000 characters. Supplied strings
  pass unchanged to the native SDK, whose existing normalization trims outer
  whitespace and stores absent/blank explanations as `None`. No rationale is invented.
  It delegates idempotency/corrections/learning to the SDK. A changed normalized
  review conflicts rather than overwrites an acknowledgement. Omitted, null, and
  blank explanations remain equivalent under the existing native normalization.
- `status`: `{}`. Per-output `cyclotron-status/v1` snapshots, unchanged for shared
  monitoring components. Snapshot metadata includes only bound output questions,
  class labels and positive label configuration.
- `history`: `{}`. Current SDK decision rows and active human reviews, projected
  to bound outputs. A limit of 200 rows fails explicitly rather than truncating;
  scalable bounded history depends on the storage owner's facade conversion.
- `events`: `{after, limit}`. Existing stable SDK event cursor, with predictions,
  labels, corrections and classifier lifecycle events scoped to bound outputs.
  The returned source cursor advances even when sibling events are filtered out.
  Use this audit stream for original and corrected explanations; `history` is an
  active-review view, not a replacement for the audit stream.

Requests/responses are bounded to 2 MB. Errors and lost transport acknowledgements
remain explicit. Callers retry only with the same unchanged identity/payload;
provider ambiguity must never cause an automatic paid replay. Existing SDK
revision and evaluation-partition firewalls remain authoritative.

## Storage and rollout boundaries

Embedded mode uses the deployment's injected engine/persistence owner; it needs
no remote lab or external Cyclotron store. Remote mode's engine is canonical for
shared items and publication-specific labels/learning. Local tests use existing
SQLite directories only as fixtures and recreate the owner across restart.
**There is no persistent EC2 SQLite hosting or production hosted-mode selector.**

Storage owner `/task-4/cyclotron-console` checkpoint
`45f8ee15057a069eb961b6a01e0240ca29fb1476` owns SQLite plus S3/DynamoDB conversion.
Read its `docs/storage-portability.md` before composition. This slice changes no
engine SQL, catalog adapter, immutable payload adapter, coordinator or cloud
resource. Full cloud execution/lease/session conversion and the cloud coordinator
remain release dependencies. Without the optional records composition, existing `decision_log` carries current
item values. The frozen revision composition below supplies historical views;
revision-specific learning still requires the runtime owner's support.

Output-scoped proposal evaluation/promotion is now available through the injected
native steering composition below. The SDK's global review-rate override remains
global and is not exposed as a publication-specific mutation. Hosted ownership,
full cloud conversion and production authorization integration remain dependencies.

## Verified evidence

`publication_lab_test.py` executes deterministic offline `classify_many` models
through the real SDK and both direct and in-process HTTP/ASGI adapters. It covers
independent common-item outputs; explicit labels with optional explanations; cross-account
and cross-publication denial; cached decisions and idempotent reviews after owner
restart; changed retry conflicts; stale binding/prediction pin denial; lost
transport acknowledgement without automatic retry; corrections and scoped event
cursors. This is real engine execution with an offline model, **not paid/live
provider or deployed cloud evidence**.

Papyrus `publication_modes_test.py` uses the existing synthetic boundary around
its actual research extraction/web metadata orchestration, then passes those
candidate values into each contract path. Source/download/provider and tokenizer
boundaries are mocked; a network guard forbids outbound connections. Optional
priming and its distinct extraction feedback remain intact. No synthetic model
is installed in production configuration.

Tracking: remote `decision-flywheel-ed9c78a5-64da-46c0-8369-aac47e91f6ce`, embedded
`decision-flywheel-046a06ad-f064-4907-96e1-b6e5941d0938`, shared milestone
`decision-flywheel-a16042c9-7357-42d1-a8ca-08387b8931bf`. Ryan authorized local
implementation after the initial planning hold; later publication/merge/rollout
work is coordinated through the release owner. This contract does not itself
activate a production endpoint, fake classifier, account mapping or data migration.

## Frozen revision and steering composition checkpoint

`PublicationLab(..., records=..., steering=...)` accepts optional compositions.
`features` in every response reports whether frozen contexts, replay export and a
steering handler are configured. Feature metadata is not authorization or cloud
activation; all actor/account/publication checks still run.

### Immutable source and binding joins

`PublicationRecords(catalog, account_id=..., source_lists={...},
binding_list=..., record_list=...)` uses only the existing injected catalog's
`item_revision`, `upsert_list_items` and `snapshot_items` operations. The host
creates/configures its account/lab-scoped lists. No SQL/file/S3/DynamoDB adapter,
mutable head, distributed transaction or separate label authority is added.
Canonical labels/learning remain in the injected SDK owner; catalog receipts are
immutable provenance joins to its acknowledged event IDs.

A reference is `{listId, itemId, revision, contentHash}`. `contentHash` hashes the
canonical **decoded JSON revision**, not transport bytes or a presumed raw SQL
fingerprint. The original immutable revision and source provenance are retained.
A binding record has only `{id, occurred_at, values}`; values are exactly
`{accountId, publicationId, revision, outputs}`. Private transport/auth material
must stay outside these public binding snapshots. A binding revision cannot be
reused for a changed output mapping. An output with recorded historical ownership
cannot silently be assigned to a different publication; a reviewed migration is
required.

When records are configured, `decide` also requires
`context: {sourceRef, bindingRef}`. The source's original ID/values must exactly
match the model item. After actual SDK execution, a content-addressed context
records the exact scoped prediction, source revision and binding snapshot. This
address names a provenance envelope only: operational item identities, partition
assignment and the model representation are unchanged. A failed/uncertain catalog
acknowledgement never becomes a successful import; retrying the unchanged call
reuses the existing SDK decision and publishes the same context.

`review` additionally requires the returned `contextId`. Its request namespace
includes that context; the verified SDK review and authoritative event cursor are
then joined to it. Original and corrected explanations retain their own source
and binding revisions. History reads the frozen source, not the SDK's latest
item-value map, and marks older contexts read-only. Missing legacy provenance is
explicit rather than filled from the current source.

The current SDK uses latest item values for learning/review. Until its owner
supports per-decision values, this composed mode refuses a changed source for an
already-labeled item and refuses review against a superseded source. This guard
avoids silently teaching from the wrong text. It is not a new learning/storage
implementation. `feedbackReconciliationRequired` marks canonical feedback changed
outside this composition; historical receipts do not overrule SDK retractions.

`replay` with `{}` returns ordered, publication/output-scoped **export records**
with original source, binding, displayed prediction and review. It does not execute
learning. SDK event cursors order receipts, not wall-clock guesses. A native
feedback event without a joined receipt (including an external retraction) blocks
replay until explicit reconciliation, rather than resurrecting a withdrawn label.
Tests replay a valid export into a fresh actual SDK store and preserve the final
corrected label/explanation without labeling the sibling output. Bounded catalog
and event budgets fail explicitly; cloud-scale indexed discovery and complete
cross-domain migration remain owner work.

### Output-scoped native steering

`PublicationSteering(records, evaluators, owner_lock=..., allow_evaluation=False)`
requires the **same existing single-writer owner lock** that protects other engine
operations. It does not create a lease or distributed coordinator. The local
integration test supplies the SDK's existing lock; the production host/storage
owner must expose/review that ownership composition before enabling execution.
The default refuses execution. Cloud execution remains off while the coordinator
choice and runtime conversion are pending.

Each `NativeOutputEvaluator(output_id, wheel, partitions=...)` wraps one existing
native wheel. The host supplies authoritative training/development/protected
partitions and their recorded propensities; none are invented by this adapter.
The existing `improve(..., candidate_proposal=..., apply_promotion=False)` evaluates
a human proposal with native fitting, evaluation firewalls and sibling guards.
The separate `promote_trial` operation applies only an already evaluated,
publication-bound trial and retains the native feedback/context/version guards.
There is no direct rubric overwrite, force promotion or global rate mutation.

The `steer` operation requires both `steer` and `read` actor capabilities. Payload:
`{bindingRevision, bindingRef, classifier, requestId, expectedFingerprint, action,
explanation, proposal}` for `action: 'evaluate'`; use `trialFingerprint` instead of
`proposal` for `action: 'promote'`. Native proposal keys are `rubric`, `tasks`,
`example_ids`, `dynamic_elements`. The explanation records steering intent and
never becomes a classification rationale or human label. Sibling metrics are
not returned as this publication's result.

Immutable intent precedes dispatch; a completed receipt makes the unchanged retry
safe even after reconstruction. A pending command with an ambiguous outcome fails
closed for operator reconciliation and never automatically reexecutes. A lost
settlement acknowledgement recovers the stored receipt without another native
execution. This guarantee assumes the existing single owner and is **not** a
claim that catalog appends provide distributed CAS. No live cloud claim, token
issuer or paid provider validation is supplied.

Reconciliation failures carry `code: reconciliation_required` through HTTP. The
remote client preserves that state and never describes an ambiguous dispatch as
a confirmed refusal or automatically retries it.

### Capacity guard after recovery

Record publication checks the prospective native catalog rows, including revision,
fingerprint and serialized list overhead, against aggregate record/byte budgets
**before writing**. An unchanged content-addressed retry fits the same membership
and remains readable at the exact budget boundary. The injected catalog remains
the only physical store; this adds no adapter or distributed reservation.

Before native steering dispatch, the single-owner gateway checks room for both
intent and completion rows and a conservative bounded result allowance. The
configurable `max_result_bytes` defaults to 64,000, bounded by the overall 2 MB
contract limit. Results exceeding that declared allowance are never truncated or
acknowledged as complete; they leave an explicit reconciliation state. Pending
and completed receipt lookup still precede dispatch, so receipt recovery does not
need new execution capacity or another evaluation. This is local single-owner
preflight, not a substitute for the pending cloud coordinator decision.

### Request-scoped account host

`create_publication_host(resolve_access)` adds multi-account host composition over
already-open labs. The required sync/async resolver receives `(request,
publication_id, capability)` and returns `PublicationAccess(lab, actor)` from fresh
verified session/account/publication authorization. Return `None` to refuse access;
a resolver exception returns a sanitized 503 without a fallback lab. Cancellation
while authorization is pending propagates without dispatch. The resolver owns
engine lifetime; this factory neither opens stores nor creates credentials.

Dynamic hosts require `X-Cyclotron-Account`, an expected-account constraint, never
an authority or instruction to choose that account. Python callers use
`HTTPPublicationClient(http, account_id=verified_selected_account)`. A mismatch
returns `409` with `code: account_changed` before any engine command. Successful
responses include host-derived `accountId`, which a pinned client also validates.
Account/actor fields in operation JSON remain forbidden. The legacy single-lab
`create_publication_app(lab, authorize=...)` retains its authentication-first
behavior and accepts the optional account constraint.

### Site authentication to the central lab

A site calls the central lab with a short-lived bearer credential that the account
service issued for the selected account and role. `site_credential_access(verify_credential,
lab_for_account, grants_for)` builds the `create_publication_host` resolver. On every
operation it requires the bearer token (`401` otherwise), asks the injected verifier
to re-verify it with the account service (`None` means unknown, expired or revoked,
`403`), requires the verified account to equal the `X-Cyclotron-Account` pin, and
derives grants only through the injected `grants_for` (no default role mapping).
There is no shared lab key, long-lived key, account-service client or issuer in
Cyclotron; the deployment's verifier calls the account service with the lab's own
approved runtime role.

The Papyrus BFF must obtain its verified selection from its trusted account host,
resolve fresh publication capabilities and forward the pin. No console role is
implicitly mapped to `review` or `steer`. Browser state/cache/component identity
must include account, publication and binding revision when the release owner
wires this endpoint. The existing Papyrus client currently validates publication
and binding only; multi-account production UI activation awaits that wiring.

These tests prove per-request routing, account isolation, stale-write rejection,
revocation/outage refusal, cancellation before dispatch and explicit retry using
the native SDK. They do not prove revocation after an SDK command has queued on
its internal owner lock. A supported public owned-operation authorization hook
remains a storage/core-owner integration dependency. There is no distributed
cloud authority or hosted SQLite fallback in this increment.

### Guarded Papyrus composition checkpoint

The queued-operation dependency now has a public core hook at owner revision
`0b903779222fdab842caa077fd8bc645e96e23e4`. Publication integration consumes this
revision through `PublicationLab.command(..., execution_guard=trusted_guard)`.
Native decide/review preflight reads run inside their guarded owner scope;
read-only publication operations use `engine.execution(...)`. Post-core provenance
writes acquire a new guarded scope and report `reconciliation_required` if that
settlement cannot complete. No native result is claimed rolled back. Steering can
use `PublicationSteering(..., owner_execution=engine.execution)` instead of a
private lock, with fresh authorization before binding/catalog reads and dispatch.
Never nest native async lock-owning engine calls in an execution scope.

`PublicationAccess` may carry the trusted per-call execution guard.
`create_publication_host` requires one by default. Only explicitly synthetic,
single-process contract fixtures may set `require_execution_guard=False`; that
opt-out is not a production backend or authorization fallback. The Papyrus
`PublicationBFF` resolver supplies a fresh account/person/publication/binding guard
and refuses an engine without the supported public hook. Account roles still
confer no implicit publication grants. Earlier notes marking the hook unavailable
are superseded by this checkpoint; cloud authority/route activation remain pending.

Verify cross-owner integration with
`python scripts/verify_publication_execution_contract.py <owner-core> <papyrus>`.
The harness loads this checkout's publication modules against the owner's actual
engine, without copying or editing its source. Local offline checks do not prove
cloud conditional mutations or deployed session configuration.
