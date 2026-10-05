# Decision Flywheel

Decision Flywheel is a small, model-neutral library for selecting labelled
context for structured decisions. It keeps the policy, budget, display order,
model identity, and development objective auditable. Its optional local head
fits deterministic numerical weights only from trusted feedback labels.

Optimization remains native to Decision Flywheel and provider-neutral; DSPy is
not part of this library's scope. This is an implementation boundary, not an
experimental result.

## Run the offline walkthrough

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
source .venv/bin/activate
make demo
```

Alternatively, without activation: `make PYTHON=.venv/bin/python demo`. The
activation above is only for the local virtual environment; this project never
asks users to source a `.env` file.

This makes `demo-output/artifact.json` and `demo-output/summary.json`. The demo
uses only tiny synthetic trusted labels and a scripted fake model: it makes no
network calls, uses no credentials or downloads, and is not a live benchmark.
It compares zero-, one-, and two-example-per-label policies: random seeds, a
lexical prototype, and target-conditioned lexical retrieval. The scripted rule
deliberately rewards a compact matching context, so the selected lexical
size-one winner is a property of this synthetic fixture, not a performance
claim.

The walkthrough selects on development labels, freezes a winner only after all
declared trials complete, saves it, reloads it, and decides a new unlabeled
synthetic target. Re-running produces the same artifact hash and context IDs.
After installation, `decision-flywheel-demo --output FOLDER` runs the same flow.
Use `make test` for all offline specifications.

## Embed a flywheel in an application

The library owns optimization, artifacts, and a restart-safe, text-free
`JsonlRunLedger`; an application owns presentation. Each completed measured
round becomes a typed `FlywheelRound`, containing only fingerprints, counts,
trial outcomes, call accounting, promotion status, active decision-element
definitions, and structured feature-proposal lifecycle records. On restart, any client
can call `ledger.status(current_feedback_fingerprint)` and render one of three
states: `never-run`, `current`, or `stale`.

This is the UI boundary for a terminal, web application, or service. It does
not expose prompts, source records, labels, or private model reasoning. A UI
can show current policy and features from its frozen artifact, then show the
ledger's measured candidate trials, outcomes, and refresh state. The local
ArXiv reviewer below is one client of this interface, not a second flywheel.

## Try the local article reviewer

The reviewer is a local Rich terminal application for collecting real human
`include` / `exclude` decisions on article records. It shows the title,
abstract, submission date, arXiv categories, author line, and—when arXiv has
it—the free-form publication citation. It makes no provider model calls. Its
SQLite event history preserves votes, comments, skips, undo operations, and
the exact prediction shown before each vote; its deterministic train,
rolling-audit, and permanent-audit assignments are not displayed while you
review.

Install the optional UI dependencies, create a local batch from the pinned
public arXiv metadata snapshot, then review it:

```bash
.venv/bin/pip install -e '.[reviewer]'
make PYTHON=.venv/bin/python review-arxiv
```

The first run samples 250 recent CS abstracts into ignored `var/` files, records
the precise source revision, then opens the reviewer. Use this local collection
mode until there are at least three Include and three Exclude training votes.
After that, resume the same queue with `make PYTHON=.venv/bin/python review`:
it runs one bounded, measured Jev core round and serves the selected frozen
policy. `make PYTHON=.venv/bin/python review-local` is the explicit no-provider
fallback.

Use `I` to include or `E` to exclude; each immediately offers an optional
comment field. Use `S` to skip, `B` to undo the latest action, and `Q` to save
and leave. Before each vote it shows a transparent local prediction: a 50/50
cold-start prior initially, then a lexical baseline trained only on eligible
human labels. The exact displayed prediction is linked to your vote for later
alignment analysis. The screen also reports prediction agreement, eligible
human labels, and baseline-refresh count after every decision. The optional source download records its exact Hub
revision and selection parameters locally; article text and your review data
are not committed.

## Measured Jev flywheel

The reviewer is only the feedback surface. The reusable core performs the
actual flywheel round: it converts only train-assigned human votes (including
optional comments as demonstration-only context) into trusted core items,
evaluates the core's incumbent, hard-swap, and random-control example policies
on a sealed development subset, and writes a text-free report plus a frozen
artifact. Rolling and final audit labels are never candidates, examples, or
development targets.

This is paid work and therefore always requires the explicit `make review` or
`make run-flywheel` command:

```bash
make PYTHON=.venv/bin/python run-flywheel  # evaluate and freeze a policy
make PYTHON=.venv/bin/python review-live   # serve the already-frozen policy
```

The regular review screen displays the latest measured candidate outcomes. A
live review session uses the selected frozen Jev artifact for each prediction;
it has a per-session request ceiling and records the prediction shown before
your vote. Its run ledger preserves measured rounds across restarts and marks
the policy stale when new eligible feedback arrives. After collecting more
feedback, run another flywheel round before serving a new live artifact.

## Safety and evaluation boundaries

Candidates and development labels must be trusted, canonical, and disjoint by
ID and normalized text. Context policies cannot return forged, duplicate, or
unbalanced examples. The optimizer accepts protected IDs and normalized-text
hashes so a fresh held-out scoreboard can remain entirely outside the search.
Freeze a policy with development data before opening that scoreboard; never pass
scoreboard labels to the optimizer.

`max_model_calls` counts attempted logical model decisions, including failures.
With caller-supplied cumulative call accounting, a search fingerprint prevents
resume against a changed task, pool, split, trial set, or ordering. Identical
complete requests may replay from a caller-owned checkpoint, so physical calls
can be fewer than trial-by-target evaluations. Jev and Kev adapters disable
hidden retries; callers should keep any retry policy outside the counted
optimizer loop.

Context token accounting is a deterministic whitespace estimate over the full
serialized request. It is not provider-reported usage and is not a substitute
for an actual provider token limit.

## Feedback, learned heads, and scripted steering

`FeedbackItem` records a reviewed final label and its selection propensity.
`HeadRow` combines that feedback with finite extracted features, and
`fit_learned_head` accepts only complete declared feature coverage. It fits on
trusted training rows, calibrates from out-of-fold predictions, and retains
text-free task, split, policy, context-artifact, source-model, and selection
provenance. Development and scoreboard IDs are supplied as firewall metadata;
they are not training rows.

`run_steering_round` is an offline, human-reviewed structural loop. An analyst
factory receives a `ScriptedMockManager` only after the mock is installed, so
tests can use recorded replies without constructing a provider client. The
analyst may propose one allowlisted scorecard element or context policy; it
cannot propose weights or calibration. The application-owned fitter receives
the candidate scorecard, its exact policy, and all declared candidate features.
The resulting core head must match that lineage before the application-owned
development metric can promote it. Protected scoreboard IDs must be recorded in
the fit provenance and cannot be used for fitting. This is a small provider-
neutral API, not a Tactus integration or a live steering service.

The steering briefing also names one safe, programmatic dynamic element:
`current_datetime`. An analyst may propose it with
`{"add_programmatic_element":{"kind":"current_datetime"}}`. Once normally
accepted and promoted, request construction can pass a recorded,
timezone-aware UTC value as `state.current_datetime`. It is never silently
injected, and it is context—not a learned weight, calibration value, or label.

## Optional dynamic retrieval (off by default)

The default product is one fixed, optimized example list. Per-item retrieval is
a separate switch: `RetrievalConfig` (the `retrieval:` block) defaults to
`enabled: false`, and `build_retrieval_policy` then returns `None`. When
enabled it returns a `PerLabelRetrieval` context policy that works with
`build_context_plan` like `PerLabelLexicalRetrieval`: `k` nearest examples per
label, never the target or protected items.

- `LexicalRetriever` (lexical v2): stop words on by default (negations kept),
  Unicode tokens, and `binary-cosine`, `tfidf-cosine` or `bm25` weighting.
  Lexical v1 is unchanged.
- `EmbeddingRetriever`: an injected `embed(texts) -> vectors` function, a
  text-free vector cache, and exact in-memory cosine search (`[retrieval]`
  installs numpy for speed). `HashingEmbedder` is an offline fake;
  `OpenAIEmbedder.from_environment()` (`[openai-embeddings]`) is live and paid.
- `S3VectorsStore` and `DynamoDBVectorStore` are design-only stubs.
- `neighbour_label_purity` compares retrievers at no cost.

## Adapters and evidence

The core package is provider-neutral. Optional extras are explicit:

- `decision-flywheel[jev]` pins `typesafe-sdk>=0.7,<0.8` and `python-dotenv>=1,<2`.
- `decision-flywheel[kev]` pins `httpx>=0.27` for the documented local endpoint.
- `decision-flywheel[laya]` supports zero-shot local use through
  `LayaAdapter.from_default(configuration=LayaConfiguration(revision="..."))`.
  Laya has no documented labelled-demonstration semantics, so few-shot context
  is explicitly excluded even when a revision is pinned.

Adapters are tested with injected fakes. The walkthrough does not validate a
live adapter or claim live-model performance. Experimental notes, manifests, and
cross-model evaluation material live in
[Decision-Flywheel-Evaluations](https://github.com/AnthusAI/Decision-Flywheel-Evaluations).
The earlier [Few-Shot-Jev experiment](https://github.com/AnthusAI/Few-Shot-Jev)
contains experimental findings about its own dataset and budgets; it is research
context, not a claim that a selector always beats every context size or model.

## Repository tooling

This repository uses [Kanbus](https://github.com/AnthusAI/Kanbus) for Git-backed
project tasks and `python-semantic-release` for conventional-commit releases.
Install the local tooling with `make install-tools`, then use `kanbus list` to
inspect the board.

## License

MIT for this repository's code. Downloaded datasets and model weights retain
their upstream terms.
