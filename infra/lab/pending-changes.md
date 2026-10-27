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

Previously: the request caps were removed on 2026-10-27: change set `targets-remove-ceilings` (PublicationTargets without `requestCeiling`/`maxItems`), then `no-ceilings-377c478` (lab code 377c478, package sha256 `6ca1b278be02e4c938e12b31e5295a5427eac2711d40954f8d1c09f53583c9fe`) removing `PredictionMaxRequests` and `CYCLOTRON_PREDICTION_MAX_REQUESTS`. The snapshot was refreshed.

Previously: The Decision Ready doorbell (lab code 8908a33, package sha256 `90e3ed86159c6c21141abf6b0c0ef6e4217bb49aa781dc177d7638e385509c1f`) was deployed on 2026-10-27 with change set `decision-doorbell-8908a33`, with `DecisionEventBusArn` empty: no grant and no events until it is set to Papyrus's bus ARN. The snapshot was refreshed.

Previously: the machine invoker's /machine/decisions grant was deployed on 2026-10-26 with change set `machine-decisions-invoke`; the snapshot was refreshed.

Earlier: **No pending changes.** The record-lake cutover was deployed on 2026-10-22 with change set `record-lake-v0-34-0` (v0.34.0, package sha256 `63b054b87abc5050891ae14fe9c7b8a330d9d5d2a997de5fe5ab8edd78da3a9e`): `*/records/*` create-only in the bucket policy and Get/Put on `*/console/records/*` for the BFF and worker roles. All five journals were converted with scripts/dev/convert_to_record_lake.py (4,385 records; control state 18,955,946 -> about 1,199,000 bytes), and the snapshot was refreshed from the deployed stack.
