# Setup findings (2026-10-11)

Jev-only measurements and replicate batches behind `docs/using-cyclotron.md`.
Decision model Jev 1.13.0; optimizer gpt-6-luna. Corpus and run folders are
local study artifacts (`~/Documents/Codex/2026-10-09/editorial-study/`).

## Titles

Held-out 200 stories of the original 400-story corpus, rubric in criteria.
Adding the story title to the text: starting rubric 74.5 to 77.5 percent
(gained 7, lost 1, p 0.07); true labeling rubric 84.5 to 85.0 (gained 10,
lost 9). So titles helped the weak rubric by about 3 points and did nothing
measurable for the strong one.

## Where the rubric goes in the Jev request

Same 200 stories, true labeling rubric: 77.5 percent when sent in the state,
84.5 percent in the criteria of the decision question (gained 20, lost 6,
exact sign test p 0.009). The learned rubric: 76.0 in state, 78.0 in criteria
(not significant). The engine now sends the rubric in criteria.

## Jev's default bias

With the original curated starting rubric Jev said publish for 17 to 20
percent of stories where the original labels were 40.5 percent publish. With
no rubric Jev said publish for 52 percent (original labels) and 65 percent
(relabeled corpus).

## Model prices

gpt-6-luna per LiteLLM's price table, standard tier: 0.10 USD per million
input tokens, 0.50 per million output; a request above 272,000 input tokens is
billed whole at 0.20 and 0.75. Jev: 0.042 USD per million input tokens.

## Batch costs (287-story relabeled corpus, one run per replicate)

| Batch | Jev calls | gpt-6-luna requests | Approximate cost |
|---|---|---|---|
| Six replicates, trigger 20 | 6,228 | 24 | about 0.47 USD (Jev 0.24, gpt-6-luna 0.22) |
| Six replicates, trigger 6 | 13,474 | 78 | about 1.15 USD (Jev 0.54, gpt-6-luna 0.62) |

A single trigger-6 run: about 2,100 to 2,300 Jev calls and 13 optimizer
requests, about 0.10 USD of gpt-6-luna.

## Windows are noisy

A window of 70 stories carries about plus or minus 7 points at 90 percent
accuracy (Wilson interval). A window holds 11 to 15 publish stories, so one
story moves publish recall by 7 to 9 points.

## Recall objective versus accuracy objective (blank start, trigger 6)

Publish recall / precision by window of 70, one run each:

| Run | W1 | W2 | W3 | W4 |
|---|---|---|---|---|
| Recall objective | 0.91 / 0.40 | 0.86 / 0.92 | 0.80 / 0.92 | 0.55 / 0.75 |
| Accuracy objective, replicate 1 | 0.82 / 0.39 | 0.86 / 0.86 | 0.67 / 1.00 | 0.73 / 1.00 |
| Accuracy objective, replicate 4 | 0.82 / 0.39 | 0.71 / 0.91 | 0.87 / 1.00 | 0.73 / 1.00 |

No consistent recall gain from the recall objective at this scale, and its
final window recall was lower. Windows are too small to tell objectives apart;
a larger corpus is needed to demonstrate an objective.

## Curated versus blank starting rubric (trigger 6, accuracy objective)

Curated start, cyclotron accuracy by window: 91.4, 94.3, 94.3, 92.9 percent
(first rubric change at cycle 121; the curated rubric was in place from the
start). Blank start, six replicates: first window 73 to 77, final window 94.3
to 95.7. The curated start wins the first window and ends in the same range.

## Selective review from a blank start

Blank start, trigger 6, accuracy objective, 287 stories, one run each:

| Policy | Labels revealed | Optimizer calls | Accuracy by window of 70 | Majority baseline by window |
|---|---:|---:|---|---|
| `reject_half` (every predicted publish, half of predicted rejects) | 46 | 0 | 14.3, 18.6, 14.3, 14.3 | 85.7, 81.4, 84.3, 84.3 |
| `casual_ten_percent` | 29 | 0 | 4.3, 2.9, 4.3, 1.4 | 87.1, 92.9, 90.0, 90.0 |

No rubric was ever proposed: the first-rubric trigger counts label transitions
among reviewed stories and never fired. Jev with a blank rubric publishes for
almost every story, so accuracy stayed far below always answering "reject".
Cost: Jev 287 calls each, no optimizer calls, about 1.5 cents each. Runs:
`optimization/rec-reject-half` and `optimization/rec-casual10` (not published).
