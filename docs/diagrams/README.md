# Cyclotron and Decision Flywheel diagrams

The two Cyclotron overview diagrams use Archify architecture specifications:

- [cyclotron-harness.html](cyclotron-harness.html) explains the self-aligning
  decision model harness around one classifier.
- [cyclotron-scorecards.html](cyclotron-scorecards.html) explains how a
  scorecard groups classifiers into one scoped decision-model request.

Their static SVGs are embedded at the start of the main README. The exported SVG
uses its built-in light and dark theme rules. The diagrams show the intended
architecture; they do not claim that every optimization is active in every run.

The classifier diagram now uses D2.js (`@d2lang/d2` version 0.1.34).
Its source is [classifier.d2](classifier.d2).
The JavaScript renderer exports optional SVGs.
The D2 CLI renders the light and dark PNGs displayed in the main README.
The main README selects an image with `prefers-color-scheme`.

To reproduce it, run from this directory:

```bash
npm ci
npm run render
d2 --layout elk --theme 0 --pad 30 --scale 2 classifier.d2 classifier-light.png
d2 --layout elk --theme 200 --pad 30 --scale 2 classifier.d2 classifier-dark.png
```

The older Archify classifier is retained as a previous design artifact.
The main README embeds both PNG images in a theme-aware picture block.

The improvement-loop diagram also uses D2 PNGs.
Its source is [improvement.d2](improvement.d2).
Render its two themes with:

```bash
d2 --layout elk --theme 0 --pad 30 --scale 2 improvement.d2 improvement-light.png
d2 --layout elk --theme 200 --pad 30 --scale 2 improvement.d2 improvement-dark.png
```

The evaluation diagram uses [evaluation.d2](evaluation.d2).
Render its two themes with:

```bash
d2 --layout elk --theme 0 --pad 30 --scale 2 evaluation.d2 evaluation-light.png
d2 --layout elk --theme 200 --pad 30 --scale 2 evaluation.d2 evaluation-dark.png
```

These workflow diagrams show the intended complete system.
They do not claim that the current reviewer runs the full loop.
The main README states the current implementation limits.

| Diagram | Checked HTML | Static image |
| --- | --- | --- |
| Cyclotron harness | [cyclotron-harness.html](cyclotron-harness.html) | [cyclotron-harness.svg](cyclotron-harness.svg) |
| Scorecards | [cyclotron-scorecards.html](cyclotron-scorecards.html) | [cyclotron-scorecards.svg](cyclotron-scorecards.svg) |
| Classifier mechanism | [classifier.html](classifier.html) | [classifier.svg](classifier.svg) |
| Prediction and feedback | [prediction.html](prediction.html) | [prediction.svg](prediction.svg) |
| Rubric changes and ML training | [improvement.html](improvement.html) | [improvement.svg](improvement.svg) |
| Learning and evaluation | [evaluation.html](evaluation.html) | [evaluation.svg](evaluation.svg) |

The workflow specifications are in
`.archify/workflow-decision-flywheel-20261005-170402/` at the repository root.
They use Archify workflow schema version 2. The Cyclotron architecture
specifications are in `.archify/architecture-cyclotron-20261007-120000/`.
They describe the agreed design, rather than a source-code call graph.

Set `ARCHIFY_HOME` to your installed Archify skill directory.
From the repository root, run each diagram separately:

```bash
node "$ARCHIFY_HOME/bin/archify.mjs" finalize workflow \
  .archify/workflow-decision-flywheel-20261005-170402/classifier.json \
  docs/diagrams/classifier.html --quality showcase --json

node "$ARCHIFY_HOME/bin/archify.mjs" finalize workflow \
  .archify/workflow-decision-flywheel-20261005-170402/prediction.json \
  docs/diagrams/prediction.html --quality showcase --json

node "$ARCHIFY_HOME/bin/archify.mjs" finalize workflow \
  .archify/workflow-decision-flywheel-20261005-170402/improvement.json \
  docs/diagrams/improvement.html --quality showcase --json

node "$ARCHIFY_HOME/bin/archify.mjs" finalize workflow \
  .archify/workflow-decision-flywheel-20261005-170402/evaluation.json \
  docs/diagrams/evaluation.html --quality showcase --json

node "$ARCHIFY_HOME/bin/archify.mjs" finalize architecture \
  .archify/architecture-cyclotron-20261007-120000/harness.json \
  docs/diagrams/cyclotron-harness.html --quality showcase --json

node "$ARCHIFY_HOME/bin/archify.mjs" finalize architecture \
  .archify/architecture-cyclotron-20261007-120000/scorecards.json \
  docs/diagrams/cyclotron-scorecards.html --quality showcase --json

node scripts/export_archify_diagrams.mjs
```

The export script uses Archify's canonical browser export.
It removes temporary viewer state from the SVG images.
Each generated HTML file has a delivery receipt and a finalization receipt.
The finalization receipts record specification and artifact hashes. These
automated checks are separate from visual inspection.

The prose uses the writing approach in
[ASD-STE100](https://www.asd-ste100.org/STE_faq.html).
The main README defines the technical names used in the diagrams.
Full dictionary conformity has not been certified.
