# Recording catalog

`scripts/recordings.py` keeps every recorded editorial run as a separate, described recording and publishes only the
few that explain something. No model calls, no keys, no `.env` reads.

A recording is `<catalog>/<id>/recording.json` (schema `cyclotron-recording/v1`) plus `fixture.json`, the schema-2
playback export made by `scripts/export_editorial_run_fixture.py`. `recording.json` holds the title, summary,
"what to notice" bullets, tags, status (`draft`, `published`, `retired`), `featured`, the setup read from the run's
`protocol.json`, per-window accuracy (cyclotron, decision model alone, majority baseline, all as fractions), the first
rubric cycle, cost, `fixture_sha256`, and a local-only `source` block (run directory, replicate set) that export drops.

    python scripts/recordings.py add RUN_DIR --id blank-start-first-run --title "..." --summary "..." \
        --notice "..." --notice "..." --tag blank-start --tag jev \
        --corpus-manifest corpus_v3_manifest.json --corpus corpus_v3_operational.jsonl \
        --cycles 280 --window-size 70 --optimizer-price gpt-6-luna --decision-model-price jev \
        --engine-commit SHA --catalog recordings/
    python scripts/recordings.py list --catalog recordings/ [--status draft] [--tag jev]
    python scripts/recordings.py compare ID1 ID2 --catalog recordings/
    python scripts/recordings.py publish ID [--featured] --catalog recordings/
    python scripts/recordings.py export-published --to ../site/recordings --catalog recordings/

`--catalog` may be replaced by `$CYCLOTRON_RECORDINGS_CATALOG`. Other commands: `show`, `unpublish` (back to draft),
`retire`, `unfeature`.

## Defaults

- `add` always writes a draft and refuses an existing id unless `--replace`. The window size defaults to 70 (the
  replicate harness's); `--cycles` defaults to the whole run and must be a multiple of the window.
- Prices default to the run: `jev` when the protocol's provider is `jev`, and the protocol's optimizer model when
  the exporter knows its price; otherwise the exporter's default.
- The starting rubric is `blank` (empty), `curated` (equal to the study's `BASELINE_RUBRIC`) or `custom` (text kept).
- Label provenance comes from the corpus manifest's `label_provenance`; older manifests use the fixture's disclosure.
- The engine commit is `--engine-commit`, else `protocol.json`'s `engine_commit`, else `null` with a warning.
- Setup parameters are read from the protocol (`rubric_trigger`, `selection_policy`, `idea_screening` when
  enabled). The study's fixed `optimize_every` and `retrain_every` are not in `protocol.json`, so they appear only
  if a protocol records them.

## Publishing

`publish` refuses a recording without: a title and summary; at least one notice bullet; label provenance; a cost
total and price basis; a majority baseline on every window; a `fixture_sha256` that matches `fixture.json`.
Publishing with `--featured` clears `featured` on every other recording; unpublishing or retiring clears it too.
A retired recording must be unpublished before it can be published again.

`export-published` validates again, then writes each published recording (without `source`) and its fixture to
`DEST/<id>/` and `DEST/index.json` (`{"schema": "cyclotron-recordings-index/v1", "recordings": [...]}` with id, title,
summary, tags, featured; featured first, then newest `created`, then id). Output is sorted-key JSON so re-running
changes nothing. It writes a `.cyclotron-recordings` marker; only when that marker already existed does it remove
directories holding a `recording.json` that are no longer published. An unmarked directory is never cleaned.

`setup.review` records how much of the run a person reviewed: `{policy, summary, stories_reviewed, stories_total}`.
`policy` is the run's feedback mode (`all`, `reject_half`, `casual_ten_percent`, or any other recorded mode), read from the
exported scenario's feedback policy, then the protocol's single `modes` entry, then the mode folder name. `stories_reviewed`
counts the recorded cycles whose label was revealed to the learner; `stories_total` counts the recorded cycles. The block is
optional: older recordings without it mean every story was reviewed. `list` shows the policy and count.

`compare` prints the setup settings that differ (including `review.policy`) and each window's cyclotron / decision-model / baseline accuracy.
