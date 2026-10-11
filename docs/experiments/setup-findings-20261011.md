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

## Review schedules from a blank start

Blank start, trigger 6, accuracy objective, 287 stories (windows of 70), one run
each. Accuracy is scored against the corpus's reference labels. An earlier version
of this note scored against the labels the run recorded, which exist only for
reviewed stories, and so reported accuracies that were far too low; that was
wrong.

| Schedule | Labels revealed | Accuracy by window | Publish recall by window | Publish precision by window | Optimizer calls | Jev calls |
|---|---:|---|---|---|---:|---:|
| `all` (replicates-cold6/rep-1) | 287 | 77.1, 94.3, 92.9, 95.7 | 82, 86, 67, 73 | 39, 86, 100, 100 | 13 | 2,320 |
| `first_50_all_then_half` | 169 | 78.6, 97.1, 92.9, 94.3 | 91, 86, 67, 64 | 42, 100, 100, 100 | 6 | 911 |
| `publish_priority_taper` | 113 | 78.6, 95.7, 97.1, 91.4 | 91, 93, 93, 73 | 42, 87, 93, 73 | 5 | 644 |
| `reject_half` | 46 | 45.7, 61.4, 61.4, 41.4 | 100, 93, 93, 91 | 22, 33, 35, 20 | 0 | 287 |
| `casual_ten_percent` | 29 | 45.7, 60.0, 60.0, 41.4 | 100, 93, 93, 82 | 22, 32, 34, 19 | 0 | 287 |

Always answering the most common label: 84.3, 80.0, 78.6, 84.3 in those windows.

The two schedules that start selective never proposed a rubric: the trigger counts
label transitions among reviewed stories and never fired. They stayed at the blank
rubric's default behavior (publish for most stories). The two onboarding schedules
learned a rubric by story 21 and held accuracy on 59 and 39 percent of the labels.
Runs: `optimization/rec-onboard50`, `rec-taper`, `rec-reject-half`, `rec-casual10`.
