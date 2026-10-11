# Hypothesis portfolio (Stages 1 and 2)

Decision-model calls are cheap, so the optimizer should not pick one winner and commit. It should keep many
ideas (rubric, example and question changes), keep evidence about each for a long time, and screen many
candidates cheaply. Stage 1 is the library layer (ledger and screen). Stage 2, below, wires it into the live loop as an opt-in.

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

## Stage 2: the live loop (opt-in, default off)

`IdeaScreeningConfig` (`idea_screening.py`) is passed as `DecisionFlywheel(..., idea_screening=...)` and saved in
`runtime_state` like the selection policy, so a resumed run keeps it. Defaults: `enabled=False`,
`max_candidates=8`, `proposals_per_round=3`, `finalists=2`, `reserve_fraction=0.2`, `alpha=0.05`,
`max_screen_calls=2000`, `stages=((50,0.5),(200,0.5),(None,None))`. Study script flags: `--idea-screening`,
`--screening-proposals`, `--screening-finalists`, `--screening-max-candidates`, `--screening-reserve`,
`--screening-alpha`, `--screening-max-calls`; the config is written to `protocol.json`. With any primary
objective other than accuracy, one `idea-screening-unsupported-objective` event is logged and the legacy
one-trial-per-control path runs. With it off, nothing changes.

Where it applies: the `rubric` and `example_ids` controls in `ControlScheduler` (`improve_controls`), and the
`rubric` stage of `optimize_stage` (what the prequential study runs) once a non-provisional rubric exists.
Questions (`tasks`) stay on the legacy path.

Each round:

1. One optimizer call asks for `proposals_per_round` DISTINCT proposals along different axes (reply shape
   `{"proposals":[...]}`, 1 to N objects, validated strictly; duplicates and extra keys are rejected). An
   optimizer without `propose_many` gives one proposal. Each distinct, changing proposal is saved as a control
   idea and emits `hypothesis-discovered`.
2. Candidates are the new ideas, then ideas never yet screened, then `revive_candidates()`, then ideas not
   screened for three cycles, capped at `max_candidates`.
3. `DevelopmentScorer(idea, row)` applies the idea's configuration through `wheel._answers`, the same decision
   path, answer cache, byte ceiling and request ceiling the flywheel uses, and reads the decision answer
   (no fitted head). It refuses any item that is not a development row. Cache hits cost no request.
4. `screen()` runs successive halving over the screening items (development minus reserve).
5. Confirmation (`confirm()`): only the top `finalists` go to the reserve, so Holm stays small. A finalist
   carried over as positive-but-not-significant takes a slot, and at least one slot goes to a fresh finalist.
   The paired exact sign test uses every reserve item both the finalist and the incumbent have been scored on,
   across rounds (read from the ledger; only missing pairs are scored). Cleared means net gain above zero and
   the Holm-adjusted p below `alpha`. Positive but not significant is kept as `advanced` and re-confirmed next
   round; no gain is `screened_out`; nothing is deleted.
6. The best cleared finalist gets the usual fitted trial (`improve`), and the selection policy still has to
   accept it. In `ControlScheduler` the trial joins the cycle's best-eligible promotion (`promote_trial`); in the
   stage path `improve` promotes directly. Only then is the verdict `promoted`, with gained, lost and p. The
   previous incumbent stays in the ledger.

The reserve rule: an item is in the reserve when a hash of the task fingerprint and its id falls under
`reserve_fraction`. It does not depend on labels, order or when the item arrives, so later items join the
reserve without ever having been screened, and reserve items never enter a screen. Because looks accumulate,
a candidate is tested several times as the reserve grows. The test in `idea_screening_test.py` simulates this:
with no true difference, 6 looks of 20 new items gave a false-promotion rate of 3.5% (4.8% at 10 looks); a
true advantage of about 20 gained and 5 lost per 100 items cleared 64% of the time by round 3 and 97% by
round 6. This is repeated testing without an alpha-spending correction, so keep `alpha` at 0.05 or lower.

Protected items are never scored: the scorer accepts development rows only, and the protected set is
validated disjoint from development. Reserve outcomes are hidden from the optimizer.

Budget: `max_screen_calls` bounds scorer calls per round, further capped by the wheel's remaining request
ceiling minus the room the fitted trial needs. If a stage or the confirmation would exceed it, the round is
truncated and promotes nothing (`control-trial-deferred`, reason `session request budget`). Exhausting the
ceiling mid-screen also truncates.

Optimizer context: `prior_control_ideas` (newest 20, compact), `hypothesis_ledger` from
`summary_for_optimizer` (10 ideas, 5 fixed and 5 broke item ids each) and `stubborn_items` (20 ids). The
digest excludes reserve items. These are development item ids, so the run records
`evaluation_context_exposed=true` whenever screening is on.

Determinism: screening items are shuffled with the cycle number as the seed and the reserve is hash-defined,
so the same inputs and recorded responses repeat a round exactly.

## Still not built

- A shadow/ensemble portfolio of the top-k diverse ideas, whose disagreement routes items to review (Stage 3).
- Cross-run idea memory, so screened-out ideas are scored on later labels and revived (Stage 4). Until then
  `revive_candidates()` is wired but only fires if something else scores a screened-out idea.
