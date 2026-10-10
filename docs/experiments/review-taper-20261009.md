# When to review fewer, and how to taper

Task decision-flywheel-8ee62604. The rule and the hypotheses were
pre-registered in a task comment before any run.

## Design

All runs use:
- Jev 1.13.0 as the decision model, on the 1,200-item corpus with the same
  seed.
- An empty starting rubric and the recorded gate configuration.
- gpt-4.1-mini as the optimizer.

The arms:
- **Metric taper (`MetricTaperPolicy`).** It reviews everything at first. At
  each 100-cycle boundary it reads only reviewed labels:
  - the ECE of the last 200 reviewed items;
  - the accuracy of reviewed items in that window with confidence of 0.80 or
    more (at least 10 of them).

  A window passes when ECE is below 0.05 and that accuracy is at least 0.90.
  After two passing windows in a row the rate steps down one level (100%,
  50%, 35%, 25%, 15%); one failing window steps it back up. Below full review,
  items are chosen at random, so every review is also an audit. The rate log
  is in the run's `results.json`.
- **Time-based taper:** review the first 100 items, then 50% at random.
- **Fixed random 25%:** from `review-selection-20261009.md`.
- **Review everything:** also from that note; it stopped at cycle 1,031.

Whole-system accuracy counts reviewed items as right.

## Results

| Arm | Labels | Share of review-everything's | Whole-system accuracy, 401–1,200 | Worst whole-system window after 200 | Learning accuracy, 401–1,200 | ECE, 401–1,200 | Mistakes slipped, 401–1,200 | USD | Minutes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Review everything (to 1,000) | 1,000 | 100% | 100% | 100% | 84.3% (401–1,000) | 0.094 | 0 | 2.69 | 68 |
| Metric taper | 1,094 | 91% | 98.5% | 91% | 86.9% | 0.062 | 12 | 3.08 | 84 |
| Time-based taper | 672 | 56% | 94.3% | 88% | 86.8% | 0.069 | 46 | 1.29 | 47 |
| Fixed random 25% | 307 | 26% | 89.6% | 83% | 86.4% | 0.079 | 83 | 0.28 | 18 |

The metric taper's rate log:
- Cycles 1 to 400: every window failed.
  - The ECE of the last 200 reviews was 0.050 to 0.057, just above the 0.05
    bar.
  - Approved accuracy was 84% to 95%.
- Windows ending at 500, 700 and 800 passed; the one ending at 600 failed
  (ECE 0.079).
- After two passes in a row (700, 800) the rate stepped down to 50% at
  cycle 800.
- At cycle 1,000 approved accuracy was 86%, so the window failed and the rate
  returned to 100%. It stayed there to the end.

## Hypotheses

- **H1, the metric taper uses at most 60% of review-everything's labels: not
  supported.** It used 91% (1,094 labels).
- **H2, whole-system accuracy within 3 points of review-everything's and at
  least 3 above fixed random 25%'s: supported.** It scored 98.5%, against 100%
  and 89.6%. This mostly reflects that it reviewed nearly everything.
- **H3, worst window after cycle 200 at least the time-based taper's:
  supported.** It scored 91% against 88%.

## Conclusion

As specified, the rule almost never lets go on this corpus:
- An ECE bar of 0.05 over 200 reviews is about the size of the sampling noise
  in that estimate. The cyclotron's calibration sat around 0.05 to 0.08 for
  most of the run.
- Approved accuracy at 0.80 fluctuated around the 0.90 target.

The time-based taper got 94.3% whole-system accuracy with 56% of the labels.
Fixed random 25% got 89.6% with 26%. The selection note's mixed rule got
95.1% with 36%. Learning accuracy was the same within noise across all of
them (84% to 87%). On this corpus, fewer labels did not slow learning.

Recommendation:
- Do not ship this rule with these parameters.
- Next variant (not run): judge each window by whether the auto-approve
  promise held, meaning approved accuracy at least the confidence it claimed,
  minus a sampling margin. Drop the ECE bar or loosen it to about 0.08. Select
  by the mixed rule below full review.
- Pre-register a 400-cycle check of that variant before any longer run.
