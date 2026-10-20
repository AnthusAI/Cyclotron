# Login-only BFF candidate — release approval pending

The deployed BFF archive SHA256 is
`cebd0f12742cd0d2c81bd76ea3581305a39d9241db5220038602c50649442090`.
All 161 packaged application files match source commit
`d59eb5c3cc3ec4d55ae1c4972b060cd1ebc28e7d` byte-for-byte. Its deployed code
hash is `zr0PEnQs0NLIG9duo1gTBaOdkkHbUiADhgLFBklEIJA=`.

The source lives on `codex/prospectus-console`, whose reviewed comparison pin is
`1c05e294bb2a5f614ad3957deefd9ae46eeffc20`. No associated GitHub PR was found.
That later source matches 158 packaged files; its three differences are only
`vendor/trace-ui/viewer.css`, `vendor/trace-ui/web.css` and
`vendor/trace-ui/web.js`, changed during later frontend releases. Preserve the
deployed BFF copies for this candidate; do not rebuild its UI or copy all of
current `main` into the deployment.

The previously unexplained session/telemetry behavior has source provenance:

* `8e9b3a54526f13366d40fb989ed46d2bc8a8c4a9`: bounded account calls,
  fixed-route timing telemetry, Lambda request correlation, less serial latency.
* `8c76fd9464cde569f4d1a49ef874e18e4a708969`: fresh listing/selection authority
  and account identity-change handling without replacing per-execution checks.
* `246338fb8e862a9641fc0b3a39c6a079046e1bf0`: safe session-phase telemetry.
* `d083add664389b23470cdc3e6ff05e8d337faa7a`: safe workspace timing.
* `d59eb5c3cc3ec4d55ae1c4972b060cd1ebc28e7d`: remove duplicate HTTP context
  preflight without caching membership grants; retain execution authorization.

This branch adds only signed-challenge transport to that deployed source line.
Runtime changes are limited to `account_routes.py`, `account_sessions.py`,
`google_identity.py`, and `s3_sessions.py`. Opaque central evidence stays in the
existing encrypted, single-use pending login. Central MAC, expiry, nonce,
binding, and replay verification remain authoritative. Legacy stored-ID
challenges still work; malformed signed challenges cannot downgrade silently.

176 focused regression tests pass, including the deployed session/telemetry,
listing/selection, original-session queued authority, revocation, account scope,
and new memory/S3 signed-challenge cases. Ten new cases failed before the fix and
pass afterwards. The cached official Lambda Python 3.13 image is unavailable on
this host; an actual-artifact Python 3.13 smoke check remains a release gate.
No image pull or dependency installation was performed.

An additional existing-runner CI job builds only public repository source with
the same hashed Linux CPython 3.13 wheel lock. It restores the three public vendor
assets from `d59eb5c3`, then compares all 3,808 file payloads against a reviewed
aggregate hash (`1bddd15ad0972044a0196856be25e94c2086afb044e9c66ccbfffa2d0f0f5d38`).
This allows equivalent candidate testing without uploading the private deployed
archive. The verified source-built zip runs the existing packaged Gateway/S3/
worker smoke plus a synthetic signed Google callback in the documented official
Lambda Python 3.13 image. The container has no network, writable code, or cloud
credentials. Nothing is deployed or uploaded; the candidate and rollback zip stay
local. The CI result must pass before treating this runtime gate as satisfied.

## Exact artifact and release scope

Prepare an offline overlay of the hash-pinned deployed archive, replacing only
the four verified application files above. Preserve every other archive member,
including dependency bytes and the deployed vendor assets. Verify unchanged
inventory, exactly four changed payloads, source hashes, and the final archive
hash. Label it `login-only-20261020-<source commit>`; retain the existing package
version and pinned dependency lock rather than issuing a new library release.
The preparation manifest supplies the final content hash and rollback archive.

After explicit release approval, use the existing `cyclotron-console` SAM /
CloudFormation BFF-only artifact change-set path documented in
`console-cloud-activation.md`. Review a change set affecting only BFF Code;
worker, frontend, resources, role/policy, environment, OAuth/security settings,
central endpoint, and live execution configuration are unchanged. This candidate
uses the existing deployed host configuration and keeps execution disabled.
Do not use the broader prediction PR as its deployment payload.

Immediately before release, confirm the existing function still has the pinned
base code hash. A changed baseline requires a new comparison and candidate;
never overwrite concurrent owner work. Retain the exact original archive and
existing template/artifact pointer for rollback through the same BFF-only path.
After release, verify the candidate code hash and perform one fresh normal Google
sign-in with the already approved identity. Read actual account/role; do not add
grants or infer membership. Failed verification rolls back or stays blocked.

Nothing has been uploaded, merged, deployed, or enabled. Approval is pending.
Prediction activation and the 18 pending human decisions are separate later work.
