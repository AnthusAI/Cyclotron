---
name: run-study
description: Run, monitor, score, record and publish a Cyclotron study (editorial prequential run, replicate batch, or a variant with a different decision model, optimizer, provider, review schedule or corpus) so it can be replayed later and published to the Cyclotron site. Use when asked to run a study, run an experiment, compare models or providers, launch an optimization run, or publish a run as a recording or use case.
---

# Run a study so it can be replayed and published

Paths are relative to the Cyclotron repo root. Driver files: `.agents/skills/run-study/launch.sh` (capped background launch plus a study card) and `.agents/skills/run-study/score.py` (scores a run from its event log). The engine script is `scripts/run_editorial_prequential_study.py`; replicate batches use `scripts/replicate_study.py` (`docs/replicates.md`); the catalog and publishing flow is `docs/recordings.md`.

## Rules that apply to every study

- **Spend.** Jev for decisions and gpt-6-luna for the optimizer are pre-authorised if capped and monitored. Kev is a local model and costs nothing, so it needs no approval. Any other paid provider or model (Bedrock, any other cloud LLM) needs the user's approval naming the model and the cap before the first paid call. Local models still get a study card and the same caps.
- **Parity.** Change one thing per study and say what it is. Every engine and baseline gets the same corpus, order, review schedule, and caps. No extra help for a baseline.
- **Honest scoring.** Score against the corpus reference labels, never against the human labels in `results.json`, which exist only for reviewed stories.
- **Keys.** `launch.sh` reads named variables from a keys file with `grep` and never prints them. Never `source` a `.env`: a bare line in it gets echoed in the error. Never commit keys or write them into a card.
- **Disk.** Keep more than 5 GB free; `launch.sh` refuses to start below that. One run's database is about 1 GB, and a full disk mid-run loses the results file.
- **Scratch.** Delete only your own files. Never wildcard-delete shared temp directories.

## 1. Write the study plan first

Create `STUDY.md` beside the run directory, before spending anything. It states: the question, the one thing that varies, everything held fixed (corpus path and sha256, decision model, optimizer, review schedule, trigger, seed), the caps, the expected cost, and what result would count as an answer. Replay depends on this file and on the card in step 2.

## 2. Launch

Pick an output directory that does not exist yet. Pass the caps; `launch.sh` refuses to start without both.

```bash
S=~/Documents/Codex/2026-10-09/editorial-study
.agents/skills/run-study/launch.sh $S/optimization/NAME \
  --operational-corpus $S/relabel2/corpus_v4_operational.jsonl \
  --operational-sha256 a26f4f0fa1af27e3b83bb39d4ebe3359a8c44a889c6e06c753e27f9bf236e660 \
  --bootstrap-corpus $S/relabel/bootstrap_v3.jsonl \
  --bootstrap-sha256 78f673c06d35cb5f0780ce10552a7d86f664f0b41abc472337a4febeaa29b675 \
  --model gpt-6-luna --decision-provider jev --decision-model jev-1.13.0 \
  --modes all --operational-items 853 --max-decision-calls 6000 --max-optimizer-calls 20 \
  --rubric-changes-every 6 --baseline-rubric none
```

It prints the process id, the log path and `NAME.card.json`. The card records the engine commit, whether `src` or `scripts` had uncommitted changes, Python version and the exact flags. Commit the engine change first so the card names a real commit. It also writes `NAME.environment.json` (engine commit, dirty flag, Python version and, for `--decision-provider kev`, the JSON body of `GET $KEV_BASE_URL/v1/models` captured at launch, or a note that the server was unreachable); keep it with the run so the recording can name the exact model server.

`--operational-items` must equal the corpus row count, and the sha256 must match, or the script stops at once. For a smoke test, cut a small corpus with `head`, hash it, and use a tiny cap such as 300 Jev calls and 2 optimizer calls. A 40-story test cost a few cents and finished in under a minute.

## 3. Monitor

The log and the budget ledger only update at the end of a run. To watch progress, count answers and rounds in the run's database:

```bash
sqlite3 $S/optimization/NAME/all/runtime.sqlite3 "select count(*) from runtime_answers; select count(*) from runtime_rounds"
```

Stop the run if spend nears a cap before the work is done and report it; do not raise a cap without approval.

## 4. Score

```bash
.venv/bin/python .agents/skills/run-study/score.py $S/optimization/NAME/all $S/relabel2/corpus_v4_operational.jsonl --window 143
```

Prints per-window accuracy against the most-common-label baseline and the cycles where the rubric changed. It reads only the event log, so it works on a run whose `results.json` was lost. Compare against Jev alone with the same rubric on the same corpus; for v4 that baseline is in `relabel2/v4-jev-results.jsonl`.

## 5. Record, then publish

Follow `docs/recordings.md`: `scripts/recordings.py add` makes a draft from the run directory, `publish` validates it, and the site picks it up from `export-published`. Add a short plain-English summary and the "what to notice" bullets. Publishing is a site change: do it only when the user has asked for the study to appear, and describe the setup (decision model, optimizer, review schedule, corpus) exactly as run.

## Adding a new decision model

Comparing two decision models is only valid when they ran the same benchmark (`docs/recordings.md`, "Benchmark id"). Checklist:

1. Adapter: add a `--decision-provider` choice and an adapter for the model, with a test; no live calls in the test.
2. Price: add an entry to `PRICES` in `scripts/export_editorial_run_fixture.py` (use zero input and output prices for a local model) so `--decision-model-price` accepts it.
3. Identity: launch with `--decision-model NAME` and, if the model has a revision, `--decision-revision REV` so `add` records it in `setup.decision_model_detail`. Only `kev` counts as local hosting; add any other local provider to `decision_model_detail` in `scripts/recordings.py`.
4. Run the same benchmark: the same corpus file, window size, review schedule, rubric trigger, optimizer and starting rubric as the run you will compare against. Change only the decision model.
5. Record it with `scripts/recordings.py add`; the new recording must show the same `benchmark.id` as the baseline (`recordings.py show ID`).
6. Compare: `.venv/bin/python scripts/recordings.py compare BASELINE_ID NEW_ID --catalog CATALOG --disagreements 10`. It refuses a different benchmark.

## Gotchas

- **Crash at the end.** The study writes `results.json` only after the last story. A full disk produced an empty file after all 853 cycles had run. The write is now atomic, but if you meet an empty file, run `score.py`; do not rerun live.
- **Resume can fail.** A `--resume` of a run that stopped on an optimizer rate-limit error stopped with `training IDs must be unique`. Until that engine bug is fixed, rerun fresh, seeded from the old runtime with `--seed-answers-from`, and pass `--transport-retries 3`.
- **Resume does nothing for a finished run.** `--resume` skips modes whose cycles are complete, so it will not rewrite a lost report.
- **Seeding.** `--seed-answers-from OLD/all/runtime.sqlite3` serves identical decision requests from an earlier run at no cost. The optimizer is not cached and is not deterministic, so a rerun is a new run with new rubrics.
- **Output directory.** The script creates it itself and fails with FileExistsError if it exists, so keep logs beside it, not inside.
- **Unreviewed stories.** Accuracy for any selective-review schedule must be scored from corpus labels (see Rules).
- **Kev (local, free).** `--decision-provider kev --decision-model kev-latest` uses `KEV_BASE_URL`, default `http://127.0.0.1:8009`. Start the server from the pinned wrapper in the Biased-Decisions kev worktree, with the dedicated environment (build it once with `python -m venv ~/.cache/kev-venv` then `pip install -r var/kev-dependencies.txt`):

```bash
cd ~/Projects/Biased-Decisions/.worktrees/kev
PYTHONPATH=var/kev-runtime nohup ~/.cache/kev-venv/bin/python var/start_kev.py > /tmp/kev-serve.log 2>&1 &
curl -s localhost:8009/v1/models
```

  It answered in about 0.35 s per call on this machine. Kev takes no few-shot examples, and with the long v3.1 rubric it did worse than with no rubric on a 30-story probe, so judge it on its own optimization run. If the port is already serving, do not restart it.
- **Local optimizer (Ollama).** Ollama is installed (`ollama serve`, model `qwen3:4b`, 2.5 GB). The study script has no flag for it yet, and `adapters/litellm_optimizer.py` has a fixed 60 second timeout and cannot set the context size. A direct probe on the first optimizer prompt of the 853-story run (84 KB, about 21k tokens) took over ten minutes on this machine and the 4B model echoed the input state back instead of proposing a rubric. Later prompts grow to about 1 MB (250k tokens), far beyond a small model. A local optimizer needs a much shorter optimizer context and a larger model before it is worth wiring in. Lower priority.
- **Bedrock.** Not built. Needs an adapter, a test, and approval naming the model and cap.
- **zsh.** Do not put flags in a shell variable and expand it unquoted; zsh will not split it. Use a bash array, as in the launch example run under `bash -c`.
