# Pending lab stack changes

Reviewed, committed, not yet deployed. `scripts/dev/check_lab_template.sh` and
`tests/test_lab_template.py` prove that `template.yaml` equals
`deployed-template.snapshot.json` plus exactly the add-only operations in
`pending-changes.json`, and nothing else. After a deploy is executed, refresh the
snapshot (`aws cloudformation get-template --template-stage Original`), empty
`operations`, and empty this list.

## Journal byte budget bridge (2026-10-21, approved by Ryan)

The next deploy of `cyclotron-console` changes exactly these paths:

| Path | Change |
|---|---|
| `/Parameters/JournalMaxBytes` | New parameter, default `4194304` (4 MiB), 1 to 64 MiB. |
| `/Resources/BffFunction/Properties/Environment/Variables/CYCLOTRON_MAX_BYTES` | `Ref: JournalMaxBytes`. |
| `/Resources/WorkerFunction/Properties/Environment/Variables/CYCLOTRON_MAX_BYTES` | `Ref: JournalMaxBytes`. |

A temporary bridge: raises how large each S3 journal document may grow while the
record-lake redesign moves history out into write-once records.

