# Hosted publication prediction composition

This source change connects the existing publication SDK to the existing hosted
console BFF, durable command queue, original human session authorization, worker,
and native S3 Cyclotron stores. It creates no deployment resources or grants.
Live execution remains disabled by default. Review, priming, training, and
publishing are not activated by these routes.

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
before the first completion it returns 503. Other operations return 503.

## Explicit activation configuration

The existing required `CYCLOTRON_*` host configuration still applies. These
additional ordinary application settings are parsed without creating resources:

* `CYCLOTRON_ALLOW_LIVE`: exactly `true` or `false`, default `false`.
* `CYCLOTRON_PUBLICATION_TARGETS`: JSON list of
  `{"accountId":"<existing UUID>","runId":"<existing UUID>","bindings":[{"publicationId":"<name>","outputs":["<classifier>"],"revision":"<reviewed revision>"}]}`.
* `CYCLOTRON_PREDICTION_MODEL`: explicit reviewed Jev model identity.
* `CYCLOTRON_PREDICTION_MAX_REQUESTS`: integer from 1 through 100. Frozen run
  provider/model/budget must match this reviewed ceiling; priming is rejected and
  the optimizer is disabled. Existing provider credentials are resolved lazily
  inside the authorized worker, not at BFF construction.

No actual account, publication target, model, or budget is selected in this PR.
Nothing here changes infrastructure, deploys a Lambda, installs credentials,
grants account access, or submits real feedback.

## Remaining integration gates

The currently deployed lab artifact lacks these routes and has live providers
disabled. Its existing artifact publisher and a separately authorized deployment
must be confirmed. The observed fresh Google login reached central identity
resolution, which returned 401 about 34 seconds after a successful challenge.
Deployed central source enables signed challenges, returns `challengeToken`, and
does not persist those challenge IDs. The old Cyclotron client dropped that token
and sent only the unpersisted ID. This PR fixes that protocol mismatch by retaining
the opaque token in the existing encrypted single-use pending record and forwarding
exactly the signed token on resolution. The central service still verifies its MAC,
expiry, nonce, binding and replay rules. Legacy stored-ID challenges remain supported;
malformed signed tokens fail closed instead of silently falling back to an ID.

The fix is tested in memory and with the existing S3 session composition, but is
not deployed. Account/role verification therefore remains blocked. A fresh normal
login after an authorized release is the next verification, not another retry of
the unchanged deployment. The deployed artifact also contains session/telemetry
changes absent from current `main`; its owner must reconcile those changes before
building a release so the source update preserves existing security behavior.

This composition uses the lab's existing cookie/CSRF authorization. Papyrus's
server-to-server Prospectus credential resolver and product registrations must be
verified against the deployed authority before connecting its clients. This PR
does not accept arbitrary bearer tokens or invent grants to bridge that gap.
Publication canonical corpora and integrated section-fit/selector bindings must
also be verified; Anth.us and Pilobolus currently have no verified live corpus.

The four-publication tests use explicitly synthetic sources, accounts, labels,
and a fake provider. They are evidence for composition, queue behavior, native
shared decisions, and access rejection, not evidence of live relevance quality.
Human labels remain held out until a real baseline and verified feedback path
exist. The subsequent publication-integrated section fit and selector stage
remains Papyrus's responsibility.
