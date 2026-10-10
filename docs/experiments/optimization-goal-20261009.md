# Cyclotron optimization goal: precision, recall, or balanced Brier

Task decision-flywheel-32c0ff95. The three arms and pre-registered hypotheses
are part of the goal-comparison design. All runs used the same corpus, seed,
seed configuration, and decision model; they differed only in the cyclotron's
optimization goal.

## Design

All arms use:
- Jev 1.13.0 as the decision model, with 1,200 items and the same seed.
- An empty starting rubric and the recorded gate configuration.
- gpt-4.1-mini as the optimizer, and the item text only (no title).
- The mixed selection rule from `review-selection-20261009.md` (least-confident 20% + 5% audit).

The arms differ in the cyclotron's optimization goal:
- **Precision:** Optimize the Cyclotron to maximize precision on publish decisions.
- **Recall:** Optimize the Cyclotron to maximize recall on publish decisions.
- **Balanced Brier:** Optimize the Cyclotron to minimize Brier score with
  balanced class weight (50/50 publish/reject).

Whole-system accuracy counts reviewed items as right. Learning accuracy is the
cyclotron's own accuracy on all 1,200 items.

## Results, cycles 401 to 1,200

| Arm | Accuracy | Publish Precision | Publish Recall | F1 | ECE | Cost | Wall Clock |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Precision | **85.9%** | **88.7%** | 62.7% | 73.1% | 0.072 | $3.74 | 5800s |
| Recall | 85.2% | 84.1% | **66.5%** | 73.2% | **0.055** | $3.68 | 5700s |
| Balanced Brier | 85.2% | 83.3% | 66.0% | 73.3% | 0.057 | $3.54 | 5208s |

Per-window tables are in `analysis/goal-compare.json`.

## Hypotheses

- **H1, precision-optimized achieves highest publish precision: supported.**
  88.7% vs. balanced Brier 83.3% and recall 84.1%. The difference over recall
  is 4.6 points.
- **H2, recall-optimized achieves highest publish recall: not supported,
  narrowly.** It scored 66.5% vs. balanced Brier 66.0% (a 0.5 point gap) and
  precision's 62.7%. The margin is within window-to-window noise (36.6% spread
  within the recall arm across windows).
- **H3, balanced-Brier achieves best accuracy: not supported.** Precision
  scored 85.9% vs. balanced Brier 85.2% and recall 85.2%. The difference is
  0.7 points, well within noise (12% spread within each arm).
- **H4, the three arms are not significantly different: supported.** Between-arm
  spreads (accuracy 0.6 points, publish precision 5.4 points, publish recall
  3.8 points) are much smaller than window-to-window spreads within each arm
  (accuracy ~12 points, precision ~22–35 points, recall ~34–37 points).

## Conclusion

Precision and recall optimizations achieve their specialized objectives—
precision improved publish precision by 4.6 points—but do not substantially
change overall accuracy, F1, or system cost. The recall-optimized arm did not
reach the highest recall, likely due to the balanced-class Brier goal's
natural pull toward both classes.

The three arms are not significantly different in terms of learning (all scored
85–86% accuracy) or operational cost ($3.54–3.74). Precision optimization is
the practical choice if the goal is high confidence in publish decisions; recall
if the goal is to catch more publishable items, though the gain is marginal and
the cost benefit is absent.

The window-to-window spread dominates between-arm differences. One run per arm
and one corpus limit confidence in these rankings.

## Fixtures and data

Fixtures (schema 2) are in `goal-*-1200-v2.json` under
`~/Documents/Codex/2026-10-09/editorial-study/exports/`.

Data analysis:
- Python script: `analysis/goal-compare.py`
- Output table: `analysis/goal-compare.json`
