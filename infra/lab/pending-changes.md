# Pending lab stack changes

Reviewed, committed, not yet deployed. `scripts/dev/check_lab_template.sh` and
`tests/test_lab_template.py` prove that `template.yaml` equals
`deployed-template.snapshot.json` plus exactly the add-only operations in
`pending-changes.json`, and nothing else. After a deploy is executed, refresh the
snapshot (`aws cloudformation get-template --template-stage Original`), empty
`operations`, and empty this list.

**No pending changes.** The journal byte budget bridge (JournalMaxBytes, set to 16 MiB) and v0.33.2 (package sha256 `9891f3f5ad64c340b1d67b3768e98b18dd9e540266314f96669041510e6b138c`) were deployed on 2026-10-22 with change set `journal-budget-v0-33-2`, and the snapshot was refreshed from the deployed stack.
