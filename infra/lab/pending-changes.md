# Pending lab stack changes

Reviewed, committed, not yet deployed. `scripts/dev/check_lab_template.sh` and
`tests/test_lab_template.py` prove that `template.yaml` equals
`deployed-template.snapshot.json` plus exactly the add-only operations in
`pending-changes.json`, and nothing else. After a deploy is executed, refresh the
snapshot (`aws cloudformation get-template --template-stage Original`), empty
`operations`, and empty this list.

## Record-lake prefix (S1, for the S3 deploy)

Three add-only list appends open one new key prefix, `<account>/console/records/`, for write-once
record-lake objects (docs/s3-cloud-storage.md, "Record lake"). Nothing is replaced or removed, and no
Lambda code changes.

- `/Resources/BooksBucketPolicy/Properties/PolicyDocument/Statement/1/Resource/-` adds `${BooksBucket}/*/records/*` to the existing deny-unless-`If-None-Match: *` statement, so records are create-only at the bucket, like `payloads/` and `anchor.json`. There is still no delete grant anywhere.
- `/Resources/BffRole/Properties/Policies/0/PolicyDocument/Statement/0/Resource/-` lets the BFF `GetObject`/`PutObject` under `*/console/records/*`.
- `/Resources/WorkerRole/Properties/Policies/0/PolicyDocument/Statement/0/Resource/-` does the same for the worker.

No `s3:ListBucket` is added. Reading covered records is a GET by sequence; listing (audit, orphan janitor) is
not needed by S2 and would be a separate, reviewed grant.
