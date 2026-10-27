# Pending lab stack changes

Reviewed, committed, not yet deployed. `scripts/dev/check_lab_template.sh` and
`tests/lab_template_test.py` prove that `template.yaml` equals
`deployed-template.snapshot.json` plus exactly the add-only operations in
`pending-changes.json`, and nothing else. After a deploy is executed, refresh the
snapshot (`aws cloudformation get-template --template-stage Original`), empty
`operations`, and empty this list.

**No pending changes.** The Decision Ready doorbell (lab code 8908a33, package sha256 `90e3ed86159c6c21141abf6b0c0ef6e4217bb49aa781dc177d7638e385509c1f`) was deployed on 2026-10-27 with change set `decision-doorbell-8908a33`, with `DecisionEventBusArn` empty: no grant and no events until it is set to Papyrus's bus ARN. The snapshot was refreshed.

Previously: the machine invoker's /machine/decisions grant was deployed on 2026-10-26 with change set `machine-decisions-invoke`; the snapshot was refreshed.

Earlier: **No pending changes.** The record-lake cutover was deployed on 2026-10-22 with change set `record-lake-v0-34-0` (v0.34.0, package sha256 `63b054b87abc5050891ae14fe9c7b8a330d9d5d2a997de5fe5ab8edd78da3a9e`): `*/records/*` create-only in the bucket policy and Get/Put on `*/console/records/*` for the BFF and worker roles. All five journals were converted with scripts/dev/convert_to_record_lake.py (4,385 records; control state 18,955,946 -> about 1,199,000 bytes), and the snapshot was refreshed from the deployed stack.
