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
