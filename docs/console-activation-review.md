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

## Caller-role recommendation and concrete alternatives

**Recommend deliberate separate BFF and worker caller roles before activation.**
The current central contract does not support this: duplicate product rows are
rejected, and enrollment/session continuation bind to the exact principal.
Changing only the registry uniqueness rule would leave workers unable to use
BFF-enrolled sessions and would weaken a deliberate identity boundary if the
principal comparison were simply removed.

Proposed normal Lambda execution roles (not deployment/assumption broker roles):

- `arn:aws:iam::682849160744:role/cyclotron-console-bff-runtime`
- `arn:aws:iam::682849160744:role/cyclotron-console-worker-runtime`

Both trust only service `lambda.amazonaws.com`, receive temporary credentials
from Lambda's runtime, and receive no `sts:AssumeRole`, IAM administration, central
storage access or directory entitlement. Functions would be named
`cyclotron-console-bff` and `cyclotron-console-worker`. Normal Amplify backend/CDK
resource provisioning creates roles; no bespoke deployment-role arrangement.

Required central extension, owned by Accounts: one product registration with an
explicit reviewed enrolling-principal/executing-principal relation. Preserve the
original enrolling principal on alias/session records. Worker continuation must
check that exact enrolled principal, original session family/auth_time, product,
issuer, client and reviewed policy revision against the specifically approved
relation. Distinguish enrollment permission from execution permission; worker
cannot enroll or create an account. Account permissions remain fresh and scoped.
Do not replace the check with an unrestricted same-product role set. Policy
changes require explicit handling of existing enrollments, not silent migration.

Required central tests before this alternative can be enabled: BFF-enroll/worker
execute positive flow; unknown principal, wrong product, unlisted role pair,
worker challenge/resolve, wrong issuer/client/policy, different origin_jti or
auth_time, changed Person, expired proof and revoked membership all fail. Retain
directory isolation and existing same-principal regressions. Local guard tests
here do not stand in for those central cross-role cryptographic/policy tests.

**Fallback only with explicit approval:** both functions directly use one
`arn:aws:iam::682849160744:role/cyclotron-console-runtime`. This matches the
existing central principal binding. Both functions then receive the UNION of
permissions below: worker can generate session envelope keys, mutate/revoke
sessions, enroll identities and create accounts; BFF can consume queue messages
and write runtime/budget coordination. IAM no longer contains those function
boundaries. Human checks, conditional storage and separate publication grants
remain required, but cannot restore an IAM separation after code compromise.
There is no implicit approval for this union. No role option has been activated.

## Exact central invoke scope

Resource prefix: `arn:aws:execute-api:us-east-1:712236451410:bnh9mctvo9/api`.
Only `execute-api:Invoke` on the following method/path suffixes is requested.
UUID wildcards are necessary at IAM level; BFF and central handlers validate
lowercase UUIDv4 account IDs and independently enforce Person/membership/product.

| BFF | Worker, after the reviewed central role relation exists |
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
  `embedded`, `runtime`, `shared-batches`, `evaluation`, `reviewer` and
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
  `coordination/oauth/*`; worker GetObject only on those same paths. No deletes or
  list. Pending logins, sessions and quota use bounded canonical S3 journals,
  not local files or DynamoDB authority. S3 versions retain ciphertext after
  logout; current active/revision is authoritative. Retention/deletion policy
  requires explicit operational review; no automatic GC is enabled.
- KMS key logical resource `SessionEncryptionKey`, alias
  `alias/cyclotron-console-sessions`; runtime policy uses the exact created key
  ARN from the reviewed stack output, not an alias wildcard. BFF
  `kms:GenerateDataKey` and `kms:Decrypt`; worker `kms:Decrypt` only.
  Encryption context must include `application=cyclotron-console`,
  `purpose=oauth-session`, `kind=pending|session`, `record=<64 lowercase hex>`.
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
  not a reset. Queue consumer/runtime composition is still release work.
- Log groups `/aws/lambda/cyclotron-console-bff` and
  `/aws/lambda/cyclotron-console-worker`, with CreateLogStream/PutLogEvents only
  on respective groups. Infrastructure creates groups and reviewed retention.
  Access logs must omit OAuth callback query strings, cookies and token headers.

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

`amplify.yml` now builds/packages the EXISTING React web console into
`trace-ui/console-dist`. It performs no backend deployment or permission mutation.
The Python BFF remains same-origin via specific `/auth/*`, `/api/*`, `/graphql`
and `/runs/*` routes to a reviewed ephemeral backend, before any SPA rewrite.
No live backend target/rewrite is configured yet. Static hosting alone is not
functional sign-in. Existing GraphQL subscriptions use WebSockets: static
Amplify plus a plain HTTP Lambda gateway does not establish that transport.
A reviewed streaming bridge or an explicit authenticated polling UI adjustment
is still required; no silent polling/transport downgrade was implemented here.

Hosted worker dispatch, frozen-run/priming composition, scoped repair procedure,
full stack synthesis, bounded actual-cloud conditional-write/failure trial and
Google sign-in/account-switch/browser proof remain activation prerequisites.
Local SQLite stays supported. Cloud SQLite/EC2 server is not a fallback.
