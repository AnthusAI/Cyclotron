# Hypothesis portfolio (Stage 1)

Decision-model calls are cheap, so the optimizer should not pick one winner and commit. It should keep many
ideas (rubric, example and question changes), keep evidence about each for a long time, and screen many
candidates cheaply. Stage 1 is the library layer only; the live optimization loop is unchanged.

## The ledger (`hypothesis_ledger.py`)

`HypothesisLedger(db)` works over the flywheel SQLite database. Ideas stay in `control_ideas`; the ledger only
references their ids.

- `idea_evaluations(idea_id, item_id, correct, label, choice, confidence, context_version, evaluated_at)`,
  keyed by `(idea_id, item_id, context_version)`. Recording the same key again replaces the row.
- `idea_verdicts(idea_id, at, verdict, reason, evidence)` is append-only. Verdicts are `screened_out`,
  `advanced`, `promoted`, `retired`, `revived`, each with a plain-language reason.

Queries: `correctness_vector`, `items_evaluated`, `paired_comparison(a, b)` (gained, lost, net and an exact
two-sided sign-test p-value over shared items), `running_score(idea, since=None)` (Beta(1,1) posterior mean and
95% interval, so an idea can be tracked on newly arriving labels), `revive_candidates()` (screened-out ideas
whose score on evaluations made after that verdict looks promising), `stubborn_items(min_ideas=3)` (items every
evaluated idea gets wrong, a hint that the label or the item is the problem), and
`summary_for_optimizer(limit=...)`, a deterministic, length-capped list of dicts (idea id, short text, items
tested, accuracy, item ids it fixed and broke versus the incumbent, last verdict and reason).

## The screen (`idea_screen.py`)

`await screen(candidates, incumbent, items, scorer, ...)` is engine-agnostic. `scorer(idea, item)` is injected
and returns something with `choice`, `confidence` and `correct`; the screen never calls a model itself.

1. Items are shuffled deterministically by `seed`. A reserve (`reserve_fraction`, default 20%) is split off.
2. Each stage scores the surviving candidates, and the incumbent once (shared), on the next block of screening
   items, then keeps the top fraction by cumulative accuracy. Default stages: 50 items keep half, 200 items keep
   half, then all remaining screening items.
3. Confirmation scores the finalists and the incumbent on the reserve. Each finalist gets a paired exact sign
   test against the incumbent, adjusted across finalists with Holm's step-down method (valid under any
   dependence, never weaker than Bonferroni). A finalist clears the gate only if its net gain is positive and
   its adjusted p-value is below `alpha`.
4. Every evaluation and verdict is written to the ledger when one is supplied. `concurrency` bounds in-flight
   scorer calls. If a stage or the confirmation would push calls past `max_calls`, the screen stops before it
   and returns a partial result with `truncated=True`; a truncated screen never clears the gate.

`ScreenResult` carries ranked finalists, which cleared, per-stage tables, the paired statistics and the total
scorer calls.

## Why the reserve exists

Scoring many ideas on one dev set and keeping the best finds noise: the maximum of N equally good ideas is
above average by chance, and a sign test on the same items that picked it is not valid. Items used to screen
or advance an idea are never used to confirm it. The reserve is touched once, by the finalists only, so the
test sees fresh items and the family-wise correction only has to cover the finalists. With all-equal
candidates the gate passes rarely (the tests simulate 200 screens and require at most 10%).

## Stage 2 (not built)

- Wire the screen into `control_scheduler` so several ideas are screened per round instead of one trial.
- A shadow/ensemble portfolio of the top-k diverse ideas, whose disagreement routes items to review.
- Ledger summaries in the optimizer context (`summary_for_optimizer` feeding `prior_control_ideas`).
- Cross-run idea memory, so screened-out ideas are revived when later labels favour them.
