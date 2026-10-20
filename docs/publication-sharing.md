# Owner-authorized publication inference

The central publication router can serve a different SaaS account only through
an explicit owner grant. A consumer does not become a member or administrator
of the owner's account. The existing same-account publication API remains
unchanged.

This change supplies a transport-independent grant lifecycle and guarded
inference boundary, with synthetic specifications. It does not install a
production grant authority, expose a hosted endpoint, configure credentials,
or activate sharing. Hosted integration is tracked separately and must preserve
the existing queue, account verification and native storage boundaries.

## Two routing stages

```mermaid
flowchart LR
    S[Discovery revision] --> C[Shared central publication Cyclotron]
    C --> G[Owner-granted publication output]
    G --> P[Consumer publication]
    P --> I[Consumer-owned integrated Cyclotron]
    I --> R[Publication section]
```

The first engine selects publications. The second engine selects sections
inside one publication. The second engine is intentionally separate; a second
central publication engine is unnecessary. A central result does not supply a
section label, authorize publication, or create training feedback.

SaaS account identity, authenticated actor identity, cloud hosting account,
publication identity and any product-specific perspective identity are separate
fields. They must be verified independently. No account name, cloud account
number, local pod key or domain is a fallback for a missing SaaS account.

## Grant scope

An owner-approved grant pins:

- Owner and consumer SaaS accounts and consumer publication.
- Explicit permitted operations: delegated inference, addressed result delivery,
  or both. Delivery-only access never authorizes a paid inference request.
- The existing central engine, its binding revision and the full set of
  outputs executed by that engine.
- The narrower output set that may be returned to this consumer.
- Grant identity and immutable revision, expiry, request allowance and billing
  attribution.

Execution and disclosure are different scopes. The current engine computes all
configured classifiers in a decision; filtering the result afterward does not
mean only one classifier ran. An owner must explicitly approve that full
execution binding. A definition or binding change invalidates the grant until
reviewed and replaced.

The initial billing contract is owner-pays. It records owner, consumer and
billing identities without charging the consumer or inferring a price. A
selected-output usage display that normalizes shares to 100% is not billing
authority. Consumer-pays, quota pricing and settlement require a separately
accepted billing agreement and implementation.

The request allowance limits service reservations. It is not a dollar budget
or a maximum provider-call count. Existing engine/provider budget enforcement
must remain in place.

## Authority and privacy boundaries

Only fresh, host-verified identities and owner authorization may create or
revoke grants. Request bodies cannot confer either authority. Consumers can
perform granted inference and retrieve only receipts bound to their own
account, actor, publication and request.

The owner callback must verify authority to manage sharing grants for this exact
engine and scope. Ordinary `console:write` is not sufficient evidence of that
authority.

Identity callbacks must use the current authenticated request context. A
shared mutable "current user" is not a valid identity adapter. Consumer-use
authorization must check the selected publication and operation, in addition
to verifying account membership.

The service returns an explicit output allowlist, rather than a full
`PublicationLab` envelope. It does not expose questions, rubrics, definitions,
training labels, feedback, sources from other requests, context examples,
history, replay, events, storage locations or provider diagnostics. It grants
no `console:write`, review, steering, training, account administration or
delegation power. Administrative audit access is a separate host concern.

Admission, dispatch and result disclosure require current identity, grant and
binding checks. Revocation, expiry, membership loss, revision drift or an
authority outage fails closed. Removing a grant cannot undo a provider call
already in flight; its result must remain undisclosed after revocation.

The trusted adapter must call the execution guard at every actual provider
dispatch and preserve the native engine's execution lease. A check made only
when accepting an HTTP request is insufficient.

Delegated inference additionally requires a trusted source validator before
any source identity or quota is reserved. It checks current source permission,
the exact canonical catalog/item/revision and supplied values, and returns
authoritative source provenance. Caller-supplied values cannot establish a
canonical content hash or pin another source's identity. Source permission and
provenance are checked again at execution and disclosure boundaries.

## Durable adapter requirements

The injected store is an authority boundary, not a best-effort cache. A hosted
implementation must provide atomic grant creation/revocation and immutable
audit records, reserve a request and its quota exactly once, and bind that
reservation to the complete identity, grant revision and source revision.

Audit or reservation failure must prevent execution. Dispatch/settlement with
an uncertain outcome retains the reservation for reconciliation; a retry must
not silently authorize another paid call. A completed receipt can be replayed
only after fresh authorization. It must never be used as an account-wide cache.
Billing attribution and request audit contain identifiers and outcomes, not
source text, policy bodies, credentials or free-form provider errors.

Durable storage, fresh identity callbacks, owner authority and inference
callbacks are mandatory host dependencies. The test store demonstrates these
requirements with synthetic tenants; it is not a production storage adapter.

## Publication handoff

For central fan-out, the service can deliver a permitted output from an
existing central decision. It must not recompute that discovery merely to
cross the account boundary. The trusted receipt resolver verifies an exact
delivery addressed to the consumer and publication; possession of a guessed
owner receipt ID never grants access to arbitrary owner sources or history.
This path performs no provider work and preserves the original central
decision and source identity, which may originate in the owner's account.

After a permitted match, the consumer's internal router receives tenant-aware
provenance: owner/consumer accounts, publication, grant revision, central
engine/output/binding, central prediction receipt and source identity/revision.
Source identity retains the native account, list, item, positive integer
revision and content hash. Identical item IDs in different tenants or source
lists must not alias. An originating publication may be explicitly null for a
shared discovery pool; the destination publication remains mandatory.

The receiver verifies the receipt through an authenticated service boundary.
A JSON flag such as `verified: true` cannot establish trust. The internal
router uses the consumer's own policies, section taxonomy, labels and storage.
Its feedback does not automatically become central training data. Changes to
either learning scope need distinct authority.

## Integration and activation gates

The additive module is independent of central priming. Integrating it into
hosted runtime/queue files requires coordination with their current owner and
tests for durable concurrency, process crashes, revocation during paid work,
storage outages and receipt ownership. Passing synthetic tests establishes
contract behavior, not production authorization.

Before activation, verify existing site resources and actual SaaS identities;
obtain real editorial criteria and section taxonomy; then review the exact
owner grant, output and execution bindings, budget/billing attribution,
retention and destination. Any persistent credentials, security expansion,
DNS or infrastructure change requires approval of that exact target and
scope. Unknown criteria and undecided labels stay unset.

Kanbus: `decision-flywheel-e5dcca5a-6438-41ef-b7a9-4f8e8dd56016`
(feature), `decision-flywheel-ea811ff3-8dba-4d50-92c9-9d81e8c1e510`
(contract), `decision-flywheel-e523bb3e-1b19-41dc-97d9-ae367ca36174`
(hosted integration).
