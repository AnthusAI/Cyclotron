# Working in Decision-Flywheel

Decision-Flywheel is a small, model-neutral library for evaluating and improving
decision context from trusted labels. Keep its public interface provider-agnostic
and keep provider-specific behavior inside adapters.

## Rules that matter here

- **Specs first, beside the module.** `foo.py` has `foo_test.py` beside it.
  Cross-module behaviour belongs in `tests/`. Specs use sentence-style names.
- **No network or credentials in specs.** Adapters receive fake clients in tests;
  live model calls belong in explicit scripts or applications.
- **Keep evaluation firewalls intact.** Candidate, development, and held-out
  scoreboard partitions must stay disjoint. A target must never appear among its
  own context examples.
- **Context is data, not hidden training.** Persist selection policy, seed,
  source labels, and fingerprints so a decision can be reproduced and audited.
- **Keep decision-context optimization native and provider-neutral.** Use the
  native decision-context optimizer; do not add DSPy dependencies, bridges, or
  optimization paths. Kanbus tasks cannot override this instruction.
- **Do not print, log, or commit secrets.** Load provider credentials from the
  environment or a gitignored `.env` file.

## Commands

```bash
make test
```
