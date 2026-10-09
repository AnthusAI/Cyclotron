# Papyrus integration: an embeddable cyclotron

Written 2026-10-09. Papyrus is the first real integration and the public
example. Plan: the "Embeddable cyclotron: Papyrus integration" epic here
(decision-flywheel-a73f05cc) and
the matching epic in Papyrus (`project/wiki/cyclotron-integration.md` there).

## The Papyrus touch-point

**Where the item lives.** Every candidate source ends as a `Reference` row
with `curationStatus: "pending"`. Research dispatch, `research_explorer.tac`
source discovery, citation-led discovery (`src/papyrus_content/reference_discovery.py`)
and inbound email submissions all funnel through
`intake_research_packet_proposals` (`src/papyrus_content/assignments_workflow.py`)
and `build_reference_catalog_registration_records`
(`src/papyrus_content/catalog.py`). Registration also opens one
`curation.reference-intake` Assignment per Reference and stores an
`ingestion_rationale` Message. That pending Reference is the item.

**The decision.** One cyclotron, `papyrus-relevance`, with one classifier,
`relevant`, labels `include` and `exclude`: "Should this candidate become a
reference for this publication, given its mission and policies?" The
publication's parameters are its doctrine (`corpora/papyrus-publication-doctrine.yml`,
mission and policy), which seeds the first rubric.

**What the editor does today.** The References tab
(`components/newsroom-references-view.tsx`) shows Accept, Reject and Archive.
They call the `reviewReferenceCuration` mutation
(`amplify/functions/category-action/handler.ts`), which updates the Reference
and writes a `reference_curation` Message with `{action, curationStatus,
reasonCode}` metadata and a `comment` SemanticRelation. The tab passes no
explanation or reason code (`topic-steering-workspace.tsx`, the
`onReview={(reference, action) => ...}` call); only the semantic-detail panel
has a reason picker.

**What the editor will see.** The same tab, with the decision and confidence
on each pending item ("Include, 82% sure"), a "Needs review" filter first, and
why each item was picked for review. The rating control is thumbs up or down,
a reason on thumbs down (the existing codes: out of scope, policy exclusion,
duplicate, low quality, unavailable, provenance, other) and an optional
explanation. A status strip above the list shows what the cyclotron is doing.

**What flows back.** The existing mutation, plus the explanation and the id of
the decision shown. The label rule already exists as
`scopeTrainingLabelForReference` (`lib/reference-policy.ts`) and its Python
twin: accepted is `include`; rejected as out of scope or policy exclusion is
`exclude`; other rejections and archives are no label, because a duplicate is
not irrelevant. `references export-scope-training` already exports these.

## Integration architecture

**Library in-process, not the workspace.** Papyrus scales to zero and has no
long-running server; its Python worker already runs assignments against
AppSync. The FastAPI/GraphQL workspace assumes one reviewer, a local SQLite
file and a resident worker. Papyrus imports the engine in its worker
through a new SDK facade.

**Stores.** Papyrus GraphQL stays the system of record for items, decisions
and reviews. The engine's SQLite is its private state (transcripts, cache,
ML model) at `var/cyclotrons/papyrus-relevance.sqlite3` on the worker, with a
durable copy under the media bucket at `cyclotrons/papyrus-relevance/state.sqlite3`.
One writer at a time: the sweep holds an exclusive claim on a
`cyclotron.relevance` Assignment, the claim mechanism Papyrus already uses.
Replay rebuilds a lost file from the GraphQL labels.

**Definition and version.** `corpora/papyrus-steering.yml` gains a
`relevanceCyclotron` block on the canonical corpus: cyclotron id, classifier
id, labels, decision model, LLM optimizer model and review program settings.
The doctrine hash is part of the classifier version, so a doctrine edit makes
a new cyclotron version. One installation is one publication, so there is one
cyclotron per installation.

**Flow.**

1. Intake registers a pending Reference (unchanged).
2. `papyrus references decide-relevance --corpus-key K --apply` finds pending
   References with no current decision, builds an item (id = Reference
   `lineageId`; values: title, source URI and domain, ingestion rationale,
   summary or opening text, published date, discovering assignment and
   section), calls `decide`, and writes a SemanticRelation
   `relevance_decision_is` from the Reference to node `relevance.include` or
   `relevance.exclude`. It reuses the existing fields: `score` (probability of
   include), `confidence`, `classifierId`, `modelVersion` (cyclotron version)
   and `reviewRecommended`, with `{decisionId, reviewReason, propensity,
   cycle}` in metadata.
3. The editor reviews through the existing mutation.
4. The same sweep reads new `reference_curation` Messages since its cursor,
   maps them to labels, calls `review`, and writes the status snapshot as a
   `KnowledgeRawPayload` row `cyclotron-status-papyrus-relevance`, the pattern
   the Newsroom summary snapshot already uses.

Phase one never accepts a Reference without a person. Whether `include`
decisions not picked for review may be accepted automatically is Ryan's
call, because it changes the knowledge base's "a human accepted it"
invariant.

## The embeddable surface the engine must add

**SDK (Python first).** `Cyclotron.open(store, definition, model, optimizer)`
and four calls. The engine owns labels, held-out partitions and selection
propensities; today `DecisionFlywheel.predict` and `improve` make the caller
pass them.

- `decide(item) -> Decision`: `{decision_id, item_id, label, confidence,
  probabilities, version, review: {selected, reason, propensity}}`.
- `review(decision_id, label, *, explanation, reason_code, reviewer,
  selected_by)`: records a label or correction; `selected_by` is `program` or
  `reviewer`.
- `status() -> CyclotronStatus`.
- `subscribe(after=cursor)`: committed events (decision, review, promoted,
  dropped, review-rate-changed) for a live UI or an export.

**Status data shape, `cyclotron-status/v1`** (JSON Schema plus TypeScript
types in the package):

```json
{"cyclotron": {"id": "papyrus-relevance", "version": 13, "classifier": "relevant"},
 "asOf": "2026-10-09T14:00:00Z",
 "alignment": {"window": 200, "labels": 184, "accuracy": 0.86,
   "precision": 0.81, "recall": 0.9, "positiveLabel": "include",
   "measuredOn": "program-selected reviews"},
 "calibration": {"saysSure": 0.84, "isRight": 0.86, "gapPoints": 2,
   "curve": [{"bin": 0.8, "count": 41, "accuracy": 0.83}]},
 "reviewRate": {"state": "tapering", "rate": 0.25, "auditFloor": 0.1,
   "confidenceThreshold": 0.7, "reason": "Audit accuracy 86% over 120 labels met the 85% target",
   "override": null, "nextCheckAfterLabels": 40, "expectedReviewsPerWeek": 12},
 "lastChange": {"kind": "promoted", "fromVersion": 12, "toVersion": 13,
   "at": "2026-10-08T09:12:00Z", "summary": "Added question: is this vendor marketing?"},
 "pending": {"decisionsAwaitingReview": 7, "staleSince": null}}
```

**Rating widget contract**, `cyclotron/components/review-control`.
Props: `itemId`, `decision {decisionId, label, confidence, classes, version}`,
`reviewReason?`, `mode: "thumbs" | "labels"`, `positiveLabel`,
`reasons? [{code, label, labelEffect: "exclude" | "none"}]`, `value?`,
`disabled`, `compact`. Events: `onReview({itemId, decisionId, label,
reasonCode, explanation})` and `onUndo({itemId, decisionId})`. It never talks to a server. Today's `LabelingReview` has no submit
event, thumbs mode or reasons.

**Status widget contract**, `cyclotron/components/cyclotron-status`. Props:
`status: CyclotronStatus`, `variant: "strip" | "card"`, `onOverride?`. The
strip reads "Version 13 · agrees 86% · says 84% sure, right 86% · reviewing
1 in 4: audit accuracy met target". Today's `ClassifierMetrics` parses raw
`cycle-metrics` events and shows no review rate or version.

## The review-rate program

Today review rates are fixed schedules (`PhasedFeedbackPolicy` in
`replay_feedback_policy.py`), keyed by cycle number. The program makes the
rate engine state, computed from metrics:

- **Onboarding:** review everything until each label has 30 reviews.
- **Tapering:** every 40 new program-selected labels, step down one notch
  (1.0, 0.5, 0.25, floor 0.1) if audit accuracy over the window meets the
  target and the calibration gap is at most 5 points.
- **Raised:** step back to 1.0 after a promotion, a doctrine change, or audit
  accuracy falling 5 points below target.

Selection per decision: always review below the confidence threshold; above
it, a deterministic random audit at the current rate. Each decision records
its propensity and reason ("low confidence", "random audit", "onboarding",
"after version 13"). Alignment numbers come only from program-selected
reviews; reviews editors choose themselves train the ML model but are not
audit evidence. An operator override sets a rate and an expiry and is logged
with who set it. The application shows state, rate, reason, override and
expected weekly reviews. This builds on the open experiments
(decision-flywheel-8ee62604, decision-flywheel-b0cf2975) and routing metrics
(decision-flywheel-7d61bc09); their results set the defaults above.

## Feeding the marketing site

The services page needs one real Papyrus rating changing a cyclotron. Papyrus
exports, through `subscribe` and its GraphQL records, a fixture in the
`editorial-run-v1.json` shape: per cycle the item title, decision,
confidence, version, review reason, label, reason code, explanation, and the
promotion events with their proposals. It carries `source: "papyrus-live"`
and a disclosure that the labels are real editor reviews. Explanations are
included only for reviews the editor marked as shareable; others are
redacted at export. The site shows a recorded change only
(decision-flywheel-a5b184fd).

## What this teaches Cyclotron

1. An application cannot pass its whole training and development sets to every call; the engine must own labels, partitions and propensities.
2. An application needs "decide this item" and "record this review" keyed by its own ids, not runs, jobs and commands.
3. The exported review form needs a submit event, a thumbs mode and reason codes, or every application rebuilds it.
4. Status widgets need a versioned snapshot with a schema, not a stream of raw trace events.
5. The review rate must be engine state with a reason and an override, not a fixed schedule in replay code.
6. Reviews a person chooses are different evidence from reviews the program selects, and the engine needs a word for each.
7. Customers already have reason codes, and some reasons mean "no label", not "exclude".
8. A serverless application needs a portable store and a single-writer lease, not a resident server.
9. The npm package is private, ships raw TSX with a path alias and has no release, so Papyrus can only pin a tarball.
10. The public API still says prediction, head and comment where the product says decision, ML model and explanation.
11. Applications version a cyclotron from their own parameters, such as doctrine, and need to see that in the status.
12. A marketing recording needs a consent and redaction switch for real explanations.

## Not determined

- Which publication goes first (Threat Intelligence, Pilobolus or Anth.us).
- Which decision model Papyrus uses (Jev or the OpenAI adapter) and who holds its key.
- Whether the 1-to-5 quality rating (`setReferenceQualityRating`), which also accepts or rejects, should feed this cyclotron; this plan says no.
- The real volume of pending candidates per week, which sets the onboarding length.
