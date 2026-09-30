# Decision Flywheel

Decision Flywheel is a model-neutral toolkit for improving structured classifiers from trusted human feedback. Its first feature is the **Decision Context Optimizer**: a policy that selects labelled examples for each new item without looking at that item's answer.

It is deliberately not a prompt-string optimizer. A policy is a versioned, testable object that chooses context from a trusted labelled pool; a decision model turns target plus context into a label and probability distribution.

## Evidence and scope

The companion [Few-Shot-Jev experiment](https://github.com/AnthusAI/Few-Shot-Jev) found that target-specific lexical retrieval improved macro-F1 over random fixed context at a fixed 96-example budget. That experiment is evidence, not a product claim. This repository extracts the general mechanism so it can be tested across decision models and datasets.

The earlier [Jev Flywheel](https://github.com/AnthusAI/Jev-Flywheel) remains the Jev-specific research demonstration of feedback, scorecards, and human steering.

## Contract

```text
trusted labelled pool ──► ContextPolicy ──► DecisionModel ──► DecisionResult
                                  ▲
new unlabeled target ─────────────┘
```

`DecisionModel` accepts a typed task, an item, and selected context; it returns a label, optional probabilities, model ID, usage, and latency. This scaffold includes a System One adapter for **Jev** and **Kev**-compatible clients. Laya support is a local adapter planned as an optional extra. Core tests use fake clients only.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
make test
```

The sibling [Decision-Flywheel-Evaluations](https://github.com/AnthusAI/Decision-Flywheel-Evaluations) repository holds preregistrations, manifests, live-response caches, and cross-model results.

## Data safety

A policy may use candidate-pool labels and target text. It must not use target labels. Optimizer development data selects policies; a protected scoreboard is used only after a policy is frozen. Store request fingerprints and response metadata, not credentials or redistributed dataset text.

## License

MIT for this repository's code. Downloaded datasets and model weights retain their upstream terms.
