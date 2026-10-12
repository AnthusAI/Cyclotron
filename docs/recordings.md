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

## Comparison-ready recordings

New recordings (and old ones after `upgrade`) carry what is needed to compare two decision models rigorously. Every field
below is optional, so older recordings still validate, list and publish.

- `fixture.json`: each cycle's `article` has the corpus story `id` (extra key; the site reads only named fields).
- `setup.corpus.sha256`: the corpus manifest's `operational_sha256` (or `sha256`).
- `setup.decision_model_detail`: `{name, revision, provider, hosting}`. `revision` comes from `--decision-revision` in the run's
  `<run>.card.json` (null when absent); `provider` is the protocol's `decision_provider`; `hosting` is `local` for `kev`, else `hosted`.
- `benchmark`: `{id, fingerprint, inputs, comparable[, reason]}`. `fingerprint` is the sha256 of the canonical JSON (sorted keys,
  compact) of exactly these inputs, which are also stored in `inputs`: corpus sha256, window size, review policy, rubric trigger
  (`rubric_changes_every`, basis, `max_rubric_optimizations`), optimizer model, selection policy primary, starting rubric kind
  (`blank`, `curated`, `custom`), and the number of cycles exported. `id` is `<corpus id>-<first 8 hex of fingerprint>`.
  The decision model, its revision and provider are deliberately NOT in the fingerprint: that is the factor that varies, so two
  recordings with the same id differ only in the decision model (and any rubric the optimizer wrote). `comparable` is false, with a
  `reason`, when the corpus sha256 or the story ids are unknown.
- `results`: each window also has `final_publish` and `raw_publish` (`precision`, `recall`, `f1` for the positive class `publish`),
  and `results` has the same two blocks overall plus `final_accuracy` and `raw_accuracy`. "Final" is the cyclotron's decision;
  "raw" is the decision model's own answer (its most probable label, first declared on a tie). Values are fractions rounded to 3
  places, or null when undefined. Scored against the fixture's `actual` label, so stories whose label was not revealed
  (selective review) are left out of these numbers.
- `predictions.jsonl` (beside `recording.json`): one row per exported cycle,
  `{n, id, gold, raw_label, raw_probs, final_label, final_probs, reviewed}`, derived from the fixture. `predictions_sha256` in
  `recording.json` pins it; `publish` and `export-published` check it when present, and export copies the file.

### compare

    python scripts/recordings.py compare A B --catalog recordings/ [--disagreements N] [--force]

It refuses (nonzero exit) unless both recordings have the same `benchmark.id`, listing the benchmark inputs that differ. When
comparable it prints the setup differences, per-window final / raw / baseline accuracy, cost, overall final and raw accuracy,
final and raw publish recall and precision, and a paired exact McNemar test (two-sided, exact binomial on the discordant
counts) on the correctness of final decisions and, separately, of raw answers, with the number of stories where the two answers
differ. Stories are paired by id. `--disagreements N` lists the first N stories whose final decisions differ, with each answer
and the gold label. `--force` compares different benchmarks anyway, with a loud warning.

### upgrade

    python scripts/recordings.py upgrade ID [--run-dir DIR] --catalog recordings/

Backfills the fields above from the recording's `source.run_dir` (or `--run-dir`) and regenerates `predictions.jsonl` from its
fixture. Missing story ids are taken from the run's `results.json`; if they cannot be found, `benchmark.comparable` is false with
a reason. It never changes accuracy numbers, notices, status, cost or the fixture, and running it again changes nothing.
