# Hypothesis portfolio (Stages 1, 2 and 2b)

Decision-model calls are cheap, so the optimizer should not pick one winner and commit. It should keep many
ideas (rubric, example and question changes), keep evidence about each for a long time, and screen many
candidates cheaply. Stage 1 is the library layer (ledger and screen). Stage 2, below, wires it into the live loop as an opt-in (mode `dev_screen`). Stage 2b adds forward shadow evaluation (mode `shadow`, the default when screening is on).

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

## Stage 2b: forward shadow evaluation (opt-in, mode `shadow`)

### Why dev-partition screening was too small

A real validation run showed Stage 2 promoting nothing. Each round screened on 16 to 29 stories and confirmed on
a reserve of 3 to 12, because it only used the engine's small development partition. With 18 ideas and 893
evaluations no finalist could clear a paired test. Decision-model calls cost a fraction of a cent, so labels, not
calls, are the scarce thing. Forward shadow evaluation gets evidence from stories that arrive anyway.

### How it works

Every new story is scored by the incumbent (as always) and by a few candidate ideas, the shadow set, before its
label is revealed. Those stories were never seen by any idea's author (the optimizer), so the evidence is clean.
It grows by one paired result per idea per story with no extra labels.

1. Rubric stages (and the scheduler's `rubric` and `example_ids` controls) still ask the optimizer for
   `proposals_per_round` distinct proposals and save them as control ideas. In shadow mode nothing is scored on the
   development partition; the new ideas take free shadow slots, newest first. `tasks` ideas are not shadowed.
2. In `run_cycle_replay`, after the incumbent predicts and the feedback policy chooses, each shadow idea answers
   through `wheel._answers` (the same request ceiling, answer cache and durable record as every decision, so
   `wheel.requests` stays truthful and cache hits are free). Then the label is revealed, and correctness for each
   idea and for the incumbent is written to the ledger with `context_version='shadow'`. The incumbent's side of the
   pair is its decision-model answer (not the fitted head), the same thing the idea's answer is.
3. Per story with any shadow scoring, one `portfolio-scored` event records the incumbent's choice and confidence
   and each idea's, with no label, so disagreement can be analysed later. Nothing routes on it yet (Stage 3).
4. After each such story, and at each rubric trigger, `review` compares every shadow idea with the incumbent on
   the shared forward stories.

### Forward-only rule

The replay ticks a story sequence number once per story. An idea records the sequence number at which it was
created (`created_seq`) and joins the shadow set at or after that; it is scored only on stories with a larger
sequence number. The `shadow_scored` table logs every (idea, story, sequence) so this can be audited, and the tests
assert it from the model's call log.

### The protected-partition rule (how it is read)

The replay has three roles: `training`, `development` and `scoreboard`. Scoreboard stories are the permanent
evaluation firewall: their labels are never revealed to the runtime. Stories the feedback policy does not select
are not revealed either. The shadow set is scored only on stories that (a) are selected for review, so their label
will be revealed, and (b) are not scoreboard. Anything else is never scored by a shadow idea, never enters the
ledger and is never evidence for promotion. Training and development stories are both allowed: for a shadow idea
they are forward stories it was not written from, and nothing else about them is used. Because only reviewed
stories count, the evidence is about the stories the policy selects, which can differ from the whole stream
under selective feedback.

### Promotion and alpha spending

Per idea and per incumbent, the idea gets at most `shadow_looks` looks, at the predetermined shared-story counts
`shadow_min_items * k`. A look clears when the exact two-sided sign test p-value is below `alpha / shadow_looks`
and gained is greater than lost. A fixed number of looks at predetermined counts tested at `alpha / looks` is a
Bonferroni bound: however the looks are correlated, an idea that is truly no better clears with probability at
most `alpha`. Testing every look at `alpha` instead would exceed it (in the simulation, 6.0% against 0.6%). It is
conservative, so power is lower than an unadjusted test. With `k` ideas in the shadow set at once, the chance that
some equal idea clears is up to `k * alpha`; each idea is held to `alpha`. A verdict `advanced` is recorded when an
idea first reaches `shadow_min_items` shared stories with a positive net.

An idea that clears is promoted by a distinct, logged path, not the fitted-trial path: `idea-promoted-by-shadow`
(evidence: gained, lost, p, n, looks, threshold), then the idea's change becomes the active decision context
(`classifier-activated`, then a `promoted` event with `promotion_path='shadow-evaluation'`), verdict `promoted`,
and the idea record gains an attempt. No head is fitted in the promotion; the usual classifier retraining refits
it on its own schedule, so predictions use the new decision answer until then.

### Eviction

An idea is evicted (verdict `screened_out`, reason in plain words, slot freed for the next waiting idea) once it has
`shadow_evict_after` shared forward stories and gained is not greater than lost. Ideas that have not reached the
count keep their slot, which is the exploration allowance. An idea that uses all its looks without clearing is
also evicted. Nothing is ever deleted: ledger rows and verdicts stay.

### The incumbent changes

The incumbent's id is its configuration fingerprint. Its ledger rows are written under that id, so a paired
comparison only uses stories scored while that incumbent was active. After a promotion the old rows stay in the
ledger but never pair with the new incumbent; every remaining idea restarts a comparison and its looks against the
new incumbent on the stories that follow (an idea is applied to the current incumbent, so it means "this change
on top of what is active now").

### Optimizer context

In shadow mode the digest given to the optimizer is each shadow idea's forward record (stories, fixed and broke
counts with a few story ids, running accuracy, last verdict, shadow status) plus forward stories every voter
got wrong, with the same caps as Stage 2. There is no hidden reserve. It contains only stories that have arrived
and whose labels were revealed, never a story awaiting its label. Because development stories can appear in it,
the run still records `evaluation_context_exposed=true`.

### Options and defaults

`IdeaScreeningConfig` gains `mode` (`shadow` or `dev_screen`; default `shadow`), `shadow_ideas` (default 4 since Stage 2c, was 3),
`shadow_min_items=40`, `shadow_evict_after=30`, `shadow_looks=10`, `max_shadow_calls=3000`. All are validated,
saved in `runtime_state` and kept on resume. A Stage 2 configuration saved without a mode resumes as `dev_screen`.
Study flags: `--screening-mode`, `--shadow-ideas`, `--shadow-min-items`, `--shadow-evict-after`, `--shadow-looks`,
`--max-shadow-calls`, recorded in `protocol.json` with the rest of the config. Screening is still off unless
`--idea-screening` is given.

### Budget safety

Shadow calls count against `max_shadow_calls` and the wheel's request ceiling. Before each shadow call the
evaluator checks that the remaining requests exceed what normal work still needs (one per remaining story, plus
the training and development size for a fitted trial). Otherwise it stops scoring, logs `shadow-scoring-stopped`
once per reason, and resumes by itself if the reason goes away. A failed shadow call is logged
(`shadow-scoring-failed`) and skipped; it never blocks the decision.

### What the simulations say

Using the module's own look rule on simulated paired outcomes (3000 runs, 10 looks of 40 stories): equal ideas
were promoted 0.6% of the time. A true +10 point idea (incumbent 75% right, idea 85%) was promoted 81% of the time
within 400 forward stories, median about 160, and 98% if early eviction is switched off; eviction at 30 stories
costs power, so raise `shadow_evict_after` if ideas are expensive to produce. A smaller +5 point idea was found
about a third of the time within 400 stories.

### Not built

Routing on shadow disagreement (Stage 3) and cross-run idea memory (Stage 4).

## Stage 2c: proposal operators (opt-in with idea screening, shadow mode)

### Why

In the last real run 18 proposed rubrics were scored on forward stories and 16 were evicted with fixed about equal to
broke; several changed Jev's answers on none of their 30 stories. The optimizer returned timid wording edits of one
rubric, so shadow evaluation had nothing to find, while a held-out test showed the true labeling rubric (different
rules: topic-specific rules for sports and animals, "outcome must be established in the text") beats the learned one
by 6.5 points. The problem was proposal diversity and targeting, not evaluation. Stage 2c changes how ideas are
asked for. Evaluation, alpha spending and promotion are unchanged.

### The operators

Each idea comes from one operator; the optimizer is told which proposals serve which operator.

* `mutate`: today's multi-proposal request, careful revisions of the incumbent.
* `bold`: a rubric that differs structurally from the incumbent and prior ideas: different decision rules,
  topic- or category-specific rules, explicit boundary cases and counter-rules, guidance for both labels.
* `target`: the optimizer sees a bounded sample of stubborn stories and is asked to fix those failure patterns
  without breaking what works.
* `combine`: the optimizer sees two complementary rubrics with their fixed/broke records and merges the winning
  clauses of each into one rubric.

`IdeaScreeningConfig.proposal_mix` is a validated mapping such as `{"mutate":0,"bold":2,"target":2,"combine":2}`.
Unset, it means the default: `bold=2, target=2, combine=2` in shadow mode (and nothing in `dev_screen` mode, where a
mix is rejected and `proposals_per_round` keeps its meaning). In shadow mode the mix total (6 by default) replaces
`proposals_per_round` for the rubric control; `example_ids` still uses `proposals_per_round`. A mix must have
`mutate` or `bold` above 0 so a round can always run. `shadow_ideas` now defaults to 4; a larger queue is fine because
eviction frees slots (and the alpha-spending threshold divides by the number of ideas shadowed at once).

### Optimizer calls per round

One call per round returns every operator's proposals (`propose_operators`; the reply is `{"proposals": [{"operator",
"rationale", "rubric"}, ...]}`). The reply is parsed strictly: a missing, unknown or unrequested operator, more
proposals from an operator than were asked for, or an empty list is rejected. Fewer is allowed. The only extra
calls are novelty retries: if the novelty gate rejects bold proposals, up to `novelty_retries` (default 1) more calls
re-ask for just the rejected number. A round with no rejection is exactly one call. Optimizers without
`propose_operators` (custom ones) get the plain multi-proposal request sized to the mix total, with a
`proposal-operator-skipped` event (operator `all`).

### Caps on what is sent

At most `target_sample` stories (default 8, at most 20), each excerpt 400 characters and each title 160; two parent
rubrics of at most 4000 characters, with at most 8 story ids each; plus the existing digest caps (20 prior ideas, 10
ledger rows, 5 ids per row, 20 stubborn ids). The operator block is therefore a few kilobytes on top of the usual
briefing.

### Novelty gate (bold)

Distance is `1 - Jaccard` over lowercased word 3-shingles (a text with fewer than 3 words uses single words).
0 is identical and 1 shares nothing. A proposal's novelty is its smallest distance to the incumbent rubric and every
prior rubric idea (and the earlier proposals of the same reply). A bold proposal below `min_novelty` (default 0.35)
is rejected (`proposal-novelty-rejected`), re-asked within the retry bound, and dropped with a
`proposal-operator-skipped` event if it still fails. Other operators store the number but are not gated. The measure
sees surface overlap, not meaning: it catches copies and light edits, and a pure paraphrase passes.

### The target sample

Candidates are stories every tried idea got wrong (`HypothesisLedger.stubborn_items`, minimum 3 ideas, across
forward and development evidence) and then the incumbent's newest errors. Only training-role stories can be shown:
their text, label and human explanation were already revealed to the optimizer. Development and protected stories,
and any story whose text equals one, are never shown, and neither is a story the runtime has no revealed text for.
Candidates are grouped by error direction (true label, wrong answer) and taken in turn across groups, stubborn
first, so the sample is balanced. Each story carries title, excerpt, true label, the wrong answer, and the
explanation. With no such story yet, the operator is skipped with an event.

### The complementary pair (combine)

For ideas X and Y on the stories both were scored on (forward evidence): `fixes_Y_not_X` is Y right and X wrong,
`fixes_X_not_Y` the reverse. A pair qualifies when both are at least 2 and they share at least 10 stories. The score
is the smaller of the two; the best score wins. Ties prefer more members with positive net versus the incumbent,
then more shared stories, then the lower ids. The incumbent may be one member. Pairs already combined are not
chosen again. With fewer than two ideas that have 10 evaluated stories, or no qualifying pair, combine is skipped:
a `proposal-operator-skipped` event with the reason, and nothing is redistributed to the other operators. Both
parents are recorded.

### Lineage

Each idea in `control_ideas` may carry `operator`, `parents` (idea ids; the incumbent appears as its
`incumbent-...` id) and `novelty`. Older ideas lack them, and readers treat that as normal. Creating an idea emits the
usual `hypothesis-discovered` plus `proposal-operator {idea_id, operator, parents, novelty}`.
`summary_for_optimizer` and the shadow digest show `operator`, short `parents` and `outcome` (the last verdict, or
`untested`), and the digest adds `operator_outcomes` once any idea has an operator.
`HypothesisLedger.operator_outcomes()` returns per operator: proposed, advanced (ever advanced or promoted),
promoted, and mean forward net gain (the latest recorded net per idea, `None` when none). `lineage(idea_id)` returns
the idea and its ancestors, nearest first.

### Options and flags

`proposal_mix`, `min_novelty=0.35`, `target_sample=8`, `novelty_retries=1`, all validated and saved with the rest of the
config. Study flags: `--proposal-mix bold=2,target=2,combine=2`, `--min-novelty`, `--target-sample`; recorded in
`protocol.json` through `as_json`. Unset flags leave the defaults. `--shadow-ideas` now defaults to 4.

### Not built

Clause-level splitting of rubrics and cross-run idea memory.

## Still not built

- Routing on the disagreement of the shadow ideas to send items to review (Stage 3). Stage 2b only records `portfolio-scored`.
- Cross-run idea memory, so screened-out ideas are scored on later labels and revived (Stage 4). Until then
  `revive_candidates()` is wired but only fires if something else scores a screened-out idea.
