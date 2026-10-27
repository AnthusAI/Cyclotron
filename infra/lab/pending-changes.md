# Pending lab stack changes

Reviewed, committed, not yet deployed. `scripts/dev/check_lab_template.sh` and
`tests/lab_template_test.py` prove that `template.yaml` equals
`deployed-template.snapshot.json` plus exactly the add and remove operations in
`pending-changes.json`, and nothing else. A remove names an existing mapping key
(no value, no list items); replace and overwrite remain refused. After a deploy is executed, refresh the
snapshot (`aws cloudformation get-template --template-stage Original`), empty
`operations`, and empty this list.

**Pending: run job queue.** Jobs on a run queue in acceptance order and run one at a time. A worker that picks up a message for a job that is not yet at the head of its run re-sends the same references to the command queue with a delay (10 s doubling to 120 s) and settles the delivery. That is the worker's only queue write, so it needs one permission:

- `/Resources/WorkerRole/Properties/Policies/0/PolicyDocument/Statement/-` (`sqs:SendMessage` on the command queue only)

No queue, visibility, redrive or event-source setting changes.

**Pending: remove the request caps.** The hosted pipeline has no request or item cap. This change removes the `PredictionMaxRequests` template parameter and the `CYCLOTRON_PREDICTION_MAX_REQUESTS` variable on both functions, and the code stops reading `requestCeiling` and `maxItems` from `PublicationTargets` (they become unknown keys, which the parser rejects). Operations (all `remove`, each naming an existing key in the snapshot):

- `/Parameters/PredictionMaxRequests`
- `/Resources/BffFunction/Properties/Environment/Variables/CYCLOTRON_PREDICTION_MAX_REQUESTS`
- `/Resources/WorkerFunction/Properties/Environment/Variables/CYCLOTRON_PREDICTION_MAX_REQUESTS`

**Deploy order is parameter first, then code. This order is required.** The new parser rejects `requestCeiling` and `maxItems`, so the new code started against a `PublicationTargets` value that still contains them fails at startup (`reviewed publication target configuration is invalid`). The currently deployed code treats both keys as optional and ignores them (v0.35.1), so removing them first is safe with the old code.

1. Read the live value (read-only): `aws cloudformation describe-stacks --stack-name cyclotron-console --query "Stacks[0].Parameters[?ParameterKey=='PublicationTargets'].ParameterValue" --output text`. Save it as the rollback copy.
2. Delete the `requestCeiling` and `maxItems` keys from every target object in that JSON, changing nothing else (account, run, `modelIdentity`, `standing`, `bindings`, `pendingOutputs` stay as they are).
3. Update the stack with the currently deployed template and package, only `PublicationTargets` changed (use `UsePreviousValue` for every other parameter, including `PredictionMaxRequests` for now), through a change set. Verify the change set shows one parameter change and the two Lambda environment updates only.
4. Deploy the new template and package. Do not pass `PredictionMaxRequests` in the parameters of this deploy (CloudFormation rejects a parameter the template does not declare). The change set must show the removal of `CYCLOTRON_PREDICTION_MAX_REQUESTS` from both functions and no other change.
5. Refresh the snapshot and empty this list as usual.

Rollback: restore the saved `PublicationTargets` value; old code accepts it, and new code does not.

Previously: The Decision Ready doorbell (lab code 8908a33, package sha256 `90e3ed86159c6c21141abf6b0c0ef6e4217bb49aa781dc177d7638e385509c1f`) was deployed on 2026-10-27 with change set `decision-doorbell-8908a33`, with `DecisionEventBusArn` empty: no grant and no events until it is set to Papyrus's bus ARN. The snapshot was refreshed.

Previously: the machine invoker's /machine/decisions grant was deployed on 2026-10-26 with change set `machine-decisions-invoke`; the snapshot was refreshed.

Earlier: **No pending changes.** The record-lake cutover was deployed on 2026-10-22 with change set `record-lake-v0-34-0` (v0.34.0, package sha256 `63b054b87abc5050891ae14fe9c7b8a330d9d5d2a997de5fe5ab8edd78da3a9e`): `*/records/*` create-only in the bucket policy and Get/Put on `*/console/records/*` for the BFF and worker roles. All five journals were converted with scripts/dev/convert_to_record_lake.py (4,385 records; control state 18,955,946 -> about 1,199,000 bytes), and the snapshot was refreshed from the deployed stack.
