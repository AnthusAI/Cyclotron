# Applying reviewed classifier criteria to the live lab

Source of truth: `infra/lab/criteria/*.json` (one file per classifier plus `definition.json`).
Each file holds the question, labels, doctrine source, the editor approval (who, when) and the
previous text. Applying a file saves a classifier revision whose config carries a `criteria`
block (file, doctrine source, approval, question sha256), so the catalog alone shows where each
question came from and who approved it. `scripts/dev/lab_criteria.py plan` compares the files
with the live catalog (read-only, get-only S3 wrapper); `tests/test_lab_criteria.py` proves the
committed files are exactly what a deploy applies.

Every step that writes needs Ryan's explicit approval. Agents run only the read-only steps.
No Lambda package is needed: the code path (`save_classifier`, bootstrap, priming) is already
deployed, and the only stack change is the `PublicationTargets` parameter.

## What the code does today (verified by reading the code and the live catalog)

1. **A new classifier revision does not move the standing run.** The run's frozen config embeds a
   copy of each classifier (question included) and `decide` builds its specs from that copy
   (`hosted_publications.PublicationCommandRuntime.execute`). The engine store is named for the
   cyclotron id (`publication-<sha256(cyclotron_id)>`) and records the definition fingerprint,
   which includes every question; `Cyclotron.open` refuses a changed definition and
   `add_classifiers` refuses a changed specification (`embedded_cyclotron.py`,
   `classifier_set.stale_definition_error`). The run config is never rewritten, because the
   machine workspace binding is a hash of it. So a changed question needs a new run **and a new
   cyclotron id**. That is the "definition versions" limitation, ticket ade3088c (open, P2):
   until Cyclotron.open accepts a new definition revision for an existing store, the supported
   path is a successor run (`docs/hosted-publications.md`, Capacity: "plan a successor run").
2. **`save_classifier` has side effects beyond the new row.** It rolls every active cyclotron
   definition that contains the classifier to a new definition revision and activates it. The live
   run stays pinned to definition revision 1 (`run_cyclotron_definitions`), so decisions are
   unaffected. But `priming_snapshot` compares the catalog's latest classifier revision with the
   run's frozen one and refuses ("classifier revision changed; review the publication binding"),
   so the moment revision 2 exists, `prime` and the bearer `accept` auto-prime fail for the old
   run, and `accept` of a revision 1 draft fails ("preview again"). Hence the order below.
3. **Priming is per classifier revision.** Votes are keyed `(classifier, revision)`. Revision 2 has
   no votes, so `catalog_priming` raises "no accepted examples for the selected classifier
   revisions". Re-importing the same items is refused ("item already exists; use the catalog
   correction workflow"), so editors relabel the existing priming items under revision 2 with
   `labelItem`. Revision 1 labels are never silently reused for a changed question.
4. **Bindings.** `PublicationTargets` bindings carry an opaque `revision` ("1"). A request with a
   different `bindingRevision` is refused (409 "publication binding changed"). The Papyrus router
   holds one `cyclotron.runId` and one `cyclotron.bindingRevision` for all publications
   (`CONFIG#router`), so all four bindings (including the pending `data-parade`) move together.
   A publication cannot appear in two targets, so the old and new runs cannot both be live.
5. **Router behaviour (Papyrus, `docs/monitoring-cloud.md`).** PARK and `criteria_pending` apply
   only to `pendingOutputs` (today `data-parade-relevance`) and do not change. Any other 409,
   including a stale binding, stops the sweep cleanly; items stay where they were and nothing is
   re-sent. A POST is never repeated. So a mismatch between router config and lab fails closed.
6. **Decisions already made** stay where they are: the old run's jobs and engine store, catalog
   labels at revision 1, and Papyrus's `ROUTE#`/`DELIV#` receipts (run id, binding revision "1").
   They are not re-decided; the new criteria apply to newly discovered items. Re-deciding recent
   items is possible (the request id includes run and binding revision) but spends requests and
   is a separate approval. Machine feedback for items delivered under binding "1" is refused by
   binding "2", so drain outstanding feedback first or accept that it is dropped.
7. **Request ceiling.** The cap is per store ledger. The new store starts at 0, so copying
   `{"total":300,"perDay":100,"since":"2026-10-21"}` would grant a second budget. Set `total` to
   300 minus the old run's attempts (read the `shared-batches` ledger or count the run's
   `publication` jobs) unless Ryan wants a fresh budget. `maxItems` (300) counts the items copied
   into the new run at bootstrap (currently the whole `hn-operational` list, 104).

## Runbook

Roll back for every step means: undo it in reverse order using the note given. Catalog journals are
append-only (the books bucket is versioned), so nothing below is deleted; new revisions simply stop
being referenced.

**0. Prepare (no writes).** Merge the criteria PR. Run
`AWS_PROFILE=cyclotron-production CYCLOTRON_LAB_ACCOUNT_ID=<LAB_ACCOUNT_ID> python scripts/dev/lab_criteria.py plan`: expect `save` for the three
classifiers and `define shared-source-relevance-r2` (revisions 2/2/2), and no `drift`. Save the
current stack parameter `PublicationTargets` (`aws cloudformation describe-stacks`, read-only) and the
Papyrus `CONFIG#router` item as the rollback copies. Get the old run's request usage. Editors/Ryan: confirm who
will relabel priming (11 `prime-p-apyr-us`, 12 `prime-threat-intelligence`, 14 `prime-anth-us`).
Editors and router: no change. Rollback: nothing to undo.

**1. Quiesce.** Set the Papyrus router `enabled: false`. Wait until the lab shows no `pending`/`running`
`web_jobs` for the run and `FEEDBACK#hn#pending` is empty. Editors: unchanged. Router: stops
cleanly; unprocessed items stay `collected`. Verify with the read-only watch script. Rollback: re-enable.

**2. Write the revisions.** `CYCLOTRON_LAB_ACCOUNT_ID=<LAB_ACCOUNT_ID> python scripts/dev/lab_criteria.py apply --apply --approved-by "<who approved running it>"`
(or the console `saveClassifier`/`saveCyclotronDefinition` as Ryan, from the same files). Writes three classifier
revisions 2 and the successor definition `shared-source-relevance-r2` revision 1. Re-running is a no-op.
Effects: the old family `shared-source-relevance` rolls to definition revisions 2 to 4 (active), the old run's
priming is paused (see above), nothing else. Verify: `plan` prints only `current`; the run
`<OLD_RUN_ID>` config fingerprint is unchanged. Rollback: nothing is
deleted and decisions on the old run are unaffected; only priming of the old run stays paused, which a rollback of step 4
accepts because its engine store is already primed.

**3. Bootstrap the successor run** (data only, no provider call): `POST /publication-runs/bootstrap` as Ryan with
`{"contractVersion":"cyclotron-publication/v1","requestId":"<new UUID>","name":"Standing Hacker News publication run (criteria r2)","config":{"cyclotron_id":"shared-source-relevance-r2","cyclotron_definition_revision":1,"item_list_id":"hn-operational"}}`.
The receipt gives `runId`. Verify: the run's `classifiers` are the three revision 2 rows with
`criteria.approval` (Ryan, 2026-10-22); `cyclotron_definition_revision` is 1; item count equals
`hn-operational`. Retrying the same `requestId` returns the same run. Rollback: leave the run unreferenced.

**4. Repoint the target (CloudFormation parameter only).** Change `PublicationTargets` to the JSON below, via a change
set (`sam deploy --no-execute-changeset`), review that only the `BffFunction`/`WorkerFunction` environment changes
and nothing is replaced, get Ryan's approval, execute.

```json
[{"accountId":"<LAB_ACCOUNT_ID>","runId":"<NEW_RUN_ID>","modelIdentity":"jev:jev-1.13.0","standing":true,
  "requestCeiling":{"total":<300 minus used>,"perDay":100,"since":"2026-10-21"},"maxItems":300,
  "bindings":[{"publicationId":"anth-us","outputs":["anth-us-relevance"],"revision":"2","priming":{"listId":"prime-anth-us"}},
  {"publicationId":"p-apyr-us","outputs":["p-apyr-us-relevance"],"revision":"2","priming":{"listId":"prime-p-apyr-us"}},
  {"publicationId":"threat-intelligence","outputs":["threat-intelligence-relevance"],"revision":"2","priming":{"listId":"prime-threat-intelligence"}},
  {"publicationId":"data-parade","outputs":["data-parade-relevance"],"revision":"2"}],
  "pendingOutputs":["data-parade-relevance"]}]
```

(Only p-apyr-us has `priming` today; adding it to the other two is optional and Ryan's call.) Effects: the old
run stops being a target; anything addressed to it or to binding "1" is refused. The router is off, and the
Papyrus sites that pin `X-Papyrus-Binding` must be updated to "2" or their curation calls answer 409
`account_changed`. Verify: `status` for each publication answers with `bindingRevision: "2"` (503 "no verified
snapshot yet" until the first decision is normal); a `prime` response envelope shows the new question text.
Rollback: restore the saved `PublicationTargets` value (same change-set process); the old run is untouched.

**5. Relabel and prime.** Editors label each priming item under revision 2 (`labelItem` with `classifier_revision: 2`),
then run `prime` (or the curation `accept`) for each binding with priming. Verify `primedExamples` per classifier
and the snapshot fingerprint. No provider call is made. Until this is done the successor decides with no examples,
so do it before step 6. Rollback: priming additions are recorded per store; abandon the successor by step 4's rollback.

**6. Repoint and re-enable the router.** Update `CONFIG#router` (`infra/monitoring/seed_router_config.py`):
`cyclotron.runId=<NEW_RUN_ID>`, `cyclotron.bindingRevision="2"`; first one `mode: dry-run` sweep (request ids change, no
Cyclotron call), then `live`. Verify the first receipts: `decision.bindingRevision` "2", expected labels on a few
items you can judge, `waitingForClassifier` still only `data-parade`. Rollback: restore the saved router item and
target parameter together; decisions already delivered stay delivered.

**7. Clean up.** The successor definition carries no DRAFT marker (all three approved). Optionally give the old family a
clean superseded name with `saveCyclotronDefinition("shared-source-relevance", "Shared source relevance (superseded by
shared-source-relevance-r2)", <its active refs>, <its settings>)`; this does not touch the retired run's pin.
Record the run id, change-set id and files' commit on the ticket. Moving this to a single run again is what ade3088c delivers.
