# Recall with an accuracy guard versus F1

## Question

How does the optimization objective change the final classifier and its measured
recall, precision, accuracy, and calibration? Compare two fresh replay trajectories,
not old predictions collected under different implementations.

## Frozen starting point

Use the 85 fully labeled items in `Three classifiers · interactive labeling`
(`fe9e2b07-523a-42c4-af18-2fc0f5543ad0`). Pin Anthus definition revision 1.
It contains the same three classifier revisions as this source. Do not substitute
the later four-classifier definition: Cyberian Gallery has no completed backfill.

Freeze the active source votes, explanations, item revisions, and arrival order
when creating both replays. Neither replay changes source feedback or the active
cyclotron edition. Both start with an empty rubric, no examples, and no learned
head. Use `protected-feedback-v1` in the fresh replays; do not relabel legacy
source traces as protected evaluation evidence.

## Controlled settings

Both conditions use the same frozen classifier definitions, item order, split
seed (`arxiv-web-v1`), Jev model (`jev-1.13.0`), optimizer model (`gpt-6-luna`),
rubric trigger (every two eligible label transitions), and remaining stage cadence
(every 20 eligible labels). Retain the 200-sample evaluation ceiling and current
class-coverage guards. Do not change the seed after inspecting performance.

The only intentional configuration difference is the effective selection policy:

| Condition | Primary | Secondary | Allowed secondary regression |
| --- | --- | --- | --- |
| Recall with guard | Positive-class recall | Accuracy | 0 |
| F1 | Positive-class F1 | None | Not applicable |

An explicit run policy overrides saved classifier policies for this run only.
Each classifier's positive class comes from its pinned class configuration.
Inspect the effective per-classifier policies before starting either replay.

All optimization requests, responses, decision requests, responses, triggers,
configuration snapshots, fitting events, and metrics must go through the API.
Use separate run-owned durable caches. Record returned usage and actual attempts;
do not infer monetary cost. Set and record explicit request and optimizer ceilings
before paid collection. A ceiling interruption is not a completed trajectory.
For this pilot, each replay has a ceiling of 1,000 decision-provider attempts and
100 optimizer calls. These are ceilings, not estimates or automatic allowances
for failed-call retries. The protected endpoint comparison has its own preflight
and request ceiling after both trajectories finish.

## Current coverage and limits

The read-only inventory on 2026-10-07 gives these positive-label counts under the
unchanged split seed:

| Classifier ID | All labels | Training positives | Development positives | Protected positives |
| --- | --- | --- | --- | --- |
| `06b3f241-174c-4a94-97e9-d7f575af1fea` | 85 | 5 | 2 | 1 |
| `knowledge-base-inclusion` | 85 | 3 | 0 | 2 |
| `3c06fe40-ac0f-4f30-82ea-293ff07838df` | 85 | 5 | 1 | 0 |

There are 16 protected items. This is a diagnostic pilot, not sufficient evidence
to declare one objective better. The knowledge-base classifier cannot pass the
current development coverage floor. The third classifier has no protected
positive examples, so its protected recall is unavailable, not zero. Do not
lower coverage floors, move protected examples into training, or search seeds to
manufacture an apparent improvement. Recent-balanced selection cannot create
positive labels that do not exist in a partition.

## Analysis and acceptance

Report each classifier separately, always in recall, precision, accuracy order.
Show positive and negative counts, raw decision-model versus final-head results,
probability calibration, new request counts, optimizer calls, and final context.
Distinguish running prequential metrics from fixed protected endpoint metrics.
Running metrics use the latest 200 human-labeled predictions, made before their
votes were revealed. Frozen source comments are revealed only with their votes.

Evaluate final frozen endpoints on the same protected items, with the same
evaluation clock and complete joint request context. Never apply a numerical head
whose feature-context provenance does not match that endpoint. Preserve complete
request/response drill-down for every evaluated item.

Record incomplete optimizations, missing class coverage, and unavailable metrics
explicitly. A diagnostic pilot can complete while the stronger comparison remains
inconclusive. Continue collecting real human labels for the current cyclotron and
run a separately versioned follow-up once each classifier has sufficient training,
development, and protected class coverage. Never overwrite this pilot's history.
