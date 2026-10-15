# Cyclotron Lab activation review — 2026-10-15

Status: proposed resources and permissions, not an activation approval. No AWS
writes, OAuth edits, DNS changes or public push were made. The existing console
builds and its signed-out screen was browser-verified at the loopback preview.
A local preview is not proof of hosted Google sign-in or deployment.

## Verified current environment

Read-only STS with `cyclotron-production` confirmed account `682849160744` and
Endymion SSO AdministratorAccess, us-east-1. Its Amplify list contains only
`prospectus-web-cms` (`dlfqqzcwv70bo`), not a Cyclotron console application. Its
us-east-1 Cognito pool list is empty. Its Route53 list exposes only `ospect.us`
zone `Z073711521FYKG7C4KTZ9`. These observations do not locate the DNS owner of
`lab.cyclotron.anth.us`; other accounts/regions were not exhaustively searched.
Do not modify that existing Amplify application or Papyrus marketing deployment.

Accounts owner verified the central API in account `712236451410`, us-east-1:
`https://bnh9mctvo9.execute-api.us-east-1.amazonaws.com/api`, SigV4 `execute-api`.
Current registry/resource policy grants only `prospectus-directory`. Cyclotron
has no grant. The directory role/client cannot be reused. Central code pin
`088de30d79185188294b412049b99ffa6ada7587` is the owner's supplied reference.

## Recommended activation: separate BFF and worker roles

Recommend the concrete separate-role deployment in `deployment/template.json`.
Both Lambda-only execution roles use temporary runtime credentials, without
AssumeRole brokers, administration or central storage access:

- `arn:aws:iam::682849160744:role/cyclotron-console-bff-runtime`
- `arn:aws:iam::682849160744:role/cyclotron-console-worker-runtime`

Central owner implemented the explicit one-product enrollment/execution pair at
`e2bb8ffb2f4288989bab6153d0394afecf8439a9`; documentation follow-up
`6eafcf6291b6f0f59fafa05cec0fe10720b32aa0`. This is source-only, not live.
Original BFF provenance remains intact. The worker is restricted to `/me` and
account authorization, with the original session family/auth_time and current
issuer/client/enrollment policy. There is no transitive delegation, directory
access or worker enrollment. Proposed pair review revision is
`cyclotron-execution-pair-v1-20261015`.

Our actual central native harness plus Python signed transport passes 15 checks,
including restart continuity, worker principal preservation, forbidden routes,
unknown role, wrong client/family, expiry, removed pair and logout. Central owner
also reports 58 new delegation tests. These do not verify deployed IAM.

Activation permits matching existing BFF-enrolled sessions under the same
policy. A fresh-login-only rollout requires an enrollment-policy revision bump
and reenrollment; do not silently promise that effect. Keep existing directory
registration and policies intact when adding the reviewed Cyclotron pair.
No shared-role permission union is proposed.

## Exact central invoke scope

Resource prefix: `arn:aws:execute-api:us-east-1:712236451410:bnh9mctvo9/api`.
Only `execute-api:Invoke` on the following method/path suffixes is requested.
UUID wildcards are necessary at IAM level; BFF and central handlers validate
lowercase UUIDv4 account IDs and independently enforce Person/membership/product.

| BFF | Worker, under the reviewed central role relation |
| --- | --- |
| `/POST/v1/identities/google/challenge` | none |
| `/POST/v1/identities/google/resolve` | none |
| `/GET/v1/identities/me` | `/GET/v1/identities/me` |
| `/GET/v1/accounts` | none |
| `/POST/v1/accounts` | none |
| `/GET/v1/accounts/*` | none |
| `/POST/v1/accounts/*/authorize` | `/POST/v1/accounts/*/authorize` |

The GET wildcard also matches deeper GET paths in IAM; central routing must
continue refusing unsupported paths. No legacy `/identities/resolve`, members
mutation, identity-linking, accountDirectory or paid entitlement is requested.
Central registry, API resource policy and caller IAM all need coordinated owner
rollout. Product `cyclotron`, capabilities `console:read` and `console:write` only.
Worker forwards the original access token, never browser Person/email headers.
Every guard performs `/me`, exact queued actor comparison, then authorization
for the pinned account/operation and fresh run binding. It cannot reenroll or
substitute a new login family after token expiry. The result is not a lease.

## Proposed storage and queue scopes

Names are proposals, not discovered resources. Account/region for these resources
is `682849160744/us-east-1`. Resource creation needs approval in addition to
runtime grants. No new DynamoDB table/grant is needed to establish authority;
rebuildable card-catalog indexing can be added separately.

- Versioned regional general-purpose S3 bucket
  `cyclotron-console-books-682849160744-us-east-1`. Canonical first-workspace
  scope is `<validated-account-UUID>/console/`; account comes from freshly
  authorized server context. Existing `StorageScope` and fixed expected bucket
  owner are mandatory. Runtime `s3:GetObject`/`s3:PutObject` only on
  `arn:aws:s3:::cyclotron-console-books-682849160744-us-east-1/*/console/payloads/*`
  and the named coordination prefixes below. Payload PUT is create-only; anchor
  PUT create-only; head PUT conditional create/update. No runtime ListBucket,
  delete, version deletion, policy/lifecycle/versioning administration or recovery
  head rewind. S3 policy must enforce appropriate conditional writes and TLS.
- BFF console coordination:
  `*/console/coordination/console/*` only. Worker console plus separately named
  `workspace`, `runtime`, `shared-batches`, `evaluation`, `reviewer` and
  `optimizer` coordination domains. Each maps to the existing named repository
  composition; no SQL or fallback path. Current content-addressed payload prefix
  is shared by domains, so both principals can write immutable bytes there;
  only permitted heads can publish their own authority. The per-workspace console
  head also contains catalog and job records: IAM cannot isolate individual
  fields inside that head. Tenant-level IAM is product service-wide; fresh human
  authorization and trusted scope construction provide account isolation.
- Separate versioned auth bucket
  `cyclotron-console-auth-682849160744-us-east-1`. Fixed scope
  `console-auth/sessions/`. BFF GetObject/PutObject on its `payloads/*` and
  `coordination/oauth/*`; worker GetObject only on payloads and `coordination/oauth/session-*/*`, excluding pending/quota heads. No deletes or
  list. Pending logins, sessions and quota use bounded canonical S3 journals,
  not local files or DynamoDB authority. S3 versions retain ciphertext after
  logout; current active/revision is authoritative. Retention/deletion policy
  requires explicit operational review; no automatic GC is enabled.
- KMS key logical resource `SessionEncryptionKey`, alias
  `alias/cyclotron-console-sessions`; runtime policy uses the exact created key
  ARN from the reviewed stack output, not an alias wildcard. BFF
  `kms:GenerateDataKey` and `kms:Decrypt`; worker `kms:Decrypt` only.
  Encryption context must include `application=cyclotron-console`,
  `purpose=oauth-session`, `kind=pending|session`, `record=<64 lowercase hex>`. Worker decrypt is further limited to `kind=session`.
  No runtime key-policy/admin/encrypt-with-arbitrary-context grants. AES-256-GCM
  authenticates the same context and exact KMS KeyId is checked. Key ARN remains
  unresolved until the reviewed stack is synthesized/provisioned.
- Queue `cyclotron-console-commands` and DLQ
  `cyclotron-console-commands-dlq`; BFF `sqs:SendMessage` only on command queue;
  worker `ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility`,
  `GetQueueAttributes` on command queue only. Message contains validated
  account/run/job references, never JWTs or actor authority. Worker rereads
  canonical job/actor and claims inside its own invocation. An SQS retry is not
  permission to repeat a paid request. DLQ redrive is operator reconciliation,
  not a reset. Queue consumer/runtime composition is implemented and tested locally.
- Log groups `/aws/lambda/cyclotron-console-bff` and
  `/aws/lambda/cyclotron-console-worker`, with CreateLogStream/PutLogEvents only
  on respective groups. Infrastructure creates groups and reviewed retention.
  Application and Gateway access logging are disabled in this template; Lambda error output omits proof bodies. Amplify managed access-log treatment of callback query strings still needs operational review: no universal redaction claim is made. PKCE and single-use callback state remain required.

Whole-domain immutable rewrites and scoped head contention remain material:
previous synthetic one-classifier initialize/decide/review/close used 90 GET and
75 PUT, 121697 PUT bytes, largest transaction 13971 bytes. This is not an AWS
latency/cost benchmark. Limits stop work; no history truncation or budget reset.
No large-store/live concurrency claim is justified yet. Crashed or uncertain
owners can require operator reconciliation; no timeout takeover is provided.

## OAuth and hosting inputs

Dedicated console public OAuth client in existing central pool
`us-east-1_hlSyajw9S` (issuer
`https://cognito-idp.us-east-1.amazonaws.com/us-east-1_hlSyajw9S`). Do not reuse
client `3fonen2dsmr4o0uif5lan71ajo` or CRM credentials/callbacks. Client ID and
managed-login domain remain owner-supplied outputs. No client secret. Exact
callback `https://lab.cyclotron.anth.us/auth/callback`; logout
`https://lab.cyclotron.anth.us/`; authorization code, PKCE S256, scopes
`openid email profile`, Google only. Proposed review ID
`cyclotron-google-only-unlinked-v1-20261015` must be backed by an actual policy
review before registration. Existing Google client/project/secrets are reused
only by their owner; no values are copied or exposed. Callback allowlist changes
may require user handoff. No email-only automatic linking.

`amplify.yml` builds/packages the existing React console into
`trace-ui/console-dist`. Standard SAM packaging supplies the Python backend;
normal Amplify hosting uses explicit HTTPS reverse-proxy rules from
`scripts/render_amplify_rules.py` before the SPA rewrite. This does not require a
bespoke deployment role. No backend target or rewrite has been activated.

Hosted transport explicitly uses authenticated HTTP event polling: bounded
100-event/1MiB pages, about two seconds between idle reads, fresh authorization
each page, abort on account change and private-data teardown on 401/403. Native
WebSockets remain supported. Hosted WebSocket streaming is not claimed.

## Completed local composition and limits

`HostedRuntime` composes encrypted S3 OAuth sessions, scoped S3 catalogs and
frozen priming, BFF enqueue, SQS worker invocation, native WorkspaceSession,
paid-attempt ledgers and guarded event writes. Original actor authorization is
rechecked after ownership and before provider dispatch/business side effects.
Unknown claims remain protected and require reconciliation; no TTL takeover or
automatic paid retry is provided. SQS uses batch 1, two workers, three failed
receives before DLQ. Sessions retain the original access token, maximum one hour;
there is no automatic refresh or worker reenrollment. Expiry can interrupt work
and leave a protected claim for operator review.

The first environment composition disables live run/provider commands, with no
environment toggle granting paid execution. Tests inject synthetic authority and
providers. Comparison/provider resume paths are not a hosted paid-work release.
No provider credentials or real charges are requested in this activation.

Whole-workspace snapshots remain bounded (default 4MiB, 10000 records, one million
commits); each idle event poll rereads the scoped journal. This is a bounded
preproduction implementation, not a large-catalog latency/cost claim. The
DynamoDB card catalog remains optional and rebuildable; it is not authoritative.
S3 authority is settled. Local SQLite remains supported; hosted composition
forbids it.

## Consolidated approval scope and rollout gates

Review `deployment/template.json` for the exact persistent changes: two retained
versioned private S3 buckets, retained session KMS key and alias, command queue
and DLQ, two narrowly scoped Lambda execution roles, two Lambda functions,
14-day log groups and an HTTP API. Runtime storage policies enforce TLS and
conditional publication; neither role can delete/list/administer buckets.
Normal SAM artifact upload uses an approved deployment artifact bucket; no
bespoke deployment-role engineering is requested.

Approval must cover the dedicated Google-only console public client in the
existing central pool, exact callback/logout/scopes above, federation review
`cyclotron-google-only-unlinked-v1-20261015`, and the explicit BFF/worker pair
review `cyclotron-execution-pair-v1-20261015`. Central owner then adds one
`cyclotron` registration with only console read/write, preserves directory,
updates the API exact-principal policy and rolls out tested pair code. Caller IAM
is the exact method/path table above. Client ID and managed-login origin remain
unresolved owner outputs; no directory client is reused.

Normal new Amplify app/repository connection, generated backend proxy rules and
`lab.cyclotron.anth.us` DNS/certificate mapping are included in hosting review.
The authoritative `anth.us` DNS account/zone still needs locating. Existing
marketing/CRM apps are excluded. Review managed access-log callback retention,
ciphertext retention and operator reconciliation before activation.

After approval, require stack/change-set review, bounded actual-S3 conditional
write/uncertain-receipt trials, real Google sign-in, create/select account,
allowed/denied tenant, viewer, account switching and logged-out browser proof.
Only then report the public URL as running. Current verified preview is local
and explicitly synthetic at `http://127.0.0.1:8916/`.
