# Replicate harness

`scripts/replicate_study.py` runs K replicates of `scripts/run_editorial_prequential_study.py` with the same
arguments and summarises the run-to-run spread, so a later change can be compared with a baseline.

    python scripts/replicate_study.py --base-args-file study-args.txt --replicates 5 \
        --out-dir runs/baseline --parallel 2 \
        --max-total-optimizer-calls 300 --max-total-decision-calls 40000

`--dry-run` prints the exact commands and runs nothing.

- `--base-args-file`: the study's own arguments (shell-style text with `#` comments, or a JSON list of strings),
  without `--output` and `--seed`. Include `--confirm-live` there if the replicates should spend; the harness adds nothing.
- `--seeds a,b,c` (verbatim, sets K) or `--replicates K` with `--base-seed S` giving seeds `S:rep-1` ... `S:rep-K`
  (default base `editorial-prequential-v1`). Each replicate writes to `DIR/rep-N` (log in `DIR/rep-N.log`).
- `--parallel P` (default 2), `--window-size` (default 70), `--mode` (study mode directory to read; default the
  first with a `results.json`).
- `--resume`: replicates whose output is complete are skipped (and counted against the global caps from their
  ledgers). A partial replicate is reported as incomplete and not relaunched or counted. Without `--resume`,
  existing replicate directories are refused.

## What a seed varies

In the study script the seed feeds the replay plan (`build_plan`: the hash order of stories within each class, and
the label-independent development/training split when `--development-rate` is set) and the feedback policy
(which labels are revealed under selective policies). It does not change the decision model or the optimizer's
sampling, so remaining variance there is the services' own nondeterminism.

## Global cap guard

`--max-total-optimizer-calls` / `--max-total-decision-calls` bound all replicates together. The per-run caps are
read from the base args (`--max-optimizer-calls`, default 60; `--max-decision-calls`, default 6000). Before each
launch: finished replicates count at the calls consumed in their `global_budget_ledger.json` (the per-run cap if
the ledger is missing), running replicates and the candidate count at the per-run cap; if the total would exceed
a global cap nothing more is launched. Any non-zero exit also stops further launches (running ones finish).
The guard is conservative by design, so runs that consume far less than their caps can still be blocked.

## Environment and secrets

The harness never reads `.env` files and never prints or writes environment values. Child processes inherit the
caller's environment, so export keys in the shell that launches it. No key value is written to any output file.

## Metrics (per replicate, percent)

Reference labels come from the operational corpus (`operational_corpus` in `protocol.json` if recorded, else the
`--operational-corpus` in the base args; id key, label key `simulated_label` or `label`), because `results.json` holds a
human label only for reviewed stories. Without a readable corpus the recorded labels are used and the summary says
`recorded (reviewed stories only)` when some are missing.

Computed read-only from `results.json` and `runtime.sqlite3` over the first floor(cycles/window) x window cycles:
cyclotron accuracy per window (`predicted_label` vs `human_label`); decision-model-alone accuracy per window
(`prediction` events' `decision_model_label` vs the human label); majority-class baseline per window (the share of
the most common human label, the best constant answer); first `classifier-activated` cycle and the number of
activations; whether a fitted head was used and the share of predictions with `fitted_head`; Jev and optimizer
requests from the ledger; post-warm-up accuracy (mean of windows 2..last); final-window accuracy; area under the
learning curve (mean of all window accuracies).

Aggregate (complete replicates only): median, min, max and a seeded bootstrap 90% interval of the median
(2000 resamples) for final-window and post-warm-up accuracy; reliability = share of replicates whose final window
beats its majority baseline by at least 5 points, and the minimum post-warm-up accuracy. Output:
`replicates-summary.json` and `replicates-summary.md` in `--out-dir`.
