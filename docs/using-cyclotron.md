# Using Cyclotron: a setup and usage guide

This guide is for a technical person who will set up a cyclotron for a decision that recurs. It is built from what
we learned running the engine on an editorial task (publish or reject a news story). It is candid about what is
settled and what is not.

The main point: the first setup takes judgment. Once it is right, the loop steers itself. Set up wrong, it steers
itself just as confidently in the wrong direction. Most of this guide is the list of small decisions that make the
difference, and the concrete failure each one produced in our runs.

Words follow [the glossary](glossary.md): item, cyclotron, classifier, decision model, ML model, LLM optimizer,
decision, confidence, reviewer, review, label, correction, agreement, explanation, feedback, cycle, proposal,
candidate, promoted, dropped, version, held-out.

Numbers in this guide carry a "Source:" line. Where we do not know, the text says "we have not measured this".
One run is one draw. Do not read a single number as a promise.

## 1. What the loop does

A cyclotron makes one kind of decision about one kind of item, again and again.

1. The decision model answers. For each item it returns a decision and a confidence. In our runs the decision model
   was Jev 1.13.0.
2. The ML model is fitted. A small model is trained on the decision model's outputs (its answers to the main
   question and to any extra classifier questions) against the labels. It produces the final decision and a
   calibrated confidence.
3. Reviewers check some decisions. A review records a label. A label that differs from the decision is a
   correction. A label that matches is an agreement. An explanation may come with it.
4. The cyclotron keeps every label and explanation and learns from them. It refits the ML model on a schedule.
5. The LLM optimizer proposes changes. Its proposals are mainly changes to the rubric. Code checks each
   proposal as a candidate, and a candidate is either promoted or dropped.

The reviewers' corrections are the signal. Everything the loop learns, it learns from them. If the corrections are
wrong or inconsistent, the loop learns that.

Related documents:

- [README](../README.md): the core library, the classifier request, and the optimizer's three controls.
- [docs/embedding.md](embedding.md): putting a cyclotron inside an application.
- [docs/hypothesis-portfolio.md](hypothesis-portfolio.md): keeping many ideas and screening them (experimental).
- [docs/replicates.md](replicates.md): running the same setup several times to see the spread.
- [docs/recordings.md](recordings.md): keeping and publishing recorded runs.
- [docs/experiments/](experiments/): the experiment notes that the numbers below come from.

## 2. Before you start: the decisions that come first

Each entry gives what it is, the default, what we saw, how it fails, and what to do.

### 2.1 Define the decision and the labels

- What it is: the question the cyclotron answers, the label set, and which label is the positive class.
- Default: none. You choose. The engine's selection objective is accuracy, which does not need a positive class.
  Precision, recall and F1 do.
- What we saw: in the editorial task, publish is the rare class. On the clean-label corpus 17.8 percent of stories
  are publish. A constant answer of reject is right 79 to 84 percent of the time, depending on the window.
- How it fails: on a lopsided task, accuracy rewards a cyclotron that says reject almost every time. A high accuracy
  number can hide a decision that never finds the rare class.
- What to do: write the question as one sentence a reviewer can answer. Name the positive class. Compute the
  majority-class baseline before you run anything. Report every result next to it.

Source: `~/Documents/Codex/2026-10-09/editorial-study/optimization/replicates-baseline/replicates-summary.md`
(majority by window 84.3 / 80.0 / 78.6 / 84.3 percent); experiment findings, "Clean labels and the v3 recording".

### 2.2 What the decision model gets to see

- What it is: the fields of the item that go into the decision request (`--item-fields` in the study script).
- Default: `text` only.
- What we saw: Jev never saw story titles in our early runs, while the person who wrote the labels did. Adding
  titles changed the starting rubric's accuracy by about 3 points and the true rubric's by 0. An earlier test with
  gpt-4.1-mini and the labeling rubric gave 89.0 percent agreement on text alone and 95.5 percent with title and
  text, so the effect of the title depends on the model and the rubric. With Jev and the loop, showing the title
  did not help: 77 percent over cycles 201 to 400 with the title, against 79.5 percent without.
- How it fails: a gap between what the reviewer saw and what the decision model sees puts a ceiling on accuracy
  that no rubric can lift. The loop then spends its optimizer calls trying to teach the decision model things it
  cannot see.
- What to do: show the decision model what the reviewer used. If the reviewer used the title, include it. Then
  check on a sample whether it changed anything. Do not assume it did.

Source: `docs/experiments/setup-findings-20261011.md` (Jev title numbers: about 3 points for the starting rubric, 0.5 and
not significant for the true rubric); `docs/experiments/decision-model-comparison-20261009.md` (ceiling check and title follow-up).

### 2.3 Where the label comes from, and how consistent it is

- What it is: who or what produces the labels the loop learns from.
- Default: none. The engine trusts the labels it is given.
- What we saw: our first editorial corpus was labeled by a single labeling model that over-published against its own
  written rubric. An audit corrected 5 of 10 sampled publish labels, all publish to reject, and 0 of 10 reject
  labels. Best achievable accuracy was capped near 85 percent until the labels were fixed. For the relabel, three
  Haiku passes agreed at kappa 0.07 to 0.24 and gave nonsense rationales (boilerplate). Two blind Sonnet passes
  under an explicit written policy agreed at kappa 0.90 on 291 of 301 stories. The 10 splits were left out. The
  policy line "an action actually taken counts, a promise or plan does not" decided the corpus. Of 150 old publish
  labels, 88 became reject and 53 stayed publish; 148 of 151 old rejects stayed reject.
- How it fails: the loop optimizes whatever the labels say, including their errors. Noisy labels cap accuracy. Biased
  labels are learned as if they were truth. Label agreement that is low is a sign that the question is not yet
  well defined, not a sign that the labelers are bad.
- What to do: write the policy down in plain sentences before labeling. Label a sample twice, independently, and
  measure agreement. If agreement is low, fix the policy, not the labelers. Audit a sample of each class by hand.
  Keep the splits out of the corpus instead of resolving them by coin flip.

Source: experiment findings, "Clean labels and the v3 recording" and "Why the earlier example was flat". The label
audit and kappa numbers are from the relabel work under `~/Documents/Codex/2026-10-09/editorial-study/relabel/`.

### 2.4 The bootstrap seed set

- What it is: a small set of labeled items the run starts with, before it has seen any stream.
- Default in the study script: at least 12 items and at least 6 labels per class, or the run refuses to start. The
  first 8 are training items and the next 4 are development items.
- What we saw: the engine's own check is the floor, not the target. With the rare class at 17.8 percent, a seed of
  12 holds few positives. In our runs we arranged the seed so the first 12 held 4 positives, since the first 8 train and the
  next 4 are the development items. We did not run a seed without that arrangement.
- How it fails: with too few positives in the seed, the ML model and the first rubric proposal see almost only
  the majority class. We have not measured how much a larger seed helps.
- What to do: put at least 6 labels per class in the seed, and place positives among the first 12 as we did (a judgment
  from how the first 12 are used, not a measured requirement). Pick the seed by class, not by stream order.

Source: `scripts/run_editorial_prequential_study.py` (the bootstrap check and the `initial_training` and
`initial_development` split). The positives-in-the-first-12 rule is from the owner's brief.

### 2.5 Where the rubric lives

- What it is: the criteria text the decision model reads.
- Default: for Jev, the adapter sends the rubric in the criteria of the decision question's first label. Jev has no
  `state.rubric`. Earlier runs put the rubric in state.
- What we saw: on the same 200 stories, the true rubric scored 84.5 percent in the criteria and 77.5 percent in
  state.
- How it fails: a rubric in the wrong place is partly ignored. The loop then measures a weak rubric and blames the
  text, not the placement.
- What to do: use the adapter for your decision model and check where it puts the rubric. If you write your own
  adapter, test the same rubric in two places on a fixed sample before you trust any result.

Source: README, "The classifier at the center of the flywheel" (Jev sends the rubric as the criterion of the first
label). The 84.5 and 77.5 percent figures are in `docs/experiments/setup-findings-20261011.md`.

## 3. The starting point

You have three choices: start blank, start from a curated rubric, or start from a custom rubric you wrote.
A fourth is a way of thinking, not a setting: let the corrections steer.

Evidence on the clean-label corpus (291 stories, Jev, rubric in criteria):

| Starting rubric | Accuracy |
|---|---:|
| No rubric | 51.2% |
| Curated starting rubric | 90.0% |
| The written policy text itself as the rubric | 87.3% |
| Reject everything | 81.1% |

With no rubric Jev publishes 65 percent of the stories by default, which is why a blank start is poor.

Source: experiment findings, "Clean labels and the v3 recording" (Jev on the clean labels).

From a blank start the cyclotron climbed. In the recorded v3 run (blank rubric, accuracy objective, windows of 70)
cyclotron accuracy was 44.3, 84.3, 90.0 and 95.7 percent. In six replicates with the rubric trigger at 6, the
first window ran 72.9 to 77.1 percent and the final window 94.3 to 95.7. With the trigger at 20 the first window
ran 41.4 to 47.1 and the final window 88.6 to 95.7.

Trade-offs:

- Blank costs the early stories. The first window is poor until the first rubric arrives. It shows the loop
  learning, and it keeps your assumptions out of the rubric.
- A curated start gives a better first window. It also bakes in your assumptions and your biases. It can make the
  decision model too strict. On the old labels the curated rubric made Jev answer publish for 17 to 20 percent of
  stories when the editors wanted 40.
- A policy text used as the rubric did worse (87.3) than the curated rubric (90.0). Written policy is not the
  same as a good rubric.
- "Letting the corrections steer" means starting simple and letting the rubric change only when the corrections
  call for it. It works when corrections are consistent and fails when they are not (section 2.3).

Single-run comparisons, same corpus (287 stories), trigger 6, Jev 1.13.0, gpt-6-luna optimizer:

| Run (one each) | Cyclotron by window (70) | Decision model alone by window | First rubric activation (cycle) | Final window | Post-warm-up | Jev / optimizer requests |
|---|---|---|---:|---:|---:|---:|
| Blank start, accuracy objective (six replicates, range) | first window 72.9 to 77.1 | | 21 (all six) | median 95.7 (94.3 to 95.7) | min 93.3 | 2,088 to 2,320 / 13 |
| Curated start, accuracy objective (`rec-curated-t6`) | 91.4 / 94.3 / 94.3 / 92.9 | 91.4 / 94.3 / 92.9 / 95.7 | 121 | 92.9 | 93.8 | 2,324 / 13 |
| Blank start, recall objective (`rec-recall-t6`) | 77.1 / 95.7 / 94.3 / 90.0 | 74.3 / 90.0 / 94.3 / 88.6 | 21 | 90.0 | 93.3 | 1,899 / 13 |

Majority baseline by window in all of these: 84.3 / 80.0 / 78.6 / 84.3 percent.

What these two runs show, and do not show:

- The curated start gave a first window of 91.4 percent against 72.9 to 77.1 for a blank start. That is the
  better first window the trade-off above describes. After the first window the two were within the
  replicate spread. Its first rubric activation came at cycle 121, because with a good starting rubric there was
  little to change early. The curated run's final window (92.9) sat inside the blank-start range at trigger 20 and
  below the trigger 6 range (94.3 to 95.7). One run each: we cannot say the curated start is better or worse after
  the first window.
- The recall objective did not show a clear effect at this scale. Its first rubric came at cycle 21 like the other
  trigger 6 runs, and its final window accuracy (90.0) was below the six accuracy-objective replicates (94.3 to
  95.7). Publish recall by window was 91, 86, 80 and 55 percent, against 82, 86, 67 and 73 percent for accuracy
  replicate 1 and 82, 71, 87 and 73 for replicate 4. A window holds only 11 to 15 publish stories, so one story moves
  recall by 7 to 9 points; the runs cannot tell the objectives apart. Showing what an objective does needs a larger
  corpus (`docs/experiments/setup-findings-20261011.md`).

Source: `~/Documents/Codex/2026-10-09/editorial-study/optimization/replicates-cold6/replicates-summary.md`,
the summaries of `rec-curated-t6` and `rec-recall-t6` (both complete at 287 cycles, summarised read-only with
`scripts/replicate_study.py --resume` in a scratch directory), `replicates-baseline/replicates-summary.md`; experiment findings, "Clean labels and the v3 recording". The 17 to 20
percent versus 40 figure is in `docs/experiments/setup-findings-20261011.md`.

## 4. Parameters

All of these are flags on `scripts/run_editorial_prequential_study.py`. The application runtime has its own
settings with different defaults (for example, the runtime default for `rubric_changes_every` is 2).

| Parameter | What it does | Default | What we measured | Change it when |
|---|---|---|---|---|
| `--rubric-changes-every` | Label transitions between rubric steps | 6 (study script; was 20) | Trigger 20: first rubric at cycle 82. Trigger 6: cycle 21 | The task is lopsided, or you want the first rubric earlier |
| `--selection-primary` | Objective used to promote candidates | accuracy | Goals moved the targeted metric, not accuracy | Errors of one kind cost more |
| `--decision-model` / `--model` | Decision model, and the LLM optimizer | `gpt-4.1-mini` for both; we used Jev 1.13.0 and gpt-6-luna | Jev matched larger models at a fraction of the cost | You change models |
| optimize and retrain cadence | Stage and ML model refit interval | 200 (fixed in the study script) | Not varied | You are building your own loop |
| `--max-decision-calls`, `--max-optimizer-calls` | Per-run caps | 6000 and 60 | Our runs used 1,038 to 2,320 decision requests and 4 to 13 optimizer requests | Always set both before a paid run |
| Idea screening, shadow evaluation, diagnosis, proposal operators | Test many ideas, not one | off | One full run promoted nothing | Only with a large corpus; experimental |

### 4.1 `--rubric-changes-every`

It counts label transitions: consecutive labeled stories whose labels differ. It does not count labels. On a
lopsided task transitions are rare, so a trigger of 20 means the first rubric arrives late. On our 287-story
corpus (17.8 percent publish), the first rubric came at cycle 82 with a trigger of 20 and at cycle 21 with a
trigger of 6, in all six replicates each.

| Measure (six replicates, window 70) | Trigger 20 | Trigger 6 |
|---|---:|---:|
| First rubric at cycle | 82 (all six) | 21 (all six) |
| First window accuracy | 41.4 to 47.1% | 72.9 to 77.1% |
| Final window median (range) | 92.1% (88.6 to 95.7) | 95.7% (94.3 to 95.7) |
| Post-warm-up minimum | 89.5% | 93.3% |
| Optimizer requests per run | 4 | 13 |
| Optimizer cost per run | about 4 cents | about 11 cents |

A lower trigger buys an earlier rubric and more optimizer calls. We have not measured triggers below 6 on this
corpus, or either trigger on a balanced task. The study script default is now 6 for this reason.

Source: `replicates-baseline/replicates-summary.md` and `replicates-cold6/replicates-summary.md`. The cost per run is
in `docs/experiments/setup-findings-20261011.md` (gpt-6-luna at LiteLLM prices).

### 4.2 `--selection-primary`

Accuracy is the default objective. The others are recall, precision, f1, balanced accuracy, brier and balanced
brier. Precision, recall and F1 are for the positive class. A secondary guard can limit how far a second metric
may fall when a candidate is promoted. With any objective other than accuracy, idea screening does not run.

What we saw on the 1,200-item corpus (Jev, gpt-4.1-mini, cycles 401 to 1,200, one run per arm):

| Goal | Accuracy | Publish precision | Publish recall |
|---|---:|---:|---:|
| Precision | 85.9% | 88.7% | 62.7% |
| Recall | 85.2% | 84.1% | 66.5% |
| Balanced Brier | 85.2% | 83.3% | 66.0% |

Precision as the goal raised publish precision by 4.6 points. Recall as the goal did not reach the highest recall
(a 0.5 point gap, inside the noise). Accuracy did not move much. The window-to-window spread inside each arm
(about 12 points on accuracy) was larger than the gaps between arms.

When rubric promotion was gated on balanced Brier on a larger development share, rubrics were promoted (once per
run) and regressions were blocked, but accuracy did not improve (76 and 79 percent against 78) and calibration got
worse. A rubric was promoted because it was better calibrated on a small development set, and the accuracy stayed
flat.

Change the objective when one kind of error costs more than the other, and say which in writing first (section 7).
Do not expect a different objective to lift accuracy.

Source: `docs/experiments/optimization-goal-20261009.md`; `docs/experiments/rubric-gate-20261009.md`.

### 4.3 The decision model and the LLM optimizer

The decision model answers every item, so its price and speed set your run cost. The LLM optimizer is called rarely
but reads a long history.

- Jev 1.13.0 started at 80 percent with no rubric on the old corpus (the OpenAI models started at 51 to 58),
  cost $0.05 for 400 decision calls, and was the fastest. A model five times as costly (gpt-4.1) scored the same,
  78 percent over cycles 201 to 400. Model strength did not set the plateau on that corpus.
- gpt-6-luna as the optimizer is priced at $0.10 per million input tokens and $0.50 per million output tokens,
  doubled above 272,000 tokens per request, per LiteLLM. The optimizer's context grows through a run (about 290
  thousand input tokens per call on average in the 1,124-cycle run), so cost grows faster than the cycle count.
- In the 1,124-cycle run the optimizer's share was $2.93 of $5.67.

Source: `docs/experiments/decision-model-comparison-20261009.md`; `docs/experiments/editorial-long-run-20261009.md`.
The gpt-6-luna price is in `docs/experiments/setup-findings-20261011.md` (LiteLLM, standard tier).

### 4.4 Retrain and optimize cadence

The study script fixes `optimize_every` and `retrain_every` at 200. They are not flags and they are not recorded
in `protocol.json`. So a recording's setup does not show them. In the 1,124-cycle run the ML model was promoted four
times, at cycles 200, 400, 600 and 800, each time for a lower balanced Brier without per-class recall regression. We
have not measured other cadences.

Source: `scripts/run_editorial_prequential_study.py`; `docs/recordings.md`; `docs/experiments/editorial-long-run-20261009.md`.

### 4.5 Caps and kill switches

Set caps before any paid run.

- `--max-decision-calls` (default 6000) and `--max-optimizer-calls` (default 60) bound one run. The run ledger
  `global_budget_ledger.json` shows consumed against caps.
- The replicate harness adds `--max-total-decision-calls` and `--max-total-optimizer-calls` across all replicates.
  It is conservative, so runs that use far less than their caps can still be blocked.
- A live run needs `--confirm-live`. Without it the study writes its protocol and ledger and spends nothing.
- Stop a run by ending the process. Resume with `--resume`. A cap that stops a run is an interruption, not a
  finished trajectory. We set one cap too low (8,000 decision requests) and the review-everything arm stopped at
  cycle 1,031 of 1,200.

Source: `docs/replicates.md`; `docs/experiments/review-selection-20261009.md`.

### 4.6 Experimental: idea screening, shadow evaluation, diagnosis, proposal operators and elements

All of this is off by default (`--idea-screening`, `--diagnosis`, and the options around them). Turn it on only
if you have a reason and a large corpus. The design is in [docs/hypothesis-portfolio.md](hypothesis-portfolio.md).

What we found, plainly:

- In one full-pipeline run on the 287-story clean corpus, nothing cleared the shadow gate. Of 12 hypotheses, 9 were
  shadowed over 367 forward evaluations. The best fixed 7 stories and broke 2 over 120 stories (p 0.18).
- In that run the old refinement step did not run, because shadow evaluation replaces it from the next rubric
  round on.
- That run ended at 81.4 percent in the last window (cyclotron by window 42.9, 88.6, 92.9, 81.4). Six legacy
  replicates ended at 88.6 to 95.7 percent.
- The only diagnosis that ran found two causes and three candidate elements. None was kept.
- Known flaws, listed in the hypothesis-portfolio document and the findings: the diagnosis confusion matrix falls
  back to every classifier version when the new version has under 20 predictions, so it described early blank-rubric
  errors; the cause trial counts a "fix" against the recorded wrong answer, not what the current rubric would
  answer; and diagnosis reply limits (name 60, description 500 characters) rejected 6 replies.
- A corpus of 287 stories is too small for shadow evaluation to confirm small gains. We have not measured it on a
  larger corpus.
- Shadow evaluation on simulation: equal ideas were promoted 0.6 percent of the time, and a true 10 point idea was
  promoted 81 percent of the time within 400 forward stories. A true 5 point idea was found about a third of the
  time. These are simulated results.

Source: experiment findings, "Second v3 run: full pipeline"; `docs/hypothesis-portfolio.md` ("What the simulations
say" and the Stage 2d section); `replicates-baseline/replicates-summary.md`.

## 5. The review program

The review program decides which decisions a reviewer checks. It sets your cost and how many mistakes get through.

### 5.0 Review everything first, then taper

Do not start with selective review. We compared five review schedules from a blank start (trigger 6, accuracy
objective, 287 stories, windows of 70, one run each). Accuracy is scored against the corpus's reference labels, not
only the stories that were reviewed.

| Review schedule | Stories reviewed | Cyclotron accuracy by window | Optimizer calls |
|---|---:|---|---:|
| Every story | 287 | 77.1, 94.3, 92.9, 95.7 | 13 |
| Every story for 50 stories, then half | 169 | 78.6, 97.1, 92.9, 94.3 | 6 |
| Publish-priority taper (all publishes and half the rejects for 50 stories, then half and a quarter) | 113 | 78.6, 95.7, 97.1, 91.4 | 5 |
| The model's publishes and half its rejects, from the first story | 46 | 45.7, 61.4, 61.4, 41.4 | 0 |
| One story in ten, from the first story | 29 | 45.7, 60.0, 60.0, 41.4 | 0 |

Always answering the most common label scores 84.3, 80.0, 78.6 and 84.3 in those windows.

The two schedules that start selective never proposed a rubric. The trigger counts label transitions among reviewed
stories, and with so few reviews it never fired, so the cyclotron kept the decision model's default behavior, which
is to say publish for most stories (recall 82 to 100 percent, precision 19 to 35 percent). The run did not fail
loudly: it ran to the end and looked busy. The two schedules that review everything first learned a rubric by about
story 21 and then held their accuracy on 59 and 39 percent of the labels.

What to do: use an onboarding schedule (the study script has `first_50_all_then_half`, `first_100_all_then_half` and
`publish_priority_taper`), and confirm that the first rubric has arrived before the review rate drops. One run each;
a window of 70 carries about plus or minus 7 points; we have not measured how short the onboarding can be.

Source: `docs/experiments/setup-findings-20261011.md`, "Review schedules from a blank start".

### 5.1 Review rate and which items to review

In the 1,200-item corpus, cycles 401 to 1,200, with a 25 percent review target:

| Arm | Whole-system accuracy | Mistakes that slipped | Mistakes caught per 100 labels | Cost |
|---|---:|---:|---:|---:|
| Review everything (to 1,000) | 100% | 0 | 15.7 | $2.69 |
| Random 25% | 89.6% | 83 | 12.6 | $0.28 |
| Least-confident 25% | 94.4% | 45 | 30.4 | $0.45 |
| Mixed (20% least-confident plus 5% random audit) | 95.1% | 39 | 25.2 | $0.48 |

Whole-system accuracy counts reviewed items as right. Mixed had the fewest mistakes slipped and the highest
whole-system accuracy, at a sixth of review-everything's cost. Random-only learned best (86.4 percent), so it is the
choice when the goal is the model's own accuracy. The random audit keeps an unbiased sample for checking
calibration.

Two cautions. Jev's confidences take few distinct values, so many items tie at the cut and the confidence-based
arms reviewed more than 25 percent (33.5 and 35.7 percent against 25.6 for random). And there was one run per arm,
with window-to-window spread of about 5 points.

Source: `docs/experiments/review-selection-20261009.md`.

### 5.2 Tapering

| Arm | Labels, share of review-everything's | Whole-system accuracy, 401 to 1,200 | Mistakes slipped |
|---|---:|---:|---:|
| Metric taper | 91% | 98.5% | 12 |
| Time-based taper (first 100, then 50% random) | 56% | 94.3% | 46 |
| Fixed random 25% | 26% | 89.6% | 83 |

The metric taper almost never let go. It needed an ECE below 0.05 over 200 reviews, which is about the size of the
sampling noise, and the calibration sat at 0.05 to 0.08. It stepped down once and went back to full review at cycle
1,000. The time-based taper reached 94.3 percent with 56 percent of the labels. Learning accuracy was the same within
noise across all arms (84 to 87 percent), so fewer labels did not slow learning here.

Do not ship a metric taper with these parameters. A variant (judge each window by whether the auto-approve promise
held, loosen the ECE bar to about 0.08) was proposed and has not been run.

Source: `docs/experiments/review-taper-20261009.md`.

### 5.3 Auto-approve thresholds drift

A fixed rule such as "approve when confidence is at least 0.80" does not hold its promise. In the 1,124-cycle run the
cyclotron's mean confidence drifted up from 0.80 to 0.84 at the same accuracy. The rule approved more items (58 to 83
per window) at lower precision (93 percent down to 81). Whole-system accuracy fell from 96 to 97 percent in cycles 401
to 600 to 84 percent in cycles 1,001 to 1,100.

Re-check any fixed threshold against a random audit on a schedule. Keep the random audit running for as long as the
threshold is in use.

Source: `docs/experiments/editorial-long-run-20261009.md` (finding 6); experiment findings, "A fixed threshold drifts".

## 6. Running it with evidence

### 6.1 The replicate harness

One run is one draw. `scripts/replicate_study.py` runs the same arguments K times with different seeds and
summarises the spread ([docs/replicates.md](replicates.md)). A seed varies the order of stories within each class and
the development split, and the feedback policy under selective review. It does not vary the decision model's or the
optimizer's sampling, so the remaining variance is the services' own.

Run `--dry-run` first. Set the global caps. A batch of six runs at trigger 6 cost about $1.15 in total (`docs/experiments/setup-findings-20261011.md`).

### 6.2 Recordings

`scripts/recordings.py` keeps each run as a draft recording with its setup, per-window results and cost, and
publishes only the few that explain something ([docs/recordings.md](recordings.md)). Publishing refuses a
recording without label provenance, a cost, and a majority baseline on every window. Publish a run because it
shows something (a blank start climbing, an objective that did nothing), not because it looks good.

### 6.3 Reading a run

- Windows: accuracy per block of 70 stories, for the cyclotron, for the decision model alone, and for the majority
  baseline.
- Majority baseline: the best constant answer. A window that does not beat it by a clear margin shows nothing.
  The harness calls a replicate reliable when its final window beats the baseline by at least 5 points.
- First rubric cycle: when the first rubric arrived. Everything before it is a decision model without a rubric.
- ML model share: the share of decisions made by the fitted ML model. In the trigger 6 replicates it was 92 to 93
  percent, against 71 percent at trigger 20.

### 6.4 What the numbers can and cannot tell you

A window of 70 stories carries about plus or minus 7 points of sampling noise. The spread between replicates of one
setup was up to 7 points on the final window at trigger 20. A gap smaller than that is not a result. A single run
shows that a path is possible, not that it is typical. Scoring against reference labels says nothing about whether
the reference labels are right. We have not measured any of this on real reviewers' labels.

Source: `replicates-baseline/replicates-summary.md`, `replicates-cold6/replicates-summary.md`;
`docs/experiments/review-selection-20261009.md` (about 5 points of spread). The plus or minus 7 point figure for a
70-story window is from the owner's brief.

## 7. What the loop cannot decide for you

The loop optimizes whatever the labels and the objective say, including their biases. Decisions in regulated areas
need a documented, auditable setup and monitoring of errors by group. This guide does not say more than that.

The things the loop cannot settle:

- Who the reviewers are, and whether their corrections are consistent with each other and over time.
- What "right" means for your decision. Write it down.
- Which errors cost more. A missed positive and a wrongful positive are rarely the same cost.
- How errors differ by group. The loop reports overall accuracy. It does not compare groups unless you ask it to.
- What must be logged for audit: the version of the rubric and ML model at each decision, the label, the
  reviewer, and the explanation. The engine records requests, responses, feedback, versions and measurements
  ([README](../README.md)). Check that this covers what you need.
- When to stop trusting the thresholds (section 5.3).

## 8. A setup checklist

In the order we would do it.

1. Write the decision as one question a reviewer can answer.
2. Name the labels and the positive class.
3. Write down what "right" means, in sentences.
4. Write down which error costs more, and by how much.
5. List who the reviewers will be.
6. Decide what must be logged for audit.
7. Decide how errors will be compared across groups, and where that data comes from.
8. Gather a sample of real items from the stream.
9. Compute the share of each class and the majority-class baseline.
10. Decide what the decision model will see, and make it match what the reviewer sees.
11. Write the labeling policy as short rules with a line for each edge case.
12. Label a sample twice, independently.
13. Measure agreement. If it is low, fix the policy and repeat.
14. Hand-audit a sample of each class.
15. Leave out items the labelers split on.
16. Build a seed set with at least 6 labels per class and positives among the first 12.
17. Check where the rubric goes for your decision model.
18. Score no rubric, your draft rubric, and the policy text, on the same sample.
19. Choose a starting rubric: blank, curated or custom. Record why.
20. Choose the objective. Keep accuracy unless one error clearly costs more.
21. Set the rubric trigger. On a lopsided task use a low one and read the first-rubric cycle.
22. Choose the decision model and the LLM optimizer. Look up the price per request and check long-context pricing.
23. Set `--max-decision-calls` and `--max-optimizer-calls`. Set global caps for replicates.
24. Do a dry run. Read `protocol.json` and the ledger. Confirm nothing was spent.
25. Choose the review program: mixed selection with a random audit is a sound start.
26. Decide when the review rate may drop, using time or volume, not a tight calibration bar.
27. Run one replicate batch before you trust the setup.
28. Read the windows against the majority baseline. Check the first-rubric cycle.
29. Read the rubrics the loop wrote. Check them against your policy.
30. Check that corrections came from the reviewers you planned for.
31. Set a date to re-check any fixed confidence threshold against a random audit.
32. Leave idea screening and diagnosis off until you have a large corpus.
33. Record the run, with label provenance and cost.
34. Write down what you did not measure.

## 9. Appendix: failure modes we hit

| Symptom | Likely cause | What we did |
|---|---|---|
| Accuracy far below always-answering-the-most-common-label, and no optimizer calls | Selective review from a blank start starved the first-rubric trigger | Review everything first (`first_50_all_then_half`), then taper (section 5.0) |
| Accuracy that looks far too low only for runs that review a fraction of stories | The accuracy was computed from reviewed stories' labels, so unreviewed stories counted as wrong | Score against the corpus's reference labels (the exporter does; check any ad hoc analysis) |
| Accuracy capped near 85 percent whatever the rubric | Labels from one model that over-published | Audited, wrote an explicit policy, relabeled with two blind passes |
| Labeling passes disagree (kappa 0.07 to 0.24) with boilerplate rationales | Weak labeler, no explicit policy | Used two blind passes under a written policy (kappa 0.90) |
| Jev publishes far more than the labels do (65 percent by default) | No rubric | Started from a rubric or let the first rubric arrive early |
| Curated rubric makes Jev too strict (17 to 20 percent publish against 40 wanted) | Rubric biased toward reject | Compared against the labels before trusting it |
| First window near 45 percent, first rubric at cycle 82 | Trigger of 20 counts label transitions, which are rare on a lopsided task | Set the study default to 6; first rubric moved to cycle 21 |
| Same rubric scores 7 points worse | Rubric sent in state, not in criteria, for Jev | Moved it into the criteria of the decision question |
| Accuracy flat after rubric promotion | Rubric not the lever on that corpus | Reported it. The model and the labels set the plateau |
| A very short rubric took over (110 characters) and the decision model was right on 53 percent | Provisional rubrics accepted with no filter | Fixed the gate and the development share; see the rubric-gate note |
| ML model replaced by the model's raw probabilities (ECE 0.19) | A selection won on a small development set | Recommended a head safeguard before using a larger development share |
| Metric taper used 91 percent of labels | ECE bar of 0.05 is about the size of the noise | Recommended a time-based taper or looser bars |
| Auto-approve at 0.80 got worse over time | Mean confidence drifted up at constant accuracy | Recommended a random audit and a re-check schedule |
| Confidence-based review reviewed 33 to 36 percent, not 25 | Few distinct confidence values tie at the cut | Noted it; a random tie-break is a follow-up |
| Run stopped at cycle 1,031 | Decision-request cap set too low | Compared over the cycles that finished |
| Run died at cycle 1,125 with a database error | Read-only query held a lock past the 5-second default | Store now opens in WAL mode with a 30-second timeout |
| Run died on one connection error | No transport retries | Study has opt-in `--transport-retries` |
| Run died when the optimizer proposed a change outside the rubric stage | Invalid proposal raised | Now recorded as `invalid-proposal` and dropped |
| Shadow evaluation promoted nothing | Corpus of 287 stories too small for small gains | Left it off by default; a larger corpus is needed |

Source: `docs/experiments/*`; experiment findings (Cyclotron-web wiki).
