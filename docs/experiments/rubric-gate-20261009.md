# Why no rubric proposal was promoted, and a gate that lets one be

Task decision-flywheel-b1b1827a. The hypotheses were pre-registered in the
task before either replay ran.

## Offline: attempt 4's 24 rubric steps

Attempt 4 is the 1,124-cycle run in `editorial-long-run-20261009.md`.

**Every rubric step took the provisional branch.** In `staged_optimization`
that branch runs while `EvaluationPolicy.recency_allowance(development
counts)` is above zero, which lasts until both classes have
`recency_decay_per_class` = 20 development labels. The study's plan drew
development as a fixed 20 per class from the whole corpus, so the set only
completes at the corpus's last item. Development counts at each attempt:

| Cycle | Development (publish / reject) | Allowance | Incumbent balanced Brier | Candidate balanced Brier | Activated |
| ---: | --- | ---: | ---: | ---: | --- |
| 94 | 1 / 5 | 1.9 | 0.130 | 0.118 | yes |
| 185 | 5 / 6 | 1.5 | 0.173 | 0.248 | yes |
| 238 | 6 / 7 | 1.4 | 0.195 | 0.420 | yes |
| 320 | 8 / 11 | 1.2 | 0.366 | 0.455 | yes |
| 561 | 10 / 15 | 1.0 | 0.551 | 0.483 | yes |
| 901 | 12 / 20 | 0.8 | 0.514 | 0.387 | yes |
| 1,081 | 19 / 22 | 0.1 | 0.463 | 0.462 | yes |

(The table shows 7 of the 24 steps; all are in
`analysis/attempt4-rubric-versions.json`.)

The promotion gate (`DecisionFlywheel.improve`) was never called. The reason
is the minimum evidence, not the promotion margin.

**The provisional margin was also no filter.** It starts at 2.0 balanced-Brier
units, which is the whole range, so every candidate was activated, including
clear regressions on development:
- Cycle 238: balanced Brier 0.195 to 0.420.
- Cycle 320: 0.366 to 0.455, and development accuracy 79% to 58%.

The rubric adopted at 320 was 110 characters long: "Include items with a
meaningful constructive or public-benefit outcome; reject harm-dominant or
routine items." That is the task's own question, returned as the rubric.
While it was active (cycles 321 to 367) the decision model was right on 53% of
items.

**The proposals were otherwise sensible.** The 25 activations held 13
distinct texts; the optimizer often returned the same rubric again. Typical
ones:
- Cycle 45 (1,376 characters): "Label as 'publish' those news articles that
  describe situations where a constructive or beneficial outcome is
  demonstrated or strongly implied. Examples … technological achievements …
  international cooperation … legal or economic rulings with fair outcomes …"
- Cycle 367 (637 characters): "Include items where a concrete constructive or
  public-benefit outcome or advancement is central, such as improved rights or
  access, scientific or technological progress … Exclude items dominated by
  harm, conflict, or controversy without established constructive effects, as
  well as routine, procedural, or informational reports …"

**Development measurements tracked operational accuracy poorly.** On 13 to 41
development items they were noisy, and they sat well below operational
accuracy: development accuracy 57% to 77% from cycle 413 on, against about 82% operational.

## Configuration G and the replay

Configuration G:
- `--development-rate 0.25`: development is a label-independent hash share of
  the stream, the engine's default rate. 100 of the 400 items get the
  development role.
- `--provisional-allowance 0.05`.
- Recency decay unchanged at 20 per class.

Everything else matches the recorded runs: the 400-item corpus, the same seed,
gpt-4.1-mini for both decisions and the optimizer, an empty starting rubric,
and every item reviewed. Exact decision requests already answered in the
412-cycle replicate were served from its answers (`--seed-answers-from`).

There were two replicates, G1 and G2. The baseline is the five recorded runs
with the current configuration.

| Run | Cycles 1–100 | 101–200 | 201–300 | 301–400 | Mean 201–400 | ECE 301–400 | 0.80 rule whole-system right, 301–400 | Rubric promoted |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 400-cycle recording (v1) | 52% | 89% | 84% | 82% | 83% | 0.017 | 89 | never |
| Attempt 1 | 58% | 86% | 78% | 80% | 79% | | | never |
| Attempt 3 | 56% | 86% | 78% | 78% | 78% | | | never |
| Attempt 4 | 57% | 85% | 84% | 66% | 75% | 0.062 | 92 | never |
| Replicate (cancelled 500 run) | 51% | 85% | 80% | 70% | 75% | 0.095 | 89 | never |
| **G1** | 55% | 83% | 77% | 75% | **76%** | 0.177 | 78 | at 367 |
| **G2** | 58% | 85% | 80% | 78% | **79%** | 0.100 | 78 | at 281 |

The baseline averages 78% over cycles 201 to 400, with a range of 75% to 83%.

**Cost:** G1 $0.77 over 19 minutes (1,439 decision calls billed, 7 optimizer
calls). G2 $1.01 over 23 minutes (1,719 decision calls, 7 optimizer calls).
The seeded answers served the rest.

## Hypotheses

- **H1, the promotion gate is reached: supported.** In both replicates the
  development set reached 20 per class by cycle 238. From then on, rubric
  steps went through `improve`:
  - G1 rejected three candidates on per-class recall safeguards and promoted
    one at cycle 367, on development balanced Brier 0.345 to 0.315.
  - G2 promoted one at 281 and rejected three.
- **H2, no accepted regressions: supported.** No provisional rubric was
  activated with development balanced Brier more than 0.05 worse. The largest
  accepted increase was 0.007. G1 rejected one candidate at 185 (0.213 to
  0.312), and G2 rejected one at 140 (0.245 to 0.283).
- **H3, accuracy at least 81% over cycles 201 to 400 in each replicate: not
  supported.** G1 reached 76% and G2 79%, against the baseline's 78% mean.
  Promoting rubrics through the gate did not raise accuracy within 400 cycles.
- **Secondary, ECE in cycles 301 to 400 no worse than the baseline by more
  than 0.03: not supported.** G1 had 0.177 and G2 0.100, against 0.017 to
  0.095. The 0.80 rule's whole-system right in cycles 301 to 400 was 78 in
  both replicates, against 89 to 92 in the three baselines that have it.

## Side effect: the head was replaced by raw probabilities

In G1, the classifier step at cycle 200 promoted the `raw_decision` feature
set, which is the decision model's own probabilities with no head. It scored
balanced Brier 0.177 against the head's 0.218 on 54 development items. From
cycle 201 to 367 the cyclotron therefore reported the decision model's raw
confidence, with mean 0.964 and ECE 0.19, until the rubric promotion at 367
refit a head.

A development set four times larger let a selection that looks good on
development but is badly calibrated in operation win. With a quarter of the
reviews held out for development, the head also trains on fewer labels.

## Conclusion

The rubric gate never promoted because the study's development set completes
only at the end of the corpus, and the provisional margin accepts anything.

Configuration G fixes both. Rubrics are then promoted (once per run) and
regressions are blocked. Accuracy did not improve (76% and 79% against 78%),
and calibration got worse. On this corpus with gpt-4.1-mini, the rubric is not
the lever that lifts the plateau.

Recommendation:
- Keep the narrow provisional allowance (0.05) as the safer default for
  review-everything runs.
- Do not adopt the 25% development share for this study without a head
  safeguard: `raw_decision` should not be selected over a fitted head unless
  it is also calibrated on development.
- Go on to the decision-model comparison, where the hypothesis is that the
  model, not the review volume, sets the plateau.

Data is under `~/Documents/Codex/2026-10-09/editorial-study/`:
- `gate/g1/`, `gate/g2/`: databases compressed after export.
- `exports/gate-g1-400-v2.json`, `exports/gate-g2-400-v2.json`: schema 2
  fixtures.
- `analysis/`: scripts and tables.
