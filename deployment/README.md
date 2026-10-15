# Cyclotron Lab deployment package

Local preparation only; no cloud resources have been created. Read
[the activation review](../docs/console-activation-review.md) before provisioning.
The existing console uses normal Amplify static hosting and an HTTPS reverse
proxy to the SAM Python BFF. SQS invokes the separate worker. S3 is authoritative;
DynamoDB is not required for authority, and cloud SQLite is prohibited.

## Reproduce locally

From the repository root, using the configured Python environment:

```sh
npm --prefix trace-ui ci
npm --prefix trace-ui run build
node trace-ui/scripts/package-console.mjs
python scripts/render_console_template.py
cfn-lint deployment/template.json
python scripts/build_console_backend.py
```

The backend builder uses the hashed CPython 3.13 Linux x86_64 wheel lock, excludes
source tests and records archive/lock hashes in the ignored manifest. The zip is
about 29MB, under 60MB unpacked. Reuse staging only immediately after a matching
locked dependency installation. Build outputs and dependency staging are ignored.

Smoke-test the extracted actual zip in the official Lambda Python 3.13 image,
with no network, read-only code, no credentials, and SQLite/local persistence
forbidden. `scripts/smoke_lambda_artifact.py` tests signed-out Gateway denial and
a synthetic Gateway → S3 job → worker → event-page path, including exact human
rationale. This is not a deployed AWS or Google test.

## Activation sequence, pending approval

1. Review the consolidated resource/IAM/OAuth scope and resolve dedicated client
   ID, managed-login origin and authoritative DNS owner.
2. Use the normal SAM packaging/change-set flow with `deployment/template.json`,
   `cyclotron-production`, us-east-1 and account 682849160744. Review the change
   set before executing; no custom deployment role is required. Named runtime
   roles require the normal named-IAM acknowledgment.
3. Central owner separately registers the exact role pair and console client,
   preserving the directory. No directory role/client reuse is permitted.
4. Connect a new normal Amplify app to this existing console and use
   `amplify.yml`. Render same-origin rules with
   `python scripts/render_amplify_rules.py <reviewed-HTTPS-backend-origin>`.
   Keep auth/API/graphql/runs rules before the SPA fallback; apply no-store
   headers from `customHttp.yml`. Map the reviewed lab domain/certificate.
5. Run bounded actual-cloud failure tests and real Google/account/browser proof.
   Keep live paid/provider commands disabled pending their own release authority.

Retained buckets/key, encrypted historical versions and protected uncertain
claims require an operator retention/reconciliation plan. A failed queue delivery
never authorizes a new paid attempt. Logs must not expose proof headers/cookies;
Amplify managed callback-URL log retention remains an explicit review item.

## Verified local artifact checkpoint

Final zip SHA256:
`2a0c0cbe7776469c929efdb91f99d95c8c45c9f48daab4a8547553bad1bd3225`.
Wheel-lock SHA256:
`d8135c26d4783a33998add2b9d1a029407a402624e0fe15637155f627934df51`.
Archive: 28,593,415 bytes; unpacked: 58,934,554 bytes; 3,808 files.
Both network-disabled official Lambda Python 3.13 container checks passed using
image digest `sha256:d5d5c74e3639a01fac254c9334a60604aa50327aa122b3ec26151e055a06a6a0`.
The synthetic browser proof is `browser-viewer-proof.jpg`; real Google/AWS proof
remains pending activation. Generated archives are retained locally and ignored.
