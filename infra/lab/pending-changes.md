# Pending lab stack changes

Reviewed, committed, not yet deployed. `scripts/dev/check_lab_template.sh` and
`tests/lab_template_test.py` prove that `template.yaml` equals
`deployed-template.snapshot.json` plus exactly the add and remove operations in
`pending-changes.json`, and nothing else. A remove names an existing mapping key
(no value, no list items); replace and overwrite remain refused. After a deploy is executed, refresh the
snapshot (`aws cloudformation get-template --template-stage Original`), empty
`operations`, and empty this list.

**Pending: list access on the books bucket for the BFF and worker roles.** One statement is appended to
each role's policy: `s3:ListBucket` on the books bucket itself. Without it, S3 answers a GET of a key nobody
wrote with AccessDenied rather than NoSuchKey, so storage cannot tell a new key from a forbidden one; the
constant-cost storage looks records up by key and needs "not found" to mean exactly that. No prefix condition:
S3 decides 404 versus 403 from list permission on the bucket, which a prefix-conditioned grant may not satisfy.
The bucket holds only `<account>/console/` data that both roles already read, so listing reveals key names
only within data they can read. No other grant changes. The two operations append to
`/Resources/BffRole/Properties/Policies/0/PolicyDocument/Statement/-` and
`/Resources/WorkerRole/Properties/Policies/0/PolicyDocument/Statement/-`.

Previously: the whole decision request is fitted and a provider-rejected request fails its job (lab code fd94173, package sha256 `ea8d752478d86840ab7d60d41faa42c45efbb8bff2f9bf79f4bc99adc66a64ca`), deployed on 2026-10-28 with change set `fit-request-fd94173`. Code only; the snapshot was refreshed.

Previously: decisions stop reading event history (lab code bf345f0, package sha256 `adb15079ad5cf43edc29611804c64882bdb246419b673b0901ce5dd14b2bc08c`), deployed on 2026-10-28 with change set `no-history-bf345f0`. Code only; the snapshot was refreshed.

Previously: **3008 MB for both functions**, deployed on 2026-10-28 with change set `memory-3008`; the snapshot was refreshed. `/Resources/BffFunction/Properties/MemorySize` and
`/Resources/WorkerFunction/Properties/MemorySize` are removed, then added back as 3008 in place of 1024.
Lambda allots CPU in proportion to memory. At 1024 MB, an uncached catalog read took a median of 6.2 s,
against 0.87 s for the same read on a laptop, and a decision job does many such reads.

Previously: the worker timeout at Lambda's maximum (900 s) was deployed on 2026-10-28 with change sets `timeout-6f1493b` and `validate-9d44290`; the snapshot was refreshed. Before that:  `/Resources/WorkerFunction/Properties/Timeout`
is removed, then added back as 900 (Lambda's own maximum) in place of 180. On 2026-10-28 at 03:51Z a
worker invocation that drained two jobs was cut off mid-job by the 180 s timeout. The job kept its
running claim, and 107 jobs waited behind it until an operator reconciled it. The command queue's
VisibilityTimeout (1080 s) already exceeds 900 s.

Previously: Every arbitrary limit was removed and story values became content-addressed records (lab code eb4065b, package sha256 `6710747c8c7ce81a146f13192de4b60b8091125c2733192b2619255d13150b0a`), deployed on 2026-10-28 with change set `no-limits-eb4065b`. That removed the `JournalMaxBytes` parameter and `CYCLOTRON_MAX_BYTES` on both functions. The snapshot was refreshed.

Previously: **Pending: remove the journal byte budget.** The storage layer no longer has a per-document byte budget
(nor record, commit, skip or collision counts), so the parameter and the variable that fed it go:

- `/Parameters/JournalMaxBytes`: the per-document S3 journal byte budget (default 4 MiB, 1 to 64 MiB).
  Nothing reads it any more.
- `/Resources/BffFunction/Properties/Environment/Variables/CYCLOTRON_MAX_BYTES`: the BFF's copy of that
  budget. `HostedConfig` no longer reads `CYCLOTRON_MAX_BYTES`; a leftover value is ignored.
- `/Resources/WorkerFunction/Properties/Environment/Variables/CYCLOTRON_MAX_BYTES`: the worker's copy, as
  for the BFF.

Deploy the code first or together: the old code reads `CYCLOTRON_MAX_BYTES` only when it is set and
otherwise uses its built-in 4 MiB default, so removing the variable under old code would lower a raised
budget back to 4 MiB. The new code has no budget either way.

Before that, **no pending changes.** The stage throttle at the account quota was deployed on 2026-10-28 with change set `stage-account-quota`; the snapshot was refreshed. Before that: Dropping `DefaultRouteSettings` (deployed 2026-10-28,
change set `no-stage-throttle-91290b3`, lab code 91290b3, package sha256
`c8600ab9551d03ead13f05878c3bb4cc5e36acc7060d22c913b6dbebb0887735`) did not clear the stage: an API
Gateway v2 stage keeps its last throttle (5/s, burst 10) when the property is removed. Adding
`/Resources/ConsoleApiApiGatewayDefaultStage/Properties/DefaultRouteSettings` back at the account's own
API Gateway quota (`ThrottlingRateLimit` 10000, `ThrottlingBurstLimit` 5000, from Service Quotas) leaves
no stage-specific ceiling. The snapshot was refreshed after the first deploy.

Previously: the worker sweep (lab code 00e04dd, package sha256 `8bec7bd6680bcdb7a4cdda369832ac105ef299fb6bf504a9709761727985ee32`) was deployed on 2026-10-27 with change set `worker-sweep-00e04dd`, adding `WorkerSweepSchedule` (once a minute) and `WorkerSweepPermission`. The worker no longer writes to its own queue, so Lambda recursive-loop detection has nothing to stop. The worker role's `sqs:SendMessage` grant is unused; removing it needs list-item removal in the checker. The snapshot was refreshed.

Previously: the run job queue (lab code e82eee2, package sha256 `5c837f0b1d934ec54c03df1f9167dafe40ae69b5d0b0de9e3e647dc78e2abda0`) was deployed on 2026-10-27 with change set `run-queue-e82eee2`, adding `sqs:SendMessage` on the command queue for the worker. The snapshot was refreshed.

Previously: the request caps were removed on 2026-10-27: change set `targets-remove-ceilings` (PublicationTargets without `requestCeiling`/`maxItems`), then `no-ceilings-377c478` (lab code 377c478, package sha256 `6ca1b278be02e4c938e12b31e5295a5427eac2711d40954f8d1c09f53583c9fe`) removing `PredictionMaxRequests` and `CYCLOTRON_PREDICTION_MAX_REQUESTS`. The snapshot was refreshed.

Previously: The Decision Ready doorbell (lab code 8908a33, package sha256 `90e3ed86159c6c21141abf6b0c0ef6e4217bb49aa781dc177d7638e385509c1f`) was deployed on 2026-10-27 with change set `decision-doorbell-8908a33`, with `DecisionEventBusArn` empty: no grant and no events until it is set to Papyrus's bus ARN. The snapshot was refreshed.

Previously: the machine invoker's /machine/decisions grant was deployed on 2026-10-26 with change set `machine-decisions-invoke`; the snapshot was refreshed.

Earlier: **No pending changes.** The record-lake cutover was deployed on 2026-10-22 with change set `record-lake-v0-34-0` (v0.34.0, package sha256 `63b054b87abc5050891ae14fe9c7b8a330d9d5d2a997de5fe5ab8edd78da3a9e`): `*/records/*` create-only in the bucket policy and Get/Put on `*/console/records/*` for the BFF and worker roles. All five journals were converted with scripts/dev/convert_to_record_lake.py (4,385 records; control state 18,955,946 -> about 1,199,000 bytes), and the snapshot was refreshed from the deployed stack.
