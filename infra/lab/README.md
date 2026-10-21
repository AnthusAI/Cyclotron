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

Not recorded anywhere in this repo (no build script, `samconfig`, Dockerfile or CI
job references it), and the live package could not be inspected with the read-only
permissions used here. Known facts: one 28,939,467-byte zip serves both functions
(same `CodeSha256`), runtime python3.13 on x86_64, no layers, uploaded to the CDK
assets bucket under `cyclotron-console/backend/<hash>` and last modified
2026-10-21. `decision_flywheel.cloud_entry` imports the hosted modules lazily and
needs the `accounts-cloud` extra (`accounts`, `web`, `priming`, `mangum`), plus
`typesafe-sdk` (`jev` extra) for the live Jev provider, which is consistent with a
size of about 29 MB.

The likely recipe, which is an inference and not yet verified byte-for-byte:

```bash
rm -rf build/lab && mkdir -p build/lab
python3.13 -m pip install --platform manylinux2014_x86_64 --only-binary=:all: \
  --python-version 3.13 --target build/lab ".[accounts-cloud,jev]"
(cd build/lab && zip -qr ../lab.zip .)
```

Before relying on it, build it, check `unzip -l` for
`decision_flywheel/cloud_entry.py`, and compare size and import behaviour with the
deployed package. Record the verified recipe here (follow-up).

## Deploy runbook

**Every deploy needs Ryan's explicit approval, and every deploy goes through a
change-set review before execution. No one deploys from a branch without that
approval. Agents do not run these commands.**

1. Make the change in `template.yaml` and update `deployed-template.snapshot.json` only after a deploy has been executed and re-captured. Run `scripts/dev/check_lab_template.sh` (offline).
2. Build the package (above) and obtain approval to upload it to the assets bucket under a new key. Update `S3Key` for both functions together.
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
