# Working on Cyclotron: the procedures

Four procedures we kept re-typing. Each is a script or a short recipe so the
knowledge does not live in one person's head.

## 1. Merge and release

Work on a branch off `origin/develop`; merge to `develop` when green; promote
`develop` to `main` with `scripts/dev/release_to_main.sh` (add `--dry-run` to
check without pushing). It builds a throwaway worktree, fast-forwards to
`origin/main`, merges `origin/develop`, runs `make check` and pushes without
force, waits for the release commit semantic-release adds to `main`, then merges `main` back into `develop` so `develop` reports the released version. It never touches your checkout.

`make test` is the fast loop: it runs `pytest -m "not slow"`, which skips the
long replay, study and editorial-corpus tests marked `@pytest.mark.slow` (or a
module-level `pytestmark`). `make test-all` runs the whole suite. Mark a new
test slow when it takes more than a few seconds; `pytest --durations=30` shows
which.

`make check` is the gate, because CI for this private repository runs locally
and `.github/workflows/release.yml` is kept only for the semantic-release job.
It runs, in order: `make test-all` (the slow tests included); `npm ci`, `npm test` and
`npm run build` in `trace-ui`; then `git diff --exit-code` on
`src/decision_flywheel/vendor/trace-ui`, which fails when the committed bundle
differs from a fresh build. If it fails on the bundle, commit the rebuilt files.
Ruff joins the front of the sequence once the lint setup lands.

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

## Kanbus in cloud sessions

`.claude/settings.json` runs `scripts/dev/ensure_kanbus.sh` at session start. When `kbs` is missing it installs the `kanbus` version pinned in the `tools` extra of `pyproject.toml` into a dedicated venv (`~/.cache/cyclotron/kanbus-venv`, override with `KANBUS_VENV`) and symlinks `kbs` into `~/.local/bin` (override with `KANBUS_BIN_DIR`); a venv avoids pip failing on distro-owned packages such as Debian's PyYAML. An existing `kbs` is left alone, with a warning if its version differs from the pin. If that directory is not on `PATH`, the script appends one `export PATH=...` line to `$CLAUDE_ENV_FILE` so the session's later Bash commands find `kbs` (without `CLAUDE_ENV_FILE` it prints a hint to stderr instead). The hook discards stdout but keeps stderr, so failures are visible. Use `kbs` commands for the board; never edit `project/issues` or `project/events` by hand.
