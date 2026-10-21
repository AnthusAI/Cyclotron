# Hosted publication prediction and priming composition

This source change connects the existing publication SDK to the existing hosted
console BFF, durable command queue, original human session authorization, worker,
and native S3 Cyclotron stores. It creates no deployment resources or grants.
Live prediction remains disabled by default. A separate data-only operation applies accepted human priming through the native engine without constructing a provider or running an optimizer. Retrospective review, training and publishing are not activated by these routes.

An operator supplies trusted publication targets: one existing account UUID, one
existing frozen run UUID, and distinct publication/output/revision bindings. The
run refers to one versioned Cyclotron containing all publication relevance
classifiers and a frozen source item list. Requests must match an existing source
revision exactly. A shared decision is computed across the whole classifier set;
each caller receives only its configured publication output projection. Subsequent
projections reuse native persisted decision state rather than running separate
publication models. Configured publication runs reject legacy console commands
that would operate on a different runtime or apply feedback.

The BFF requires its existing verified session cookie, same-origin CSRF protection
for writes, explicit account pin, and fresh Prospectus account authorization.
The queue contains references only. The worker checks the original session,
frozen run binding, execution fence, and configured publication binding again
before opening state and before provider dispatch. Native S3 ownership and CAS
remain authoritative. The shared batch journal maps to the existing
`shared-batches` storage domain; there is no new IAM prefix or local SQLite store.

`POST /publications/{publication}/operations` accepts the existing
`cyclotron-publication/v1` contract. `decide` returns HTTP 202 with a durable
`jobId`, never a fabricated prediction. The SDK polls the locally derived
same-origin `GET /publications/{publication}/commands/{jobId}` path with redirects
disabled and a bounded deadline. It never repeats the POST. Pending or uncertain
outcomes require reconciliation. Completed responses use the existing native
publication contract. `status` serves a verified completed worker snapshot;
before the first completion it returns 503. `prime` uses the same queue and receipt path described below. Other operations return 503.

## Explicit activation configuration

The existing required `CYCLOTRON_*` host configuration still applies. These
additional ordinary application settings are parsed without creating resources:

* `CYCLOTRON_ALLOW_LIVE`: exactly `true` or `false`, default `false`.
* `CYCLOTRON_PUBLICATION_TARGETS`: JSON list of
  `{"accountId":"<existing UUID>","runId":"<existing UUID>","bindings":[{"publicationId":"<name>","outputs":["<classifier>"],"revision":"<reviewed revision>"}]}`.
* `CYCLOTRON_PREDICTION_MODEL`: explicit reviewed Jev model identity.
* `CYCLOTRON_PREDICTION_MAX_REQUESTS`: integer from 1 through 100. Frozen run
  provider/model/budget must match this reviewed ceiling; automatic run priming is rejected and
  the optimizer is disabled. Existing provider credentials are resolved lazily
  inside the authorized worker, not at BFF construction.

No actual account, publication target, model, or budget is selected in this PR.
Nothing here changes infrastructure, deploys a Lambda, installs credentials,
grants account access, or submits real feedback.

## Remaining integration gates

The currently deployed lab artifact lacks these routes and has live providers
disabled. Its existing artifact publisher and a separately authorized deployment
must be confirmed. The signed-challenge login protocol is supported by the source and has been verified through normal Google sign-in. Successful sign-in does not establish a publication target: the selected account must contain the reviewed current classifier revisions, shared Cyclotron and frozen run before any native application. No target, principal or publication mapping is inferred from account display names. The deployed artifact also contains session/telemetry changes absent from current `main`; its owner must reconcile those changes before building a release so the source update preserves existing security behavior.

This composition uses the lab's existing cookie/CSRF authorization. Papyrus's
server-to-server Prospectus credential resolver and product registrations must be
verified against the deployed authority before connecting its clients. This PR
does not accept arbitrary bearer tokens or invent grants to bridge that gap.
Publication canonical corpora and integrated section-fit/selector bindings must
also be verified; Anth.us and Pilobolus currently have no verified live corpus.

The four-publication tests use explicitly synthetic sources, accounts, labels,
and a fake provider. They are evidence for composition, queue behavior, native
shared decisions, and access rejection, not evidence of live relevance quality.
Evaluation answers remain held out from their own baseline. Separately approved genuine human priming examples do not require a prior prediction; they must remain disjoint from operational/evaluation inputs. Native application, retrospective feedback, classification and optimizer training have separate receipts. The subsequent publication-integrated section fit and selector stage
remains Papyrus's responsibility.


## Accepted human priming without provider execution

The existing account-scoped GraphQL `primingCommand(file)` and `primingCommand(accept)` intake validates explicit human labels and optional reasons against selected classifier revisions. Both are pure data operations; acceptance alone does not apply examples to an engine. Unspecified labels stay outside that output's priming import. No prior prediction, synthetic decision or retrospective review is fabricated.

After acceptance, an authenticated same-origin request may submit:

```json
{"contractVersion":"cyclotron-publication/v1","operation":"prime","payload":{"bindingRevision":"<reviewed revision>","listId":"<accepted list>"}}
```

The target must declare an additional trusted `modelIdentity` naming the exact adapter identity the native classifier store will use later. This prevents pure-data initialization from binding the store to a placeholder provider identity. A subsequent live prediction adapter must match it. Missing identity, stale classifier revisions, pending criteria and operational/evaluation item overlap are refused before application.

The BFF freezes a fingerprint of the selected publication outputs' accepted catalog examples, explanations and classifier revisions. Only account/run/job references reach the queue. The worker re-reads that accepted snapshot and invokes native `add_priming_examples` with the original authenticated person as `added_by`. Browser-supplied labels, authority claims and provider objects are not execution inputs. Every native journal initialization/commit checks the original session and command fence; changed binding, revoked authority, changed catalog or uncertain ownership requires reconciliation.

`prime` works with `CYCLOTRON_ALLOW_LIVE=false`. It constructs no provider factory, uses a model that refuses dispatch and a disabled optimizer, and runs only the native data operation. The SDK requires a positive request ceiling on open; the refusing model makes that ceiling incapable of external dispatch. No live-call permission is inferred. Completed receipts expose only selected-output added IDs, `primedExamples` and addition audit, never sibling examples or private text. Same request identity returns the same durable job; a reopened native engine independently treats identical examples as no change. This is applied priming context, not a prediction, retrospective review or optimizer-training receipt.

## Outputs awaiting criteria

A target may declare `pendingOutputs` as a distinct subset of its declared binding outputs. Such an output must not yet have a runnable classifier. Status reports `pending_criteria` with no invented question or prediction; decide and prime refuse it. Active bindings still share the original engine and request. These declarations do not grant cross-account access or create publication/section routing identities. Owner-authorized sharing and a publication's internal section router are separate integration responsibilities.

## Data-only frozen-run bootstrap

An authenticated writer can POST `/publication-runs/bootstrap` before target
configuration exists, with the usual account header, session and same-origin
CSRF protection. The strict payload is:

```json
{"contractVersion":"cyclotron-publication/v1","requestId":"<new UUIDv4>","name":"<run name>","config":{"cyclotron_id":"<existing definition>","cyclotron_definition_revision":1,"item_list_id":"<existing source list>"}}
```

The server requires the current canonical definition and classifier revisions,
then reuses native run normalization inside one catalog transaction. It freezes
item revisions and fingerprints, records the original authenticated person, and
checks fresh original-session authority again at the S3 commit. No client-authored
frozen classifier, provider setting, priming list or account override is accepted.
The response contains the run UUID and frozen configuration fingerprint, without
private classifier text. The run uses the existing `live` data schema required
by command storage; that value grants no dispatch authority. Bootstrap does not
change host `allow_live`, enqueue work, open the native engine or construct a
provider/optimizer. Its structural request ceilings are fixed at one and are not
an authorization to execute.

A request UUID is scoped to the account and bound to the original person and
exact input fingerprint. Identical retries return the same frozen run, including
after catalog advancement or an uncertain successful write. Changed input/person
is refused. CAS conflicts and unknown outcomes require retrying that same request
identity; no second run or automatic provider work is created. Operators must
still verify the intended production owner and configure the resulting reviewed
run/output/model bindings before native priming.

## Standing runs (continuous sources)

A target may declare `"standing": true`. This flag, its `requestCeiling` and optional `maxItems` exist only in the trusted `CYCLOTRON_PUBLICATION_TARGETS` configuration; no request field can set or change them. The run id stays fixed. The frozen run is created once with the existing bootstrap (or `freeze_run`) from an operational item list that must hold at least one item; nothing in the run configuration is rewritten afterwards, so queued-actor workspace bindings stay valid.

When the worker handles `decide` for a standing target it first resolves the provider credential, then registers the source and only then reserves a provider request:

* A never-before-seen `{id, values}` is appended to the run's own operational list (as revision 1) and to the run's item table in one console CAS transaction that is fenced by the command claim. Identical values return the existing revision without a write. The same id with different values creates a new immutable revision (revision + 1) and moves the run's item pointer to it. Earlier revisions stay readable by `itemRevision`.
* The id, size and shape are bounded: id 1-200 printable characters without edge whitespace; 1-32 value keys; encoded values at most 32 KiB; every classifier input field (default `text`) a non-empty string of at most 20,000 characters. The classifier set comes from the frozen run, never from the caller.
* An id that already exists in any other item list of the account (priming or evaluation lists) is refused with `source_refused`, so operational and priming items stay disjoint. Item count is capped by `maxItems` (default 500, refusal `standing_capacity_reached`). Console catalog size, not this cap, is the practical limit (see Capacity).
* The shared decision, cache and fingerprint pinning are the existing engine behavior. An identical re-request (same content, any request id) is served from the persisted shared decision and makes no second model call. Successful responses for standing targets add `"source": {"id", "revision", "fingerprint"}`; use `source.revision` as `itemRevision` for feedback.
* Registration happens before the request reservation, so a ceiling refusal can leave a registered but undecided source. Re-sending it later is idempotent.

### Request ceilings (what is enforced, and where)

Non-standing targets are unchanged: the run's frozen `max_requests` (bootstrap fixes 1) is the lifetime cap of the run's shared-batch ledger, and `CYCLOTRON_PREDICTION_MAX_REQUESTS` bounds a frozen run's configuration at provider construction.

Standing targets enforce a **cumulative lifetime cap on paid provider requests**, taken from `requestCeiling.total` (1-100000), and optionally an accruing daily allowance: `requestCeiling.perDay` with `requestCeiling.since` (UTC date). The cap in force on a UTC day is `min(total, perDay * days elapsed since `since`, counting that day)`; before `since` it is 0. Unused allowance carries forward; it is not a hard per-day cap. The cap is applied as the engine's `max_requests` for that command and checked atomically against the persisted attempts ledger (`shared-batches`, S3 CAS) at reservation. Cached results do not consume it. When reached, the job fails closed without a provider call and the receipt carries `code: request_ceiling_reached` (HTTP 429). Raising the cap is an operator configuration change; the host-level `CYCLOTRON_PREDICTION_MAX_REQUESTS` does not cap standing totals.

### Capacity

The console catalog and the shared-batch ledger are each a single S3 journal object limited by `max_records` and `max_bytes` (defaults 10000 records, 4 MiB). Every registered source, completed job (with its decision snapshot) and event consumes that budget, so a standing run supports on the order of hundreds of decisions before the store reports its record or byte budget and fails closed. Plan a successor run (new bootstrap and target `runId`) or raise the host limits before that.

## Machine access (service principals)

`POST /machine/publications/{publication}/operations` and `GET /machine/publications/{publication}/commands/{jobId}` exist alongside the unchanged human routes. They are meant for an API Gateway route with `AWS_IAM` authorization. The handler never trusts headers for identity: it reads `requestContext.authorizer.iam.userArn` from the gateway event, requires an assumed-role ARN, converts it to its role ARN and requires an exact match in `CYCLOTRON_MACHINE_PRINCIPALS` (a JSON list of at most 20 exact `arn:aws:iam::<12 digits>:role/<name>` values; wildcards, paths, users and patterns are rejected at startup). Without that context every call returns 403.

Account and run come only from the matched target: `X-Cyclotron-Account` must equal a configured target's account and the publication must be a `standing` binding of it, otherwise 403. Callers cannot name a run. A machine can only `decide`, `status` and `feedback` for that target. It cannot prime, bootstrap, open or change settings, grant, read other targets or use human routes.

Request body (all operations): `{"contractVersion":"cyclotron-publication/v1","operation":"decide|status|feedback","requestId":"<8-128 chars [A-Za-z0-9._:-]>","payload":{...}}`. `requestId` is required for `decide` and `feedback` and forbidden for `status`.

* `decide`: payload `{"bindingRevision","item":{"id","values"}}`. Returns 202 with the publication envelope plus `jobId` and `status`. The same `requestId` with the same content returns the same durable job; with different content 409 `request_conflict`. Another job already pending for the run returns 409 `run_busy`; retry later. Use a new `requestId` to retry after a terminal refusal. Live prediction disabled returns 503.
* Poll `GET .../commands/{jobId}`: 202 while pending or running; 200 with the native publication response when completed; refusals are 503 `provider_key_unavailable`, 429 `request_ceiling_reached`, 507 `standing_capacity_reached`, 409 `source_refused`, each with `code` and a non-secret `message`; any other terminal failure is 409 `reconciliation_required`.
* `status`: payload `{}`; returns the newest verified snapshot (200) or 503 before the first completed decision.
* `feedback`: payload `{"bindingRevision","itemId","itemRevision","output","label","comment"?,"editor":{"site","subject"}}`. Handled synchronously (no queue, provider or optimizer). It stores one catalog human label for that classifier revision and item revision, and a `publication-machine-feedback` run event holding the machine role, session name and asserted editor, in one transaction. Idempotent by `requestId`; reusing it with a different label or output is 409. `output` must belong to the binding (403), the item must have been submitted to this run (409) and the label must be one of the classifier's labels (409). Returns 200 `{status:"recorded", recordedAs:"human-label", asserted:{machine,editor}, ...}`.

**Authority claim.** A machine caller asserts, on behalf of its trusted Papyrus site service, that a named editor made this decision. Cyclotron verifies the caller's role but not the editor. The label is recorded like any human label, with the assertion stored as provenance, and is latest-wins per classifier revision and item revision like a console vote. It does not train or optimize anything by itself.

The queued authority is a `MachineActor` (kind `machine`) bound to the target's account and run and to the workspace binding. The worker re-checks, before opening state and before each side effect, that the principal is still configured, that the target is still a configured standing run and that the binding is unchanged. Revocation stops queued work.

### Which side holds which statement

HTTP APIs cannot carry a resource policy, so a principal in another AWS account cannot be authorized by this API alone. The caller therefore assumes a role in the Cyclotron account:

* Cyclotron account: `MachineInvokerRole` (trust: the single caller role ARN, parameter `MachineCallerRoleArn`; permission: `execute-api:Invoke` on `.../POST/machine/publications/*` and `.../GET/machine/publications/*` of this API only). `CYCLOTRON_MACHINE_PRINCIPALS` lists this invoker role ARN.
* Caller account: the caller role's identity policy allows `sts:AssumeRole` on that invoker role ARN only, then signs requests (service `execute-api`, region `us-east-1`) with the returned credentials and a fixed role session name that identifies the service.
* API Gateway: route `ANY /machine/{proxy+}` with `AWS_IAM`; the default proxy route is unchanged.

## Provider credential

The worker reads the provider key lazily at first provider use from AWS Secrets Manager secret `cyclotron/providers/openai` in the deployment account and region, as a plaintext string or `{"api_key": "..."}`. The value is cached for the warm container, handed to the Jev adapter as an explicit constructor argument (`JevAdapter.from_environment(api_key=...)`, no environment mutation, no dotenv), and never logged or placed in configuration. A missing, empty or unreadable secret fails the command before any state registration or request reservation with `provider_key_unavailable` (HTTP 503), and nothing is consumed. The worker role holds `secretsmanager:GetSecretValue` on only `arn:aws:secretsmanager:<region>:<account>:secret:cyclotron/providers/openai-??????`; the BFF role has no grant. Create the secret with the default Secrets Manager key (a customer-managed key would additionally need `kms:Decrypt` for the worker). Rotation takes effect on the next cold start.
