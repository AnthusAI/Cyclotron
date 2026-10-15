# Central accounts integration boundary

## Optional cross-app Google identity experiment

The local-only cross-app identity mode is explicitly selected by injecting a
`GoogleIdentityClient` as `HostedAccounts(..., google_identity=identity)`. It uses
the central owner's separate identity factory, not an activated account Lambda.
It does not change standalone/offline use. No production client, registry policy,
runtime role or OAuth configuration was created for this experiment.

The agreed server-to-server contract is:

1. Before the browser redirect, the SigV4 BFF calls
   `POST /v1/identities/google/challenge {}` without a human token. The response
   `{challengeId, nonce, expiresAt}` has a maximum 300-second lifetime and is
   centrally bound to the registered product role, Cognito issuer, exact client
   and reviewed federation policy revision. The BFF validates its shape and
   expiry and binds its nonce/ID to the independent one-use browser state and PKCE
   verifier. Neither the challenge ID nor the PKCE verifier is accepted from the
   browser callback.
2. The callback obtains both JWTs from the same code exchange and independently
   verifies their signatures, issuer, token use and client. In this mode it also
   requires exact ID audience, matching subject/authentication time/nonempty
   `origin_jti`, authentication no older than 300 seconds, and the expected ID
   nonce. Distinct JWT signing keys and JWT IDs are allowed. Existing optional
   `at_hash` verification remains in place.
3. It calls `POST /v1/identities/google/resolve {challengeId}` with the access JWT
   in `X-Prospectus-Human-Token` and ID JWT in
   `X-Prospectus-Identity-Token`. Both proof headers and the request body are
   SigV4 signed using temporary runtime credentials, sent only to the fixed
   configured central HTTPS endpoint, without redirects. The identity header is
   forbidden on every other route. Resolve returns `{person:{id,createdAt}}`.
4. Only after successful central resolution does the BFF create an opaque local
   session bound to that Person. The ID JWT is discarded after the callback;
   only the access JWT remains in bounded server memory. Tokens and CSRF values
   are omitted from session representations. No JWT goes to browser storage,
   response bodies, cookies, local files or application logs. A callback/state
   replay fails before another code exchange or central resolution.
5. Each later authenticated request calls `GET /v1/identities/me` with access
   evidence only. It returns `{person}` or `{person:null}` and never enrolls.
   Missing/changed Person or denied continuation revokes the local session;
   central unavailability fails closed. Central continuation requires the
   enrolled authentication time and hashed `origin_jti` family plus the current
   role/client/policy provenance. A new login family requires a fresh challenge.
   The console does not implement refresh-token storage or automatic refresh.

Central owns Google subject extraction and canonical association. The BFF never
uses email, username prefixes or a Cognito `identities` array to infer identity.
The current automatic central policy requires a reviewed built-in Google-only
configuration and exactly one unlinked Google identity; ambiguous profiles fail
closed. This is a trust contract for the Cognito federation configuration, not
direct verification of a Google-signed token. Proof-pair retries under the same
challenge are centrally idempotent until expiry; changed proof or conflicting
identity is refused. The BFF starts a new login after an unsuccessful callback
rather than retaining a failed proof pair for browser-driven retries.

The old accounts API and this identity factory are **not assumed unified**.
Before exposing account state, and before account-create side effects, the BFF
requires the accounts response Person to equal the enrolled canonical Person.
Mismatch (including a missing old-account Person) fails closed. A synthetic
accounts fixture can demonstrate account continuity, but does not establish that
the existing account storage can consume these canonical Persons. Memberships
and entitlements remain central; there is no implicit product or staff grant.

This code supplies nonce/state/PKCE and enforces proof freshness regardless of
provider UI behavior. Reauthentication behavior still needs live client review;
AWS documents that the `prompt` parameter depends on the managed-login branding
version, so no unverified prompt setting is treated as a freshness guarantee.
See [AWS authorization endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html).

### Local cross-language verification

Run `PYTHONPATH=src python scripts/verify_google_identity_contract.py PATH_TO_PROSPECTUS`
with the optional accounts dependencies and the Prospectus checkout's locked
Node/tsx installation. The script invokes its real
`accounts/testing/google-harness.ts` factory through JSON lines with public RSA
JWKS. It opens no HTTP listener and makes no cloud/provider calls. Two instances
of this BFF model the two product registrations; it does not modify Papyrus.

Verified on 2026-10-14: distinct synthetic pool subjects and independent access/ID
signing keys resolved to one central Person. A deliberately synthetic accounts
adapter showed that Person reopening the same account and scoped GraphQL store.
Wrong issuer/client, wrong token family, wrong nonce and ambiguous linked
profiles failed. Reusing local callback state was refused before another central
resolution; identical central proof retry succeeded and changed proof returned
409. The handler's gateway caller context is synthetic: this verifies no deployed
IAM enforcement, live Google sign-in, account-storage unification or persistence
durability. An independently issued replacement access JWT in the enrolled
family continued successfully; changing the family revoked the BFF session
without another enrollment call. Focused Python account/session/web regressions
passed 95 tests.

Add `--filesystem` to that command to use the central harness's
`filesystemRoot` mode and its already-built native Virtuus
`FileStore`/`JournaledStore` bridge. This creates and removes a disposable identity
directory. The test restarts the central process with the same directory and
public keys while keeping both BFF sessions open, then checks Person and session
continuity, selected-account/scoped GraphQL access, and durable exact-retry versus
changed-proof behavior. This filesystem mode and the original memory mode both
passed locally. No runtime console code change was needed.

Only **central identity enrollment** uses the durable filesystem in this proof.
Without `--accounts-handler`, the account/membership adapter is an explicit
Python in-memory fixture outside the restarted process, and the workspace is
temporary SQLite. It does not prove account-storage durability,
multi-process console sessions, a live S3 backend, or deployed Google/IAM
configuration. Native journal interruption and cross-process enrollment safety
are separate central-repository tests; this script establishes the BFF contract
across a central restart after successful publication.

Add `--filesystem --accounts-handler` to select the central harness's
`accountsFixture:true` option. This sends all BFF account calls through the real
`accountHandler` → `AccountsService` → `googlePersonDirectory`, backed by the same
durable identity repository. Only account/membership persistence is substituted
with the central `MemoryAccountStore`; no Python account-service fake handles
those requests. No old `/v1/identities/resolve` endpoint is used.

This mode passed: both pools resolved the same canonical Person; each could
create, list, select and authorize its own product's account and open a scoped
temporary GraphQL workspace. Repeating identical account creation returned the
same account; changing its input under the same request key was refused. The
second product could not select the first product's account, despite sharing its
Person and membership, because it had no entitlement for that product. No
automatic cross-product entitlement was introduced to make the test pass.

Restarting central preserved identity/session-family and proof receipts but
deliberately lost `MemoryAccountStore`: both BFF account lists became empty,
selected accounts cleared, and old account selection returned 403 even though
the local temporary workspace still existed. This verifies actual account-handler
and shared-PersonDirectory compatibility in the optional local factory. It does
not establish durable accounts/memberships or activate the existing deployed
account Lambda. The original two modes also remain available for comparison.

### Durable accounts through the actual BFF

The latest local proof was run against central checkpoint
`bf655d5e4ac6c3775fdecbc9aa07bb79af9072f5`. Reproduce with:

```bash
PYTHONPATH=src python scripts/verify_google_identity_contract.py \
  PATH_TO_PROSPECTUS --filesystem --durable-accounts
```

This mode configures `filesystemRoot` and `durableAccounts:true`, **omitting**
`accountsFixture`. It uses the actual Python OAuth/accounts BFF, signed transport,
central `canonicalAccountsHandler`, shared PersonDirectory, and native pinned
Virtuus FileStore/JournaledStore for both identities and account aggregates. It
creates and removes a disposable root. `--accounts-handler` remains a separate
memory-account control and cannot be combined with `--durable-accounts`.

Observed end-to-end behavior:

- Two registered synthetic pools resolve the same Google subject to one Person.
  A second Google subject with the same email resolves to a different Person.
- One Person owns two Cyclotron accounts. The second Person has viewer access to
  one and member access to the other. Both memberships are listed under the
  appropriate Person; the owner retains both accounts.
- Viewer reads succeed and direct GraphQL writes fail without changing the
  workspace. Member writes succeed only in the selected account's store. A
  nonmember and a random account selection are refused. Shared Person/membership
  still does not grant Papyrus access to a Cyclotron entitlement.
- Restarting central with the same root/public keys preserves the full account
  summaries (names, IDs, roles and entitlement capabilities/status), both product
  selections and the member selection; scoped GraphQL reads remain authorized.
  The restored viewer remains read-only when selected; a direct write is denied.
  The existing Python BFF processes and their opaque sessions remain open during
  this restart, so this is not a claim of BFF-session persistence.
- Revocation in one account leaves the other account's membership usable. A later
  revocation of that remaining membership clears the member's selected account
  and denies a direct GraphQL write. Another central restart retains both
  revocations; knowing the surviving local workspace/account ID restores nothing.
- Repeating account creation with its original key/input after restart returns
  the same account. Changed input under that key is denied. Repeating an identical
  membership setting preserves its account revision instead of duplicating audit.
- The existing nonce/issuer/client/profile, callback replay, central proof retry,
  changed-proof replay and same-family/new-family checks remain in this run.

The durable mode passed, and all three older control modes were rerun. This
closes the local Python-BFF-to-durable-account verification gap. No console runtime
or local-first CLI behavior changed. It does **not** prove actual S3 durability,
deployed IAM enforcement, live Google, browser-based durable onboarding, a
production endpoint or safe concurrent/ephemeral console workers. The only
workspace data is temporary SQLite; no real model calls or customer data are used.

Central's deliberate MVP bounds remain material: bounded account scans (256
documents), 100 retained memberships/account, 128 audit entries and 64 KiB/account
document; capacity fails closed. Audit rollover, index/scan sizing, a strict
cross-account quota, retention/read-bound work, and operator reconciliation of
pre-publication uncertain claims remain central design/production work. This BFF
test does not replace the central owner's interruption, race or limit tests.

## Existing account boundary

The existing console remains `trace-ui/src/WebApp.tsx`, served by
`decision_flywheel.web_api`. The optional accounts adapter adds a hosted access
boundary without changing the offline exporter or local workspace command.

Install `.[accounts]` in a hosted Python runtime. Construct `AccountsClient` with
`SigV4Transport(endpoint, region)`. The fixed HTTPS endpoint may include its API
Gateway stage. `HostedAccounts` supplies the verified OAuth session resolver and
an `AccountWorkspaceAccess`; inject that access into
`create_app(..., account_access=hosted.access)`, then call `hosted.install(app)`.
This mode refuses shared token login,
unauthenticated LAN authority, and a global worker. A failed central check never
falls back to the local store supplied to `create_app`.

`HostedAccounts` provides the session callback; the host supplies the workspace
callback:

- `resolve_session(connection)` returns `VerifiedAccountSession` only after
  validating the server-side OAuth session. It checks the access JWT's issuer,
  signature, subject, client ID, token use and expiry. It obtains the person ID
  from central identity resolution; email never links identities automatically.
  An absent or expired session returns `None`. A selected account is an untrusted
  preference; it does not confer authority.
- `workspace_for_account(account_id)` returns an `AccountWorkspace` containing
  only that account's store and worker. It maps a validated central ID to an
  operator-provisioned workspace. It must never interpret a browser value as a
  filesystem path. The host owns worker startup/shutdown and resource limits.

Every GraphQL query and subscription needs `console:read`; every mutation needs
`console:write`. This includes operations over WebSocket transport. Each streamed
subscription result receives another central read check. Each workspace binds its
HTTP, WebSocket and timeline URLs to an explicit account query parameter. That
parameter is untrusted and centrally reauthorized; the server never substitutes
the latest session preference for a request's account. A delayed Alpha callback
cannot write to Beta after another tab changes the selection. Timeline requests
also reauthorize and use the
account's store. Central authorization remains responsible for intersecting
membership roles with active product entitlements. The adapter additionally
rejects a write granted to a viewer, mismatched person/account/product/operation
bindings, and an invalid authorization revision.

The server forwards the original verified access token in
`X-Prospectus-Human-Token`. SigV4 separately identifies the runtime to Prospectus.
The SDK credential chain is refreshed on each request and static credentials
without a temporary session token are refused. Requests use a fixed HTTPS endpoint,
do not follow redirects, and expose neither upstream response bodies nor tokens
in errors. No central person, identity or membership database is stored here.

The client implements account listing, explicit identity resolution, idempotent
account creation, and read/write authorization. Unknown identity listings may
return `person: null`. Creation receives the caller's UUID idempotency key and
preserves it across retries. Central service tests, rather than this adapter,
must prove transactional creation and identity linking. Creation unwraps exactly
the central `{account: ...}` response; it does not accept a second response shape.

## OAuth and account UI

`account_sessions.py` validates RS256 signatures from the pinned Cognito issuer's
JWKS, the dedicated app client, token use, subject, issue time and expiry. The
authorization code flow uses S256 PKCE, a browser-bound single-use state value,
and an ID-token nonce; access and ID token subjects must agree. Email claims never
associate identities. The callback is the configured public origin followed by
`/auth/callback`; neither Host headers nor browser return URLs choose it.

`account_routes.py` implements `/auth/login`, `/auth/callback`, `/auth/logout`,
`/api/session`, `/api/accounts`, and `/api/accounts/select`. Mutations require an
exact configured Origin and a session CSRF token. The OAuth callback accepts the
cross-site provider redirect only with its browser-bound state proof. Account
creation requires a retained UUID idempotency key. In the original pool-bound
mode, signing in or listing accounts never creates a central person or account.
The optional Google mode explicitly resolves/enrolls a canonical Person at
sign-in; it still creates no account or membership merely by signing in.

By default, access tokens remain in bounded server memory. A cloud host must inject
`S3OAuthSessions` with the reviewed KMS cipher and scoped journals; it encrypts
access/CSRF and pending PKCE proof before canonical S3 publication. Blocking storage
work runs off the HTTP event loop, and route mutations explicitly save the session.
The browser receives only an
opaque HttpOnly, Secure, SameSite=Lax `__Host-` session cookie, plus a CSRF token
in the same-origin session response. Sessions expire at the access-token expiry
or one hour, whichever is sooner. Refresh tokens are not stored. Logout revokes
the server session; a restart signs everyone out. This store is deliberately
single-process. `S3OAuthSessions` supplies the cross-process store but is not yet
provisioned or wired into a production entry point. Sessions expire with the
original access token; workers cannot reenroll or substitute another login.
HTTP loopback cookies are available only through an explicit test configuration.

`trace-ui/src/AccountConsole.tsx` wraps the existing workspace with sign-in,
account selection and creation, role/permission status, retry, and sign-out.
`WorkspaceTransport.tsx` binds callbacks to the mounted account; switching
unmounts the old workspace and clears its run URL. Expired HTTP authentication
also unmounts private data and returns the shell to sign-in. Its hook is used throughout
the existing console catalog and editor components, preserving the unscoped
local application when no provider is present. Server checks remain authoritative
even if browser controls are bypassed.

The selector and account types come from the shared
`@anthus/prospectus-accounts-client` package, pinned as a checksum-verified local
archive under `trace-ui/vendor/`. Only its `/react` entry and erased types enter
the UI; its IAM server entry is never imported. The small `consoleSession.ts`
client handles only the product's same-origin BFF/session routes.

## Deployment and remaining work

The OAuth/session and account UI code is implemented and tested locally with RSA
fixtures. A real dedicated Cognito client, central account API activation and
runtime-role registry entry, workspace/worker provisioning, and the reviewed
hosted entry point remain pending. The normal local CLI deliberately does not
enable account mode. Never deploy the synthetic preview harness used for local
verification or use fixture keys as a production trust anchor.

Cloud deployment must use the canonical S3 adapters and ephemeral worker
composition described in [s3-cloud-storage.md](s3-cloud-storage.md). DynamoDB is
a rebuildable index. Hosted SQLite, an always-on SQLite server and a persistent
single-host workaround are excluded. Local SQLite remains the native default.
The earlier single-host proposal is superseded by the user's explicit cloud
storage requirement.

Before exposing the console, review the runtime host and temporary IAM identity,
separate OAuth client/callback, worker lifecycle/quotas, backup restore, and DNS/TLS.
Keep live work disabled initially. Configure the Python host and proxy to exclude
OAuth callback query strings and tokens from access logs; the existing local
server already disables Uvicorn access logs.
Deployment identifiers and customer credentials belong in the private deployment
configuration, not this public library.

Synthetic specifications live beside `account_sessions.py`, `account_routes.py`,
and `prospectus_accounts.py`. They exercise real RSA verification and the actual
FastAPI, GraphQL and SQLite boundary with a mock provider and central transport.
They do not prove Google authentication or a live central deployment.
