# Pending lab stack changes

Reviewed, committed, not yet deployed. `scripts/dev/check_lab_template.sh` and
`tests/test_lab_template.py` prove that `template.yaml` equals
`deployed-template.snapshot.json` plus exactly the add-only operations in
`pending-changes.json`, and nothing else. After a deploy is executed, refresh the
snapshot (`aws cloudformation get-template --template-stage Original`), empty
`operations`, and empty this list.

**Pending: run job queue.** Jobs on a run queue in acceptance order and run one at a time. A worker that picks up a message for a job that is not yet at the head of its run re-sends the same references to the command queue with a delay (10 s doubling to 120 s) and settles the delivery. That is the worker's only queue write, so it needs one permission:

- `/Resources/WorkerRole/Properties/Policies/0/PolicyDocument/Statement/-` (`sqs:SendMessage` on the command queue only)

No queue, visibility, redrive or event-source setting changes.

**Pending: Decision Ready doorbell.** When a machine decision job finishes, the worker puts one `Decision Ready` event (no decision content) on Papyrus's EventBridge bus. Nothing is created or granted until the `DecisionEventBusArn` parameter is set to that exact bus ARN at deploy time (default empty: no variable value, no grant, no events). Operations:

- `/Parameters/DecisionEventBusArn` (String, default empty, ARN pattern or empty)
- `/Conditions/HasDecisionEventBus` (parameter is not empty)
- `/Resources/WorkerFunction/Properties/Environment/Variables/CYCLOTRON_DECISION_EVENT_BUS_ARN` (the parameter)
- `/Resources/WorkerRole/Properties/Policies/0/PolicyDocument/Statement/-` (`events:PutEvents` on exactly that ARN, only when the condition holds)

Previously: the machine invoker's /machine/decisions grant was deployed on 2026-10-26 with change set `machine-decisions-invoke`; the snapshot was refreshed.

Earlier: **No pending changes.** The record-lake cutover was deployed on 2026-10-22 with change set `record-lake-v0-34-0` (v0.34.0, package sha256 `63b054b87abc5050891ae14fe9c7b8a330d9d5d2a997de5fe5ab8edd78da3a9e`): `*/records/*` create-only in the bucket policy and Get/Put on `*/console/records/*` for the BFF and worker roles. All five journals were converted with scripts/dev/convert_to_record_lake.py (4,385 records; control state 18,955,946 -> about 1,199,000 bytes), and the snapshot was refreshed from the deployed stack.
