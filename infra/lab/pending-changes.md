# Pending lab stack changes

Reviewed, committed, not yet deployed. `scripts/dev/check_lab_template.sh` and
`tests/test_lab_template.py` prove that `template.yaml` equals
`deployed-template.snapshot.json` plus exactly the add-only operations in
`pending-changes.json`, and nothing else. After a deploy is executed, refresh the
snapshot (`aws cloudformation get-template --template-stage Original`), empty
`operations`, and empty this list.

**No pending changes.** The six verifier changes (LabVerifierEnabled, HasLabVerifier, LabVerifierRole, the BFF AssumeRole statement and the two gated BFF environment variables) were deployed on 2026-10-21 with change set `curation-routes-v0-31-0` (v0.31.0, package sha256 `9225ea1516105dff49c51d43866d4d6ae6c0613df26fce92aff6a3cb1335a204`), and the snapshot was refreshed from the deployed stack. `LabVerifierEnabled` is `false` until Prospectus enables the verifier.
