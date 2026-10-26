# Pending lab stack changes

No pending CloudFormation changes. The v0.35.1 runtime artifact was deployed on 2026-10-26 with change set `v0-35-1-standing-cap-removal`; both Lambda code keys reference `cyclotron-console/backend/7b9fe023c3752ee7ab8c462c778fe7f5302cd5fa61d65d04fe68a7d37b643248`. The deployed template snapshot was refreshed from the live `Original` template after verification.

The record-lake cutover was deployed on 2026-10-22 with change set `record-lake-v0-34-0` (v0.34.0, package sha256 `63b054b87abc5050891ae14fe9c7b8a330d9d5d2a997de5fe5ab8edd78da3a9e`): `*/records/*` create-only in the bucket policy and Get/Put on `*/console/records/*` for the BFF and worker roles. All five journals were converted with `scripts/dev/convert_to_record_lake.py`; the snapshot was refreshed from the deployed stack.
