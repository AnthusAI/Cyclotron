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
  predictionFingerprint, requestId, label, explanation, reasonCode}`. A label and
  nonblank explanation are explicit human input. The server derives reviewer
  identity from the verified actor, checks the displayed prediction pin and
  binding, and namespaces request identities by account/publication/actor. It
  delegates idempotency/corrections/learning to the SDK. Changing the contents of
  a retried request conflicts rather than overwrites an acknowledgement.
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
remain release dependencies. Existing `decision_log` carries current item values;
immutable historical source revisions require the owning catalog/history contract
and should use explicit source-revision identities until reconciled.

Output-scoped steering is also a dependency: the current SDK's global review-rate
override must not be exposed as though it changed only one publication. Papyrus's
new panel accepts an optional host steering callback, which must stay absent
until backed by an authorized output-scoped command. The shared controls are
reused, not a new steering engine. This slice does not claim complete deployment
mode parity or production authorization integration.

## Verified evidence

`publication_lab_test.py` executes deterministic offline `classify_many` models
through the real SDK and both direct and in-process HTTP/ASGI adapters. It covers
independent common-item outputs; explicit label/reason/explanation; cross-account
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
