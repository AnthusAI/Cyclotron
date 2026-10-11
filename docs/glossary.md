# Glossary

One word per concept, used in the engine code and tests, the console, the
marketing site, and the docs. This page is the source of truth.

| Concept | Use | Retire |
|---|---|---|
| The thing being judged | item | document, article, target (as generic) |
| One AI decision about one kind of item | cyclotron | scorecard (done) |
| One reviewed output of a cyclotron: its own rubric, labels, ML model and evaluation; its result is a decision | classifier | score |
| An internal question inside a classifier. Nobody reviews it; its answer feeds only that classifier's ML model | supporting question | element, decision element |
| A number derived from a supporting question's answer (or the main answer), an input to the ML model | feature | |
| The fast model that answers questions | decision model | engine, backend |
| The small model trained on your labels | ML model | head, ML head, learned head, fitted head, perceptron |
| The capable model that proposes changes | LLM optimizer | optimizer alone, agent |
| What the cyclotron outputs for an item | decision | prediction, answer, final answer, verdict, classification, score |
| How sure it is | confidence | probability (in UI) |
| The person who checks decisions | reviewer | human, editor, annotator, user |
| The act of checking a decision | review | labeling, voting, feedback (as a verb) |
| People guiding what a cyclotron learns, through their reviews | steer, steering | |
| The reviewer's recorded answer | label | vote |
| A label that differs from the decision | correction | disagreement, miss |
| A label that matches the decision | agreement | match |
| The reviewer's written reason | explanation | rationale, comment, note |
| Labels and explanations together | feedback | human feedback, signal |
| One pass: decide, review, learn | cycle | round, step, iteration |
| What the optimizer suggests | proposal | hypothesis, change |
| A proposal under test | candidate | trial, experiment |
| When a candidate passes | promoted | accepted, activated |
| When it fails | dropped | rejected (collides with the label) |
| A numbered state of a cyclotron | version | revision, edition |
| The labels kept back for testing | held-out | development, audit, evaluation set |
| The numbers | accuracy, precision, recall, calibration | Brier and ECE on detail pages only |

Rules behind the table:

- "Decision" is the product word. The output is a decision, not a
  prediction. Prediction implies forecasting; buyers decide.
- The human verb for checking one decision is "review". Labeling sounds like
  data entry. "Steer" is approved for what a team does over time: its reviews
  steer a cyclotron, and the team can step back once monitoring shows the
  cyclotron has learned enough. Use it for guidance over time, not for the
  single act of checking. In marketing copy, "editor" may stand in for
  "reviewer" when the example is editorial, as in the opening story.
- Nothing about versions says accepted or rejected, because reject is a
  label in the editorial example.
- "Score" is reserved for Primus, where a scorecard produces scores.

## How it applies in code

Phase 1 changed user-visible strings only: the console UI and the marketing
site. `src/decision_flywheel/glossary_test.py` scans trace-ui copy for retired
words (scorecard, vote, votes, voting, rationale, labeling, annotator).
Strings that must keep one go in `docs/glossary-allowlist.txt`.

Identifier renames follow in later phases, each merged green. Until then these
names remain in code, payloads, GraphQL fields, test ids, and CSS classes:

| In code now | Glossary word | Phase |
|---|---|---|
| `vote` | label | planned |
| `rationale`, `comment` | explanation | planned |
| `revision`, `edition` | version | planned |
| `head` | ML model | planned (awaiting decision) |
| `prediction` | decision | last, with a version bump |

## Outputs and supporting questions

"Classifier" covers one reviewed output: reviewers label it, and it has its own rubric, examples, ML model and
evaluation. Inside a classifier, the optimizer can add supporting questions (code and README: `tasks`, "decision
element"). They have no rubric of their own and are never labeled; their answers become features for that
classifier's ML model, and the optimizer revises them as one of the classifier's three controls (rubric, examples,
supporting questions). A cyclotron is a versioned, ordered group of classifiers.
