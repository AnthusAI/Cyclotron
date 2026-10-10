# Editorial example past 1,000 cycles (attempt 4, 1,124 cycles)

Task decision-flywheel-8f3a052a. The question: where does accuracy level off,
how many corrections it takes to get there, and what a long run costs.

## Setup

- Corpus: the 1,200-item Wikinews editorial corpus with simulated semantic
  labels (sha256 `bba0fa49…`; its first 400 items are the 400-cycle
  recording's corpus, byte for byte).
- Decision model and optimizer: gpt-4.1-mini. The run starts from an empty
  rubric, every item is reviewed (feedback mode `all`), and a rubric step runs
  every 20 label transitions.
- The trained model on the confidences (the ML head) is refit every 200
  cycles.
- Prices are list prices for gpt-4.1-mini ($0.40 input, $0.10 cached input,
  $1.60 output per million tokens) applied to the usage the provider reported.
- Accuracy is scored against the frozen reference labels, evaluator-only.

The run stopped at cycle 1,125 of 1,200. A rubric step failed with
`sqlite3.OperationalError`: a read-only progress query held the store's
rollback-journal lock past SQLite's five-second default. That is fixed on
develop: the store now opens in WAL mode with a 30-second busy timeout
(decision-flywheel-189dddde). All 1,124 completed cycles are intact. No
`results.json` was written, so this note is computed from the run's event log
and there is no exported fixture. The site keeps the 400-cycle recording.

Three earlier attempts did not finish:
- **Attempt 1** was stopped at 600 cycles because the store was growing toward
  3.5 GB.
- **Attempt 2** died at cycle 140 on a single connection error; the study now
  has opt-in transport retries.
- **Attempt 3** died at cycle 510. The optimizer proposed changes outside the
  rubric stage, and the step raised. Such a proposal is now recorded as
  `invalid-proposal` and dropped.

## Per 100 cycles

In this table, "cyclotron" means its own labels and confidence, and
"decision model" means the raw gpt-4.1-mini labels and stated confidence. ECE
is the calibration error (10 bins).

The 0.80 rule auto-approves when the cyclotron's confidence is at least 0.80
and sends everything else to review. "Whole system" counts reviewed items as
right.

| Cycles | Accuracy | Brier | ECE | Mean conf. | DM accuracy | DM ECE | DM mean conf. | 0.80 approved / right | Whole system | USD | Minutes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1–100 | 57% | 0.726 | 0.304 | 0.846 | 57% | 0.379 | 0.949 | 72 / 39 | 67 | 0.13 | 4.0 |
| 101–200 | 85% | 0.240 | 0.038 | 0.812 | 85% | 0.108 | 0.952 | 54 / 51 | 97 | 0.18 | 4.9 |
| 201–300 | 84% | 0.285 | 0.099 | 0.892 | 84% | 0.113 | 0.948 | 85 / 70 | 85 | 0.38 | 9.5 |
| 301–400 | 66% | 0.405 | 0.062 | 0.703 | 62% | 0.324 | 0.944 | 43 / 35 | 92 | 0.40 | 12.9 |
| 401–500 | 82% | 0.237 | 0.095 | 0.807 | 82% | 0.146 | 0.966 | 60 / 57 | 97 | 0.36 | 9.9 |
| 501–600 | 81% | 0.259 | 0.053 | 0.804 | 81% | 0.157 | 0.961 | 58 / 54 | 96 | 0.45 | 13.0 |
| 601–700 | 84% | 0.252 | 0.055 | 0.816 | 84% | 0.131 | 0.965 | 64 / 58 | 94 | 0.42 | 9.6 |
| 701–800 | 83% | 0.284 | 0.064 | 0.828 | 83% | 0.141 | 0.971 | 64 / 55 | 91 | 0.61 | 20.0 |
| 801–900 | 83% | 0.287 | 0.051 | 0.834 | 83% | 0.150 | 0.975 | 74 / 62 | 88 | 0.74 | 22.9 |
| 901–1000 | 82% | 0.298 | 0.055 | 0.837 | 82% | 0.164 | 0.985 | 78 / 63 | 85 | 0.99 | 25.1 |
| 1001–1100 | 79% | 0.332 | 0.047 | 0.837 | 79% | 0.195 | 0.985 | 83 / 67 | 84 | 0.45 | 13.1 |
| 1101–1124 | 21/24 | 0.220 | 0.039 | 0.836 | 21/24 | 0.114 | 0.989 | 19 / 17 | 22/24 | 0.56 | 3.2 |

Totals: $5.67 at list prices for 148 minutes of wall-clock time.
- Decision model: $2.74 over 7,038 calls (6.16 M input tokens, 0.18 M output).
- Optimizer: $2.93 over 25 calls (7.26 M input tokens, 0.01 M output).

The optimizer's share grows through the run because each call carries a longer
history: about 290 k input tokens per call on average.

## Findings

1. **Accuracy levels off at about 82% from cycle 400.** From cycle 401 to
   1,100, every window falls between 79% and 84%. After the first 200 reviews,
   about 900 more reviews did not raise it.
2. **No rubric proposal was ever promoted.** All 24 rubric steps (cycles 45 to
   1,081) were activated as provisional, working rubrics. None reached
   `promoted`, and no proposal was dropped as invalid. The step's reason is
   "working rubric refined with a decaying recency preference; evaluation
   remains exploratory". Why the gate never promotes is the next experiment.
3. **The ML head was promoted four times:** at 200, 400, 600 and 800, each for
   lower balanced Brier with no per-class recall regression. At 1,000 no
   candidate passed the safeguards.
4. **The cyclotron's label differed from the decision model's on 38 cycles,
   all between 324 and 367, and nowhere else in 1,124.** Those cycles ran
   under the very short rubric adopted at cycle 320 ("Include items with a
   meaningful constructive or public-benefit outcome; reject harm-dominant or
   routine items.").
   - The decision model said "publish" on 39 of those 47 items and was right
     on 25 (53%).
   - On the 38 items where the head overrode it, it moved the label to
     "reject" and was right on 21, against the decision model's 17.
   - The 66% dip in cycles 301 to 400 is mostly the decision model under that
     rubric. The head recovered four points (62% to 66%).
   - Cycles 368 to 400, under the next rubric, were also weak: 22 of 33.
   - A cancelled replicate of the first 412 cycles (same settings, fresh model
     replies) also dipped in this window, to 70%, without any label
     differences. So part of the dip is these items, not only the rubric.
5. **Calibration is the cyclotron's lasting gain.** Its ECE stays at 0.04 to
   0.10 from cycle 101 onward, while the raw decision model's rises from 0.11
   to 0.20. The model's mean stated confidence climbs from 0.95 to 0.99 as its
   accuracy stays at 82%.
6. **The fixed 0.80 rule gets worse later in the run.** The cyclotron's mean
   confidence drifts up from 0.80 to 0.84 at the same accuracy. The rule
   therefore approves more items (58 to 83 per window) at lower precision (93%
   down to 81%). Whole-system accuracy falls from 96 to 97% in cycles 401 to
   600 to 84% in cycles 1,001 to 1,100. A threshold fixed once does not hold
   its promise over a long run.

For comparison, the 400-cycle recording ended at 82% in its last 100. Attempts
1 and 3 had 78 to 80% in cycles 301 to 400, and 89 to 93% in single later
windows, so window-to-window spread between runs is about ±5 points.

## What follows

- The plateau is about 82% with gpt-4.1-mini on this corpus. The review-rate,
  taper and goal experiments (decision-flywheel tasks 5 to 7) should treat a
  stronger decision model as a variable.
- The rubric gate never promotes. Finding out why costs nothing and is next.

Data (local, not in the repository), all under
`~/Documents/Codex/2026-10-09/editorial-study/`:
- `run-1200-attempt4-db-locked-at-1125/` (event log, protocol, run log)
- `attempt4-summary.txt`, `attempt4-labels-promotions.txt`
- `analysis/attempt4-windows.json`, plus the scripts beside it
- the earlier attempts' data, gzipped
- the cancelled 412-cycle replicate: `run-500-cancelled/`
