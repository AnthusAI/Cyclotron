# Changelog

All notable changes to this project are documented in this file.

This changelog is maintained by hand, one entry per release. semantic-release bumps the version on `main` but does not write this file. The Python package and the web package (`cyclotron`, tarball `ui-vX.Y.Z`) are versioned separately.

## Engine 0.20.0 to 0.21.0: one cyclotron, many outputs

The epic `flywheel1f`. A cyclotron can now run several reviewed outputs (classifiers) side by side.

- **Sibling check (#9).** A change to one output is scored on every sibling's development labels before promotion. `CyclotronDefinition(sibling_policy=SiblingPolicy("refuse"|"record", max_regression))` sets the policy; `refuse` is the default. The policy is not part of the definition fingerprint.
- **Seeded supporting questions (#10).** `Cyclotron.open(seed_supporting_questions=...)` is fixed when the store is created.
- **Shared supporting questions (#11).** A supporting question that several outputs ask with the same name, instructions and labels is asked and paid once. Sharing is a store setting fixed at creation. New stores share; existing stores keep their requests byte-for-byte.
- **Per-output monitoring (#12).** The review-rate program checks every output, and the worst output decides. `review-rate-changed` events name it. `Cyclotron.overview()` lists every output.
- **Checklist reviews (#13).** `review(decision_id, labels={classifier: label, ...})` labels several outputs in one call. Partial checklists are allowed, and it is idempotent by `review_id`.
- **Per-output routing (#14).** Each output is selected for review on its own. `decision.review.outputs` and `decision.review.requested` record it. `expectedReviewsPerWeek` counts the chance that any output is requested (#20).
- **Deferred learning (#15).** `Cyclotron.open(learning="deferred")` makes `review()` only record and queue. `await cyclotron.learn()` runs the queue, and `pending_learning()` lists it. **`learn()` runs each queued output at most once per call** (#21), so an output that gained labels during its pass stays queued. Callers that want an empty queue loop:

  ```python
  while cyclotron.pending_learning():
      await cyclotron.learn()
  ```

- **Promote a supporting question (#16, #22).** `promote_supporting_question` (store and GraphQL `promoteSupportingQuestion`) makes a supporting question a classifier of a new cyclotron version. A trace-ui control asks for confirmation first.
- **Cost per output (#17).** `usage()` includes `outputs.by_output`, an estimate that sums to the total. Each output now has its own optimizer copy, so optimizer events no longer leak into sibling traces.
- **Async optimizer transport (#21).**
  - With an `acomplete` coroutine transport (`OpenAIOptimizer(async_client=...)`, `LiteLLMOptimizer(acompletion=...)`, or `from_environment(asynchronous=True)`), `learn()` lends the store lock during provider calls, so reviews and decisions keep working.
  - A pass is superseded, and stays queued, if an output's criteria or a label it rests on changes during the call. `max_superseded_passes` caps repeats.
  - Cancellation is safe. A pass's own interrupted paid request is dropped openly and recorded as `decision-request-abandoned`.
  - Synchronous transports behave as before.
- **Glossary (#18).** The console, docs and diagrams use classifier (a reviewed output), supporting question, feature and ML model. The trace UI shows superseded and cancelled learning steps as such.

## Web package 0.2.2 (2026-10-16)

- The priming panel, and the animated cycle mark and `CyclotronBrand` `animate` prop described next.


- New export `cyclotron/components/cycle-mark` (`CycleMark`): the logo's cycle mark with one animation. At rest it is exactly `RefreshCwWide`. A cycle moves both arrows half the racetrack (a dash offset and arrowheads on one closed path, never a rotate transform, because the wide mark matches itself only at 180 degrees) and lands on the logo pose. `cycleKey` or a `cycle()` handle plays one cycle; reduced motion keeps it still; no new dependencies.
- `CyclotronBrand` takes an optional `animate` prop (off by default). `animate` cycles the mark 3 s after the logo first becomes visible, then every 10 s (`{firstDelayMs, intervalMs}` to change). The clock runs only while the logo is on screen and the tab is showing.

## Web package (unreleased)

- New exports: `cyclotron/components/priming-panel` with `cyclotron/priming-client`, and `cyclotron/components/held-out-report` with `cyclotron/held-out-report` (the cyclotron-held-out-report/v1 type).
- Two styling tiers, documented in the README: the plain-CSS exports need only `components.css`; `priming-panel` and the other Tailwind-tier exports need Tailwind v4 in the host.
- `scripts/dev/release_ui.sh` runs the web tests and build, packs the tarball, prints its SHA-256 and SHA-512 and the pin block, and creates the `ui-v<version>` tag and release. `--dry-run` stops before the tag.
- `PrimingPanel` creates request ids with `requestId()`, so it works on plain-HTTP hosts without `crypto.randomUUID`, and its template download carries a MIME type.
- `ReviewControl` in thumbs mode with more than two classes shows one button per class instead of throwing.
- The held-out report doc string names the schema `cyclotron-held-out-report/v1`.

## Web package 0.2.1 (2026-10-09)

- Status strip reads "Tapering · 25% reviewed"; the card takes a classifier display name.
- Review control works with no reason list; field ids derive from item and decision ids (fixes a hydration mismatch in server-rendered host pages).
- `seed_rubrics` on `Cyclotron.open` lets an application seed the first rubric without changing the definition.

## Web package 0.2.0 (2026-10-09)

The npm package `cyclotron` (the web components and TypeScript types) is
versioned separately from the Python package and released as a tarball on
GitHub (`ui-v0.2.0`, built with `make pack-ui`).

- New exports: `cyclotron/components/review-control` (ReviewControl),
  `cyclotron/components/cyclotron-status` (CyclotronStatusView),
  `cyclotron/cyclotron-status` (the cyclotron-status/v1 type),
  `cyclotron/sdk-types` (Decision, Review and subscribe event shapes from the
  Python SDK) and `cyclotron/styles/components.css`.
- `styles/components.css` holds component rules only, with no global reset
  and no `:root` theme, so an application can import it safely.
  `styles/shared.css` (the console and marketing theme) imports it.
- The tarball now includes `CalibrationBins.tsx` and
  `CalibrationProvenance.tsx`, which `ModelComparison` and
  `ReliabilityCurve` import; a spec checks that every packed import resolves
  inside the tarball and that none uses the `@/` alias.
- Removed the `cyclotron/lib/utils` export, whose file did not exist.

## Python package releases from main

One entry per version, taken from the feature and fix commits between releases. Oldest first.

### 0.10.0 (2026-10-09)

First release recorded on main; earlier numbers (0.7.0 to 0.9.0) were never recorded here.

### 0.11.0 (2026-10-09)
- feat(status-widget): render cyclotron-status/v1 as a strip or a card
- feat(review-control): an embeddable, data-only review control
- feat(review-rate): make the review rate engine state, computed, explained and overridable
- feat(store): portable, single-writer, crash-safe embedded cyclotron store
- feat: add glossary, lint spec, and console copy per glossary
- feat(sdk): embed a cyclotron in an application with decide, review, status, subscribe
- fix(status): number versions by activation and say what changed
- feat(status): add the cyclotron-status/v1 snapshot for embedding applications
- feat(brand): shorten the cycle mark to 1.75:1
- feat(brand): tagline reads self-aligning AI decisions
- fix: batch scorecard trace ingestion

### 0.12.0 (2026-10-09)
- feat(package): release the embeddable web package as 0.2.0

### 0.13.0 (2026-10-09)
- fix(review-control): derive field ids from the item and decision
- fix(status-widget): shorter review-rate copy in the strip and a classifier display name
- feat(sdk): seed a classifier's first rubric from the application

### 0.14.0 (2026-10-09)
- feat(study): export routing metrics, model usage and cost per window
- feat(study): land Ryan's uncommitted study and calibration work

### 0.15.0 (2026-10-09)
- feat(sdk): include item values in the decision log
- feat(sdk): measure model usage and cost per window of decisions

### 0.16.0 (2026-10-09)
- fix(optimizer): drop a proposal that reaches outside its stage instead of failing the step
- feat(study): opt-in transport retries for long editorial runs
- perf(events): record the out-of-fold fit proof once, in its fit-completed event
- feat(study): run the editorial study on a corpus of any length
- perf(replay): read only the event kinds each per-cycle check needs
- feat(study): extend the Wikinews editorial corpus to 1,200 items
- perf(cycles): keep cycle events and calibration samples from growing with the training set

### 0.17.0 (2026-10-09)
- fix(store): open the runtime store in WAL mode with a 30-second busy timeout
- feat(study): export the first N cycles of a longer editorial recording

### 0.18.0 (2026-10-10)
- fix(replay): a declared selection policy does not make a runtime used
- feat(replay): a review-rate taper driven by reviewed outcomes
- feat(study): choose the optimization goal for candidate selection
- feat(study): export a cut from a recording that stopped early
- feat(replay): review a fixed share by least confidence, at random, or mixed with an audit
- feat(study): let the decision model see chosen corpus fields, such as the title
- feat(study): choose the decision model and provider apart from the optimizer
- feat(study): configure the rubric gate and seed exact answers from an earlier run

### 0.19.0 (2026-10-11)
- feat(web): animated cycle mark and optional animate prop on CyclotronBrand

### 0.19.1 (2026-10-11)
- fix(web): the cycle mark does not play a cycle at mount under React StrictMode

### 0.20.0 (2026-10-12)
- fix(glossary): ML model and supporting questions in the console, docs and diagrams
- feat(usage): estimate cost per output
- feat(workspace): promote a supporting question to an output
- feat(learning): optimize after the review instead of inside it
- feat(review): route reviews per output
- feat(review): one review labels several outputs
- fix(review-rate): watch every output, not just the first classifier
- fix(batch): never change an existing store's request when sharing questions
- feat(batch): ask a supporting question shared by several outputs once
- feat(embedded): seed supporting questions when a store is created
- fix(promotion): check sibling outputs before promoting a context change

### 0.20.1 (2026-10-12)
- fix(review-rate): weekly review estimate follows per-output routing

### 0.21.0 (2026-10-17)
- feat(status): report how many examples primed a cyclotron
- fix: keep develop tests passing on the storage ports
- fix: make operational publication review explanations optional
- feat: compose shared priming and guarded publication source for review
- fix(learning): second review round on the async learning window (#21)
- fix(learning): make the async learning window safe (review of #21)
- feat(trace-ui): control to make a supporting question a classifier
- feat(optimizer): async optimizer transport so deferred learning overlaps provider calls

### 0.22.0 (2026-10-17)
- feat(held-out): measure before and after on a fixed held-out set
