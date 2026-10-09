# Persistent web workspace

## Goal

Use one reusable DecisionFlywheel engine. The web app is an application of that
engine, not another optimizer. Keep the existing timeline, metrics, and exchange
inspector. Add run history and human labeling around them.

## Architecture

### Workspace identities

Top-level navigation is Cyclotrons, Item lists, and Optimizations. Each cyclotron
definition pins ordered classifier revisions and shared settings. A classifier
has a stable identity and immutable configuration revisions, an ordered class
list, a decision question, and optional positive/negative roles. Binary and
multi-class configurations are both valid. An item list is source-neutral;
imports use stable item identities, UTC source timestamps and immutable content
revisions. Classifier labels and explanations attach to a classifier revision
and an item revision, not universally to the item. Corrections append history.
The same item can receive independent labels for any number of classifiers.

Prediction transport is not one request per classifier. Compatible classifiers
can share a decision-model request for the same target. Each contributes its
main question and discovered supporting questions. The shared state namespaces
each classifier's rubric, examples and dynamic elements. Question bindings map
provider output keys back to classifier and local question identities. Each
classifier's learned head uses only its own mapped features. Usage is counted
once for the shared exchange, not multiplied by the number of classifiers.
Provider/model compatibility and context budgets determine batch boundaries;
an oversized batch is rejected before a model call, never silently truncated.
Cache identity must include the entire shared request and all configuration
fingerprints. A changed classifier cannot reuse an unrelated previous answer.

The catalog, independent-label UI/API, external arXiv item updater, shared Jev
transport/cache, and general interactive session coordinator are implemented.
Run setup selects a frozen item list and classifier revisions. Each classifier
uses the existing DecisionFlywheel, its own feedback, head, and optimization
state. Preparing an item shares a request; explicit labels and explanations
drive the corresponding classifier's learning triggers through API-recorded
cycles. Tests cover shared calls, restart reuse, independent feedback, and
GraphQL trace ingestion. The old arXiv adapter remains for existing runs.

The shared lifecycle is also checked through the actual Jev, Kev, and Laya
adapter wire contracts with injected clients:

```bash
.venv/bin/pytest tests/shared_provider_lifecycle_test.py -q
```

Each case uses two scoped classifiers, a supporting question, a calibrated
learned head, and real coordinator caches. Sibling rubric changes, refreshed
training answers, and legacy heads without response provenance cause a bounded
refit before the next joint prediction. The tests check full confidence vectors,
consumed feature columns, one-time transport usage, preserved rubric/examples,
same-cycle invalidation, and restart reuse without recollection. This verifies
the reusable transport/coordinator contract; it does not install optional local
checkpoints or demonstrate their predictive quality.

Labels saved directly in the catalog do not trigger session learning. Interactive
cyclotron sessions support corrections and item-level undo through durable API
commands. Corrections retain the original prediction and append feedback history;
dependent inferred guidance and fitted heads are invalidated. Undo retracts local
labels and reopens the saved prediction without calling either model. It does not
restore a superseded label or erase prior exposure. Inherited source labels and
frozen replay feedback remain read-only. Existing runs freeze their item queue:
refreshed lists supply new runs, not silently changed history.

The correction drawer retains unsent drafts by run and item. A stale feedback
identity blocks submission until the human reloads the saved feedback. Failed or
interrupted correction/undo commands offer explicit recovery with the original
command identity. Recovery cannot resume paid-capable commands, interleave with
pending work, or reactivate an inactive edition. See
[cyclotron versioning](cyclotron-versioning.md) for the API and storage contract.

Run history defaults to the inspected run's cyclotron family. GraphQL
`Run.cyclotronId` resolves that membership from the persisted cyclotron-version
relationship before consulting the original configuration. This lets the initial
run and later editions appear together without rewriting a frozen run snapshot.
Search and the explicit All cyclotrons filter remain available. Inspecting an
inactive edition is read-only; activation is a separate, explicit action.
Cyclotron and nested classifier controls use at least 44-pixel touch targets on
narrow screens or coarse-pointer devices. The settings editor scrolls its fields
while keeping its Save and Cancel footer visible.
The viewport shell reserves the device's safe-area insets on all four sides.
App and exported playback pages use `viewport-fit=cover`; native zoom is not
disabled. Device keyboard and gesture checks complement the automated layout
specs; a desktop browser with zero insets does not prove physical notch behavior.

`scripts/update_arxiv_items.py` is an external example importer. It refreshes a
local SQLite mirror from normalized or raw arXiv JSONL and upserts chronological
pages through GraphQL. A Hugging Face JSONL download resolves and records an
immutable revision first. Repeating imports is idempotent; changed items retain
their old revisions and labels. The importer never starts optimization or
constructs decision-model clients. Dataset retrieval stays out of the library.

A run is one execution of the flywheel, not a synonym for an optimization call.
It has one of two input sources:

- Interactive: predictions receive new human labels, explanations, skips, and
  corrections through the labeling UI.
- Replay: a frozen snapshot supplies the existing labels and explanations in
  their recorded arrival order. Each parameter experiment starts a separate run.

Both paths use the same predict → feedback → trigger checks → optional learning
cycle logic and the same API event contract. They use the same timeline and
request/response inspector. Input source, frozen label revision, model settings,
learning cadence, objective, and parent run belong to run metadata, not invented
events. Do not confuse read-only playback of a completed run with executing a
new replay. Browsing a run must never start paid work.

The application supports interactive labeling and in-app frozen-feedback replay
creation, stepping and run/pause controls. Both use the same command worker and
API traces. Creating a replay is atomic and makes no provider calls; advancing it
does, within explicitly approved budgets. The timeline refreshes on completed
replay cycles. Offline restart/pause specs verify chronological replay across
restart, frozen explanations and no automatic continuation after failure. Browser
replay controls have also been exercised against a disposable fake-model API:
step, run, pause, reload and explicit resume preserve completed cycles and labels.
This is technology acceptance, not experimental evidence for classifier quality;
imported history alone does not prove a newly executed replay.

- React and existing shadcn components provide the single-page workspace.
- FastAPI serves the app. Strawberry provides GraphQL queries, mutations, and
  subscriptions. SQLite holds the run catalog, items, jobs, and trace history.
- Each run has an immutable ID, configuration, objective, dataset identity, and
  independent state/cache. Runs do not overwrite earlier runs.
- An optimization runner submits events through the GraphQL ingestion mutation.
  The API acknowledges them only after SQLite commits them. Retries use an
  event identity and cannot duplicate events. Subscriptions read committed events
  after a cursor, so reconnecting cannot lose the history.
- Exact optimizer and decision requests/responses, configuration snapshots,
  triggers, human feedback, fit results, and metrics use this path. No separate
  trace export file is required. Internal engine state/cache is not the app's
  event source. A reusable API sink supports other applications and runners.
- Labeling shows recorded optimization phases as events arrive. Inspect activity
  opens the correlated full request, response, tool calls, and applied before/after
  snapshots. A proposed rubric is not an applied rubric. Routine no-op trigger
  checks do not hide the last outcome. Saved-label acknowledgement is separate
  from optimization success; failed commands stay stopped until explicit recovery.
- Label submission creates a durable command. A single worker serializes each
  run's cycles. The API remains available while model calls run. Predictions
  precede labels; triggered optimization completes before the next prediction.
- Human explanations are part of eligible learning feedback. Protected audit
  comments must not enter the optimizer context. Undo retains the original vote
  and records a retraction; it does not erase history or hide prior exposure.

## First delivery

1. API-owned, append-only event ingestion and SQLite run history, tested without
   network calls. Reusable GraphQL event sink with acknowledgement/idempotency.
2. Import existing recordings as explicitly recorded runs. Never claim that
   those old recordings are newly streamed or retrained.
3. Web run list, run selection, and the existing timeline inspector backed by
   stored API events. Stream active-run updates from a durable cursor.
4. arXiv labeling page: title, abstract, date, categories, authors, journal
   reference, prediction, Include/Exclude/Skip, optional explanation, and undo.
5. Connect durable commands to the reusable engine. Show cycle progress,
   optimizer exchanges, model fitting, and current configuration live.
6. Add replay execution controls around that same engine, using a frozen label
   snapshot and explicit budgets. Keep historical playback independent of the
   execution worker. Both input sources use the same run-history UI.

## Safety and comparison

Local-first, one worker process, no public deployment. LAN access can use a secret;
this trusted-LAN demo explicitly runs with `--allow-unauthenticated-lan` at the
user's request. Do not expose it on a public network. Same-origin checks remain
in place and no browser receives provider keys. Creating a live run
requires explicit confirmation, decision-request ceiling, and optimizer-call
ceiling. Failed/interrupted paid work never resumes silently.

Interactive sessions default to a 1,000-call optimizer ceiling rather than a
ten-call demonstration budget. Limits remain finite and explicit. The GraphQL
`updateRunLimits` mutation requires confirmation, preserves classifier and label
history, and records the before/after operating limits. The serialized worker
applies updated limits before the next command. An explicit `optimize` command
performs a catch-up pass over rubric, questions, examples, and classifier-head
stages using already recorded feedback, without submitting votes again. Future
feedback then uses the normal transition and cadence triggers. Hitting a ceiling
pauses learning separately from successful label storage.

After the web cycle is verified, compare recall-primary/accuracy-secondary and
F1-primary as separate runs. Freeze the same labels, arrival order, initial empty
configuration, partitions, ceilings, and model identifiers. Use development
labels for optimization and a matched protected audit for comparison. Report
accuracy, precision, recall, F1, counts, uncertainty, and usage for both policies.
Do not select the winner by repeatedly tuning on that protected audit.

## Acceptance checks

Tests use fake decision and optimizer clients. Verify event ordering, durable
reconnect, ingestion retries, run isolation, vote/retraction provenance, review
before learning, audit separation, limits, and full exchange detail. Verify the
browser path: choose a run, prepare an item, see the prediction, label/comment,
watch triggered optimization, click its exact request/response, reload, and see
the same recorded history. A static mock is not completion of the live demo.
