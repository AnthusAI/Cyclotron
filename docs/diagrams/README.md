# Cyclotron diagrams

Each `<name>.d2` file is the source of one diagram. `make diagrams` renders it
to `<name>.svg` with [D2.js](https://www.npmjs.com/package/@d2lang/d2), the
WebAssembly build of D2. Each SVG contains light and dark themes and follows
the reader's `prefers-color-scheme`, so the README, the documentation, the lab,
and the website can embed the same file with a plain `<img>`.

| Diagram | Source | Shows |
| --- | --- | --- |
| Harness | [harness.d2](harness.d2) | What Cyclotron adds around one decision model |
| Cyclotrons | [cyclotrons.d2](cyclotrons.d2) | Many classifiers in one decision-model request |
| Classifier | [classifier.d2](classifier.d2) | The decision request and the learned ML head |
| Improvement | [improvement.d2](improvement.d2) | One optimization round, from feedback to promotion |
| Evaluation | [evaluation.d2](evaluation.d2) | Data partitions that keep learning and evaluation apart |

The diagrams show the intended complete system. They do not claim that the
current reviewer runs every step.

## Render

From the repository root:

```bash
make diagrams
```

The first run installs the pinned D2.js version into `docs/diagrams/node_modules`.
No D2 CLI or browser is needed. `make check-diagrams` fails if any committed SVG
differs from its source; CI runs it on every pull request.

## Conventions

Every diagram starts with `...@_cyclotron`. That import sets the ELK layout,
the D2 light and dark themes, and the role classes:

| Class | Use for |
| --- | --- |
| `step` | Code that transforms or moves data |
| `llm` | The LLM optimizer and anything it proposes |
| `ml` | ML fitting and the learned head |
| `model` | The decision model |
| `human` | Human labels, explanations, and review |
| `store` | Saved context, versions, and records |
| `outcome` | A final prediction or promoted version |
| `group` | A container |
| `note` | Explanatory text |
| `spacer` | An empty cell in a grid layout |
| `flow` | The main path of a request |
| `proposal` | Edges that carry LLM proposals |
| `learning` | Edges that carry trusted labels or fitted values |
| `evidence` | Supporting data, drawn dashed |

Classes name a role, not a colour. `render.mjs` adds
[_cyclotron.css](_cyclotron.css) to each SVG; it colours each role in light
and dark mode with values from the shared UI kit
(`trace-ui/src/styles/shared.css`). To change a colour, edit the stylesheet
and run `make diagrams`.

Keep each diagram at most about 880 pixels wide, the width of a GitHub README
column, so its text displays near full size. When a loop makes ELK spread a
diagram into one long row or sideways, lay it out with a D2 grid of two or
three columns instead, as `harness.d2`, `cyclotrons.d2`, `classifier.d2`, and
`improvement.d2` do. Leave `""` cells with `class: spacer` where a connection
must pass, so no line crosses a shape.

The prose uses the writing approach in
[ASD-STE100](https://www.asd-ste100.org/STE_faq.html).
