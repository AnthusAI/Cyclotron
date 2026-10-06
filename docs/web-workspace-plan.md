# Persistent web workspace

## Goal

Use one reusable DecisionFlywheel engine. The web app is an application of that
engine, not another optimizer. Keep the existing timeline, metrics, and exchange
inspector. Add run history and human labeling around them.

## Architecture

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

## Safety and comparison

Local-first, one worker process, no public deployment. LAN access needs a secret
and same-origin checks. No browser receives provider keys. Creating a live run
requires explicit confirmation, decision-request ceiling, and optimizer-call
ceiling. Failed/interrupted paid work never resumes silently.

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
