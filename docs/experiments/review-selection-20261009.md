# Which items to review: least-confident, random, or mixed

Task decision-flywheel-b0cf2975. The hypotheses were pre-registered in a task
comment before any run.

## Design

All arms use:
- Jev 1.13.0 as the decision model, chosen in
  `decision-model-comparison-20261009.md`.
- The 1,200-item extended corpus with the same seed.
- An empty starting rubric and the recorded gate configuration.
- gpt-4.1-mini as the optimizer, and the item text only (no title).

The arms differ only in which items are reviewed:
- **Review everything (reference):** this run stopped at cycle 1,031 on the
  decision-request cap I set too low (8,000). It is compared over cycles 401
  to 1,000.
- **Random 25%:** a seeded hash picks the items.
- **Least-confident 25%:** an item is reviewed when its confidence is at or
  below the 25th percentile of the previous 100 issued confidences. The first
  20 cycles are random.
- **Mixed:** least-confident 20%, plus a random audit of about 5% of all
  items.

Selection uses only the issued confidence, never the hidden label.

Jev's confidences take few distinct values, so many items tie at the
percentile cut, and "at or below" reviews all of them. The two
confidence-based arms therefore reviewed more than 25%: least-confident 33.5%
and mixed 35.7%, against random's 25.6%. Compare them with that in mind; the
per-label row below corrects for it.

Whole-system accuracy counts reviewed items as right. Learning means the
cyclotron's own accuracy on all items, evaluator-only.

## Results, cycles 401 to 1,200 (reference arm: 401 to 1,000)

| Arm | Labels (all cycles) | Labels 401+ | Learning accuracy | ECE | Whole-system accuracy | Mistakes that slipped through | Mistakes caught per 100 labels | Worst whole-system window after 200 | USD | Minutes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Review everything | 1,000 | 600 | 84.3% | 0.094 | 100% | 0 | 15.7 | 100% | 2.69 | 68 |
| Random 25% | 307 | 207 | **86.4%** | **0.079** | 89.6% | 83 | 12.6 | 83% | 0.28 | 18 |
| Least-confident 25% | 402 | 289 | 83.4% | 0.092 | 94.4% | 45 | **30.4** | 88% | 0.45 | 22 |
| Mixed (20% + 5% audit) | 428 | 310 | 85.4% | 0.095 | **95.1%** | **39** | 25.2 | 84% | 0.48 | 23 |

Per-window tables are in `analysis/selection-*.json`.

## Hypotheses

- **H1, least-confident's whole-system accuracy is at least 5 points above
  random's: not supported, narrowly.** The gap was 4.8 points, and
  least-confident also reviewed about 30% more items. Per label it is clearly
  better: it caught 30.4 mistakes per 100 labels against random's 12.6.
- **H2, least-confident's ECE is at least 0.03 worse than random's: not
  supported.** The gap was 0.013. Calibration did not collapse without an
  audit.
- **H3, mixed is within 2 points of least-confident on whole-system accuracy
  and within 0.02 of random on ECE: supported.** It scored 95.1% against
  94.4%, and ECE 0.095 against 0.079.
- **H4, least-confident learns at least as well as random: not supported.**
  Random learned best: 86.4% against 83.4%. Reviewing everything did not
  learn better either (84.3% over 401 to 1,000).
  - One reading is that review volume beyond about 25% does not raise this
    plateau.
  - Fewer labels also means fewer rubric rewrites. The arms ran 23, 7, 6 and
    8 optimizer calls, so the rubric churned less.

## Recommendation

**Mixed is the practical default.** It catches mistakes nearly as well as
least-confident: it had the fewest slipped (39) and the highest whole-system
accuracy (95.1%), at a sixth of review-everything's cost. Its random audit
keeps an unbiased sample for calibration and promotion checks.

**Random-only is the right choice when the goal is the model's own accuracy
rather than catching mistakes.**

Caveats:
- One run per arm, and window-to-window spread on this corpus is about ±5
  points.
- The realized rates differ because of tied confidences. The percentile cut
  should break ties at random so the rate holds at 25%; that is a follow-up.
- The reference arm is short by 200 cycles.

Fixtures (schema 2) are in `exports/selection-*-v2.json` under
`~/Documents/Codex/2026-10-09/editorial-study/`. The databases are compressed
after export.
