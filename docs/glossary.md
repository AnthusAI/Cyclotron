# Glossary

One word per concept, used in the engine code and tests, the console, the
marketing site, and the docs. This page is the source of truth.

| Concept | Use | Retire |
|---|---|---|
| The thing being judged | item | document, article, target (as generic) |
| One AI decision about one kind of item | cyclotron | scorecard (done) |
| One question inside a cyclotron | classifier | element, score |
| The fast model that answers questions | decision model | engine, backend |
| The small model trained on your labels | ML model | head, learned head, fitted head |
| The capable model that proposes changes | LLM optimizer | optimizer alone, agent |
| What the cyclotron outputs for an item | decision | prediction, answer, verdict, classification, score |
| How sure it is | confidence | probability (in UI) |
| The person who checks decisions | reviewer | human, editor, annotator, user |
| The act of checking a decision | review | labeling, voting, steering, feedback (as a verb) |
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
- The human verb is "review". Labeling sounds like data entry; steering is a
  metaphor. In marketing copy, "editor" may stand in for "reviewer" when the
  example is editorial, as in the opening story.
- Nothing about versions says accepted or rejected, because reject is a
  label in the editorial example.
- "Score" is reserved for Primus, where a scorecard produces scores.

## How it applies in code

Phase 1 changed user-visible strings only: the console UI and the marketing
site. `src/decision_flywheel/glossary_test.py` scans trace-ui copy for retired
words (scorecard, vote, votes, voting, rationale, steer, steering, labeling,
annotator). Strings that must keep one go in `docs/glossary-allowlist.txt`.

Identifier renames follow in later phases, each merged green. Until then these
names remain in code, payloads, GraphQL fields, test ids, and CSS classes:

| In code now | Glossary word | Phase |
|---|---|---|
| `vote` | label | planned |
| `rationale`, `comment` | explanation | planned |
| `revision`, `edition` | version | planned |
| `head` | ML model | planned (awaiting decision) |
| `prediction` | decision | last, with a version bump |
