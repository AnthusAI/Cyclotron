# Local review: optional curated-example priming

Baseline: Cyclotron `547dc00`. Branch: `codex/priming-http-import`.
Initiative: `decision-flywheel-da886c09-7acb-45d3-acf8-c81585f26d8a`.
Milestone: `decision-flywheel-479b4a63-6bfe-4524-9b29-7ae2e71d20ad`.
Stories: `decision-flywheel-91909d64-f351-4441-8a3c-e2cf36b7b23f` and
`decision-flywheel-89212dcc-72c9-4f47-bb08-f150a1515ebd`.

Operators can optionally import schema-valid CSV/JSON/JSONL/Excel rows or use
customer-hosted HTTP transformation. The workflow validates complete previews,
keeps extraction feedback distinct from explicit human labels/explanations,
and accepts the item and all selected classifier labels in one retry-safe
transaction. The reusable UI has host-supplied transport and candidate rendering.
It is embedded in Papyrus's actual References view by the companion local branch.

Priming examples are pinned as initial training context for a new embedded SDK
store or workspace run. They do not generate pretend predictions, operational
review counts, or held-out data. Operational identities must remain disjoint;
existing stores cannot silently replace their seed examples. No priming is needed
for normal operation.

Validation:

- Full Python run: 1,360 passed, 1 skipped. The subsequently added standalone
  configuration/credential specs pass in the focused priming checks.
- Focused priming/config/API/context/glossary specs: 29 passed.
- Full UI run and TypeScript/build verification cover the shared controls,
  explicit explanations, row errors, retries, stale host responses and the
  acceptance/cancellation transaction race.
- Papyrus browser verification covers both existing curation paths, research
  revision, two output labels, stored provenance and one-output mobile cancel.
- Synthetic source content renders as text. Transformer labels are refused.
  Authentication/origin checks, credential refusal, bounded responses, and
  disabled redirects are retained. There is no customer-code execution in the
  Cyclotron host and no new credential or IAM provisioning.

Previews: standalone on 8796; Papyrus on 8798; synthetic external transformer on
8797. The earlier callable draft at `2a45f44` remains preserved in the separate
`cyclotron-priming` worktree and is superseded.

Public pushes and deployment are held. Production service/host configuration and
Ryan's selected real examples remain pending. Excel support is `.xlsx`, batches
are bounded to 200 rows/4 MB files and an aggregate 8 MB draft. Classifiers must
share a compatible item schema. Remote cancellation can remain pending and is
shown explicitly. The exact target values are inspectable; a future provider's
complete prompt is not fabricated.
