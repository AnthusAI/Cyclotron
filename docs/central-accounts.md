# Central accounts integration boundary

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
creation requires a retained UUID idempotency key. Merely signing in or listing
accounts never creates a central person or account.

Access tokens remain in bounded server memory. The browser receives only an
opaque HttpOnly, Secure, SameSite=Lax `__Host-` session cookie, plus a CSRF token
in the same-origin session response. Sessions expire at the access-token expiry
or one hour, whichever is sooner. Refresh tokens are not stored. Logout revokes
the server session; a restart signs everyone out. This store is deliberately
single-process. Multiple processes require a reviewed shared session store.
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

The current console requires a Python process, persistent SQLite state, and a
serialized background worker. It cannot place authoritative databases in an
ephemeral hosting directory. A persistent single-host deployment can preserve
these semantics with one Uvicorn process and one writer/worker per workspace;
live work should initially be disabled, worker creation bounded, and backups
made using SQLite's backup API. Multiple hosts or independent writers require a
separate storage and job-coordination design.

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
