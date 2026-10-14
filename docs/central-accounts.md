# Central accounts integration boundary

The existing console remains `trace-ui/src/WebApp.tsx`, served by
`decision_flywheel.web_api`. The optional accounts adapter adds a hosted access
boundary without changing the offline exporter or local workspace command.

Install `.[accounts]` in a hosted Python runtime. Construct `AccountsClient` with
`SigV4Transport(endpoint, region)`, then inject `AccountWorkspaceAccess` into
`create_app(..., account_access=access)`. This mode refuses shared token login,
unauthenticated LAN authority, and a global worker. A failed central check never
falls back to the local store supplied to `create_app`.

The host supplies two callbacks:

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
subscription result receives another central read check; changing the selected
account requires reconnecting. Timeline requests also reauthorize and use the
account's store. Central authorization remains responsible for intersecting
membership roles with active product entitlements. The adapter additionally
rejects a write granted to a viewer, or any response whose person, account or
product differs from the request.

The server forwards the original verified access token in
`X-Prospectus-Human-Token`. SigV4 separately identifies the runtime to Prospectus.
The SDK credential chain is refreshed on each request and static credentials
without a temporary session token are refused. Requests use a fixed HTTPS origin,
do not follow redirects, and expose neither upstream response bodies nor tokens
in errors. No central person, identity or membership database is stored here.

The client implements account listing, explicit identity resolution, idempotent
account creation, and read/write authorization. Unknown identity listings may
return `person: null`. Creation receives the caller's UUID idempotency key and
preserves it across retries. Central service tests, rather than this adapter,
must prove transactional creation and identity linking.

## Deployment and remaining work

This increment does not implement an OAuth session resolver, callback, browser
account switcher, workspace provisioning, or a hosted entry point. Supplying a
test session resolver does not constitute production authentication. The normal
local CLI deliberately does not enable account mode. Never deploy the synthetic
preview harness used for local verification.

The current console requires a Python process, persistent SQLite state, and a
serialized background worker. It cannot place authoritative databases in an
ephemeral hosting directory. A persistent single-host deployment can preserve
these semantics with one Uvicorn process and one writer/worker per workspace;
live work should initially be disabled, worker creation bounded, and backups
made using SQLite's backup API. Multiple hosts or independent writers require a
separate storage and job-coordination design.

Before exposing the console, review the runtime host and temporary IAM identity,
separate OAuth client/callback, same-origin secure session cookies and CSRF
protection, account switching, worker lifecycle/quotas, backup restore, and DNS/TLS.
Deployment identifiers and customer credentials belong in the private deployment
configuration, not this public library.

Synthetic specifications live beside the adapter in
`src/decision_flywheel/prospectus_accounts_test.py`. They exercise the actual
FastAPI, GraphQL and SQLite boundary with fake central transport and sessions;
they do not prove Google authentication or a live central deployment.
