# Working on Cyclotron: the procedures

Four procedures we kept re-typing. Each is a script or a short recipe so the
knowledge does not live in one person's head.

## 1. Merge and release

Work on a branch off `origin/develop`; merge to `develop` when green; promote
`develop` to `main` with `scripts/dev/release_to_main.sh` (add `--dry-run` to
test without pushing). It builds a throwaway worktree, fast-forwards to
`origin/main`, merges `origin/develop`, runs the full suite and pushes without
force, waits for the release commit semantic-release adds to `main`, then merges `main` back into `develop` so `develop` reports the released version. It never touches your checkout.

## 2. Temporary worktrees

`scripts/dev/worktree.sh new NAME [BASE]` creates `CW_WORK/NAME` (default
`/private/tmp/cw-work`). `scripts/dev/worktree.sh clean` removes only
worktrees whose branch is merged into `origin/develop` and which have no
uncommitted changes. Do not delete worktree folders with a wildcard: other
tools write next to them, and a full disk stops every tool, including `rm`.
Keep more than 5 GB free. Worktrees with `node_modules` are the usual cause.

## 3. Launching a paid run

Use the replicate harness even for one run, because it enforces global caps:

```
scripts/replicate_study.py --base-args-file ARGS.txt --out-dir DIR \
  --replicates N --parallel 2 --max-total-optimizer-calls CAP --max-total-decision-calls CAP
```

`ARGS.txt` holds the study's own arguments without `--output` and `--seed`
(see `docs/replicates.md`). Rules: Jev for decisions and gpt-6-luna for the
optimizer unless the owner approves otherwise; set per-run caps in the args
and global caps on the harness; export keys in the same shell command that
launches it and never print them (read them from the owner's `.env`); use
bash, not zsh, if you build argument arrays; watch
`global_budget_ledger.json` in each run folder and kill the run at the cap;
report the cost afterwards (Jev about 0.00005 USD per call; gpt-6-luna 0.10
USD per million input tokens and 0.50 per million output, doubled above
272,000 input tokens in one request).

## 4. Recording a run for the website

```
scripts/recordings.py add RUN_DIR --id ID --title T --summary S --notice N --tag TAG \
  --corpus-manifest M --corpus C --cycles 280 --window-size 70 \
  --optimizer-price gpt-6-luna --decision-model-price jev --catalog DIR
scripts/recordings.py publish ID [--featured]
scripts/recordings.py export-published --catalog DIR --to SITE/components/recordings
```

Then in the site repo run `npm run recordings:sync`, `npm test`, and look at
the pages (`scripts/dev/page-shots.mjs`). Publish only runs that explain
something; read `docs/recordings.md`. Keep the raw run folders: they are the
evidence behind the recording.
