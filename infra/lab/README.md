# Cyclotron lab stack (`cyclotron-console`)

Source for the CloudFormation stack `cyclotron-console` (us-east-1, the
`cyclotron-production` AWS profile) that serves the central Cyclotron lab at
`https://lab.cyclotron.anth.us`. It was deployed with SAM (the Lambda tag
`lambda:createdBy=SAM`) but its template lived in no repo. This directory
reconstructs it from the deployed template.

Files:

- `template.yaml`: the stack template.
- `deployed-template.snapshot.json`: `aws cloudformation get-template --template-stage Original`, captured read-only. Treat as the reference.
- `parameters.example.json`: placeholder parameters, no live values.

`scripts/dev/check_lab_template.sh` proves `template.yaml` equals the snapshot
(ignoring formatting and key order); `--live` also compares against the deployed
stack (read-only `get-template`). `tests/test_lab_template.py` runs the offline
comparison in `make test`.

Note: the deployed Original and Processed templates are identical and carry no
`Transform`. The SAM transform was expanded before deploy (the API is a plain
`AWS::ApiGatewayV2::Api` with an OpenAPI `Body`, with SAM-style logical ids such as
`ConsoleApiApiGatewayDefaultStage`). `template.yaml` is therefore plain
CloudFormation that `sam deploy` accepts as is; it is not a compact
`AWS::Serverless::*` source. Re-expressing it that way would be a separate,
reviewed change.

## Resources

| Logical id | Type | What it is |
|---|---|---|
| `BffFunction` | Lambda | `cyclotron-console-bff`, python3.13 x86_64, 1024 MB, 28 s, handler `decision_flywheel.cloud_entry.bff_handler`. Serves every HTTP API route. |
| `WorkerFunction` | Lambda | `cyclotron-console-worker`, 1024 MB, 180 s, handler `decision_flywheel.cloud_entry.worker_handler`. Consumes the command queue (`WorkerFunctionCommands`: batch 1, max concurrency 2, partial-batch failure reporting). |
| `BffLogGroup`, `WorkerLogGroup` | Logs | `/aws/lambda/cyclotron-console-{bff,worker}`, 14-day retention. |
| `ConsoleApi` + `ConsoleApiApiGatewayDefaultStage` | API Gateway v2 (HTTP) | Routes (all to the BFF, payload format 2.0): `ANY /`, `ANY /{proxy+}` (no auth at the gateway; the BFF does its own OAuth/session auth) and `ANY /machine/{proxy+}` with `AWS_IAM` (SigV4). `$default` stage, auto-deploy, throttle 5 rps / burst 10. |
| `BffFunctionRootPermission`, `...ProxyPermission`, `...MachinePermission` | Lambda permissions | Let API Gateway invoke the BFF for `/`, `/*`, `/machine/*`. |
| `BooksBucket` | S3 | `cyclotron-console-books-<account>-<region>`: console payloads and coordination objects. Versioned, SSE-S3, public access blocked, `Retain`. |
| `AuthBucket` | S3 | `cyclotron-console-auth-<account>-<region>`: OAuth session payloads. Same hardening. |
| `BooksBucketPolicy`, `AuthBucketPolicy` | S3 policies | Deny non-TLS; deny `PutObject` on `*/payloads/*` and `*/coordination/*/*/anchor.json` unless `If-None-Match: *` (write-once); deny `head.json` writes with neither `If-Match` nor `If-None-Match` (compare-and-swap only). |
| `SessionEncryptionKey`, `SessionEncryptionAlias` | KMS | `alias/cyclotron-console-sessions`, rotation on, `Retain`. Key policy delegates to the account root; runtime access comes from role policies scoped by encryption context. |
| `CommandQueue`, `CommandDLQ` | SQS | `cyclotron-console-commands` (visibility 1080 s, 4-day retention, redrive to the DLQ after 3 receives) and `cyclotron-console-commands-dlq` (14-day retention). Both SSE-SQS. |
| `BffRole` | IAM | `cyclotron-console-bff-runtime`. See below. |
| `WorkerRole` | IAM | `cyclotron-console-worker-runtime`. See below. |
| `MachineInvokerRole` | IAM, conditional | `cyclotron-machine-invoker`, only if `MachineCallerRoleArn` is non-empty. Assumable by that one role ARN (session max 1 h); may `execute-api:Invoke` only `POST /machine/publications/*/operations` and `GET /machine/publications/*/commands/*` on this API. |

Secrets Manager: not a resource of this stack. `WorkerRole` may read
`secretsmanager:GetSecretValue` on `cyclotron/providers/openai-??????` (the
provider key secret, created outside the stack; it is not in this repo, and
nothing here reads or stores its value).

Stack outputs: `BackendOrigin`, `BffRoleArn`, `WorkerRoleArn`, `SessionKeyArn`,
`BooksBucketName`, `AuthBucketName`, `CommandQueueUrl`, `MachineInvokerRoleArn`
(conditional).

### IAM

BFF role: S3 get/put on books `*/console/payloads/*` and
`*/console/coordination/console/*`; auth bucket get/put on
`console-auth/sessions/payloads/*` and `.../coordination/oauth/*`; KMS
`Decrypt`/`GenerateDataKey` on the session key only for encryption context
`application=cyclotron-console`, `purpose=oauth-session`, `kind` in
`pending|session` and a 64-character `record`; `sqs:SendMessage` to the command
queue; `execute-api:Invoke` on a fixed list of routes of the central accounts API;
log stream writes for its own log group.

Worker role: broader S3 on books coordination prefixes (`workspace`, `runtime`,
`shared-batches`, `evaluation`, `reviewer`, `optimizer`); read-only on session
objects; `kms:Decrypt` for `kind=session` only; two central API routes; SQS
receive/delete/change-visibility/get-attributes; its own logs;
`GetSecretValue` on the OpenAI secret.

### Hard-coded values (not parameters)

These are literal in the deployed template and are kept literal so the
template matches. Changing any is a reviewed change:

- Public origin `https://lab.cyclotron.anth.us`.
- Central accounts API `https://bnh9mctvo9.execute-api.us-east-1.amazonaws.com/api`, which lives in a different AWS account (also in both roles' `execute-api:Invoke` ARNs).
- Lambda code location: S3 bucket `cdk-hnb659fds-assets-<account>-us-east-1` (the CDK bootstrap assets bucket), key `cyclotron-console/backend/<hash>` (no `.zip` suffix). Both functions point at the same object.

## Parameters

| Name | Meaning |
|---|---|
| `ConsoleIssuer` | OIDC issuer (Cognito user pool) URL; default is the central pool. |
| `ConsoleClientId` | Dedicated console OAuth client id. Must not be the directory/CRM client. |
| `ConsoleAuthorizationOrigin` | Existing central managed-login origin. |
| `FederationReviewId` | Review id recorded as the `FederationReview` Lambda tag; only the one reviewed value is allowed. |
| `ExecutionPairReviewId` | Same, for the `ExecutionPairReview` tag. |
| `MachineCallerRoleArn` | Exact IAM role ARN allowed to assume `cyclotron-machine-invoker`. Empty creates no machine role and no `CYCLOTRON_MACHINE_PRINCIPALS`. |
| `PublicationTargets` | Reviewed `CYCLOTRON_PUBLICATION_TARGETS` JSON (up to 3000 chars): which accounts, runs, model identity, request ceilings and publication bindings the hosted lab may run. Empty leaves the variable unset. |
| `AllowLive` | `CYCLOTRON_ALLOW_LIVE`. `true` permits live provider calls; default `false`. The stack description still says "live providers disabled". |
| `PredictionModel` | `CYCLOTRON_PREDICTION_MODEL`, default `jev-1.13.0`. |
| `PredictionMaxRequests` | `CYCLOTRON_PREDICTION_MAX_REQUESTS`, 1 to 3 digits, default 100. |

No parameter is secret; the provider key lives in Secrets Manager. Deployed values
include ids and ARNs (pool id, client id, a caller role ARN in another account),
which are not committed here. Use `parameters.example.json` as a shape and take
live values from `aws cloudformation describe-stacks` (read-only) when preparing a
change.

## How the Lambda package is built

Build with `scripts/dev/build_lab_package.sh [COMMIT] [OUTPUT_DIR]` (default `HEAD`,
`build/lab`). It makes no AWS calls and prints the zip path and sha256. Do not
commit the zip.

What it does:

1. `pip install --no-deps --only-binary=:all: --no-compile --python-version 3.13 --implementation cp --platform manylinux_2_34_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 --target stage -r infra/lab/requirements.lock`. The lock is the exact 42-distribution closure of the deployed package (see below). Lambda python3.13 is Amazon Linux 2023 (glibc 2.34), so the 2_34 wheels are preferred, as in the deployed package (cryptography ships a `manylinux_2_34` wheel).
2. Copies `src/decision_flywheel` from COMMIT with `git archive` (no `dist-info`, no `*_test.py`, no `conftest.py`, no `__pycache__`) and the repo `LICENSE` as `LICENSE.decision-flywheel`, as the deployed zip has them.
3. Zips deterministically (sorted entries, fixed 2026-01-01 timestamps, mode 0644, deflate level 6). Two builds of the same commit and lock give the same sha256.

### The deployed package (verified read-only, 2026-10-21)

`cyclotron-console/backend/b2a3bb38c2454b9adeac76a8e08a6bc9` (the key is the sha256
prefix of the zip; the zip's sha256 base64 equals the functions' `CodeSha256`).
3,979 entries, 60.2 MB uncompressed, no `.pyc`, no layers, no `.env`, credentials or
private keys (the only `.pem` files are the `certifi` and `botocore` CA bundles).
`decision_flywheel` is **copied source, not pip-installed**: `__version__ = "0.30.0"`
and it matches commit `002e6f2` (`0.30.0`, the head of `main`), except as listed
under differences. `boto3` is not included (the Lambda runtime provides it; the code
uses `botocore`), nor are `uvicorn` or `python-dotenv`.

Directly requested distributions (the ones carrying a `REQUESTED` marker):
`botocore`, `fastapi`, `httpcore2`, `httpx`, `httpx2`, `jsonschema`, `mangum`,
`openpyxl`, `pyjwt`, `strawberry-graphql`, `tenacity`, `truststore`, `typesafe-sdk`.
All wheels are cp313 or pure Python; the compiled ones are `cffi`, `cryptography`
(`cp311-abi3-manylinux_2_34`), `pydantic-core` and `rpds-py`.

### Known differences between the build and the deployed package

Built from `002e6f2` with `infra/lab/requirements.lock`: same top-level set, same
version for every `dist-info`, every file byte-identical except:

- `decision_flywheel/vendor/trace-ui/{web.js,web.css,viewer.css}`: the deployed bundles are a different (minifier-renamed, slightly larger) build of `trace-ui` that matches no commit. The build ships the committed bundles from COMMIT.
- `decision_flywheel/schemas/cyclotron-held-out-report.v1.{schema,example}.json` are in the commit and the build, but not in the deployed zip.
- The build marks all 42 distributions `REQUESTED` (pip `-r`), the deployed zip only the 13 above; metadata only, so 29 `RECORD` files differ by one row (and the 13 requested ones match).
- 28 files in the deployed zip have mode 0600; the build uses 0644.
- Zip bytes differ (28,938,384 versus 28,939,467 bytes), so `CodeSha256` will differ.

A deploy of the build changes behaviour only through those vendor bundles and the two
additional schema files. The package has not been run on Lambda from this build.

## Deploy runbook

**Every deploy needs Ryan's explicit approval, and every deploy goes through a
change-set review before execution. No one deploys from a branch without that
approval. Agents do not run these commands.**

1. Make the change in `template.yaml` and update `deployed-template.snapshot.json` only after a deploy has been executed and re-captured. Run `scripts/dev/check_lab_template.sh` (offline).
2. Build the package (above) from the reviewed commit and record its sha256 on the ticket. With Ryan's approval, upload it to the CDK assets bucket (`cdk-hnb659fds-assets-<account>-us-east-1`) under a new key (for example `cyclotron-console/backend/<sha256>`); never overwrite the live key. Edit `S3Key` in both `BffFunction` and `WorkerFunction` together (they must point at the same object; `LabCodeKey` is not parameterized because `check_lab_template.sh` compares the template to the deployed snapshot literally). Then run the offline check, which will show the intended `S3Key` difference to review, and update the snapshot only after the deploy.
3. Prepare parameters from a private copy of `parameters.example.json` (never committed).
4. Create the change set without executing it:

   ```bash
   AWS_PROFILE=cyclotron-production sam deploy \
     --template-file infra/lab/template.yaml --stack-name cyclotron-console \
     --region us-east-1 --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides file://private-parameters.json \
     --no-execute-changeset --no-confirm-changeset
   ```

   (Check `sam deploy --help` for the parameter-file form your SAM CLI supports.)
5. Review the change set (`aws cloudformation describe-change-set`). Reject anything that replaces or deletes a bucket, the KMS key, the API, a role or a queue; list and attach the review to the ticket. Buckets and the key are `Retain`.
6. Get Ryan's explicit approval of that change set, then execute it. Afterwards re-run `scripts/dev/check_lab_template.sh --live`, probe the unauthenticated API (expect 401/403), and refresh the snapshot in a follow-up commit.
