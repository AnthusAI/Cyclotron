# Measuring what a cyclotron learned

This page defines Cyclotron's headline evidence: a before-and-after number on a
fixed held-out set of human labels. It covers the metric, how the held-out
set is chosen and frozen, the report format, and how the shared UI shows it in
an embedded application (Papyrus) and in the central lab.

## The question it answers

> On reviews it never trained on, how much better does the cyclotron agree with
> editors now than when it was created?

Two classifiers answer the same items:

- **Before** is version 1 as the store was created: the seed rubric and any
  priming examples, with no reviews and no ML model.
- **After** is the classifier active now, with its learned rubric, example
  list, supporting questions and fitted ML model.

Both are scored on every item of one frozen held-out set, with the same
request clock, so the only difference between the two columns is what the
cyclotron learned from reviews.

## The metric

The headline is **balanced accuracy**: for each label, the share of items
with that label the classifier got right, averaged over labels. A rare label
(`include` in Papyrus) counts as much as a common one, so a classifier that
says `exclude` to everything scores 50%, not the 85% plain accuracy would
give it.

The report also carries accuracy, precision and recall for the positive label
(macro averages when a classifier has no positive label) and agreement on each
label, for both endpoints.

**Weighting.** Once the review rate tapers, confident decisions are reviewed
only at a sampled rate. Each held-out item therefore counts with the inverse
of the probability it was sent to review (its recorded selection
propensity), so the numbers stand for every decision in the audit share and
not only the uncertain ones that were reviewed. While every decision is
reviewed, every weight is 1.

**Is the change real?** The report counts the items the active classifier
newly gets right (`gained`) and newly gets wrong (`lost`), and runs an exact
two-sided sign test on those pairs. The verdict is:

| Verdict | When |
| --- | --- |
| `nothing-learned-yet` | The active classifier is still version 1 with no ML model. |
| `too-few-labels` | The rarest label has fewer than `minimumPerLabel` held-out items (default 10). Numbers are shown, no claim is made. |
| `improved` | p < 0.05 and gained > lost. |
| `worse` | p < 0.05 and lost > gained. |
| `no-clear-change` | Anything else. |

Only `improved` supports a public claim such as the marketing site's.

## The held-out set

The engine already splits items by identity before any label is seen: about
20% of items fall in the **audit share**, and their reviews never train,
tune, prime or select examples for any classifier. A held-out set is frozen
from that share:

- Only reviews the cyclotron selected (by the review program, low confidence
  or the random audit draw). Reviews a reviewer chose to make are excluded:
  they are not a sample of decisions, and they are used for training.
- The latest active label for each item; undone reviews are dropped.
- Frozen means copied: item values, labels, selection propensities and the
  freeze time are stored in the set. Later labels go into a new set, which
  freezing again creates; freezing with no new labels returns the same set.
- If a frozen item is later reviewed by a reviewer's own choice it moves into
  training; it is then left out of evaluations and counted as
  `excludedSinceFrozen`. A label corrected after freezing is counted as
  `labelsChangedSinceFrozen`; the frozen label is still used, so a set never
  changes underneath a comparison.

Sets and reports are stored in the cyclotron's own store, so they work the
same on the local SQLite store and the S3 cloud store, and travel with
`snapshot` and `restore`.

## Cost

Freezing makes no model call. Evaluating makes at most two decision-model
requests per scored item (`held_out_request_bound()`), through the store's
shared request cache: re-evaluating after no change costs nothing, and after a
new version only the "after" column is new. An evaluation that could exceed
the open's request ceiling is refused before any call. Nothing is learned,
fitted or promoted during an evaluation.

## Engine API

```python
summary = cyclotron.freeze_held_out_set()                # no model call
bound = cyclotron.held_out_request_bound()               # most new requests an evaluation can make
report = await cyclotron.evaluate_held_out()             # cyclotron-held-out-report/v1
history = cyclotron.held_out_reports()                   # the last 10, oldest first
```

Each takes `classifier=` for a cyclotron with several outputs, and
`evaluate_held_out` takes `set_id=`, `minimum_per_label=` and the same
`execution_guard=` as `decide`.

## Report format

`cyclotron-held-out-report/v1` is described by
`src/decision_flywheel/schemas/cyclotron-held-out-report.v1.schema.json`,
with an example beside it and TypeScript types in the package
(`cyclotron/held-out-report`). Its parts:

- `headline`: the metric, its definition, before, after and `changePoints`.
- `before` and `after`: version, refits, fingerprint and all metrics.
- `change`: gained, lost, both right, both wrong, the p-value, the verdict and
  a one-paragraph `summary` a person can read.
- `heldOutSet`: the set's id, freeze time, size and label counts, and what
  was left out since freezing.
- `cost`: new and cached requests and tokens.
- `items`: per item, the label, both predictions, its weight and the outcome.
  Only identities and labels, never item text.

## Showing it in either deployment mode

The package exports `HeldOutReportView` (render a report) and `HeldOutPanel`
(load, freeze and evaluate through the host's transport) from
`cyclotron/components/held-out-report`, and `createHeldOutClient` from
`cyclotron/held-out-report`. Styles are in `styles/components.css`.

Both modes serve the same two GraphQL operations, so the card is the same
component on the same data:

```graphql
query    { heldOutOverview(cyclotronId: ID!, classifier: String): JSON }
mutation { heldOutCommand(action: "freeze" | "evaluate", payload: JSON!): JSON }
```

`decision_flywheel.held_out_service.HeldOutService` implements them. The host
supplies `open_cyclotron(cyclotron_id, max_requests=None)`, which opens the
authorized store for one call and raises for any other (evaluating reopens with
`max_requests` equal to the confirmed request bound); a scale-to-zero host such as Papyrus holds
the store's lease only while the call runs.

- **Embedded (Papyrus).** Papyrus's own API mounts the two operations with a
  `HeldOutService` whose opener checks the account and publication grant,
  and places `HeldOutPanel` beside the status card. Papyrus owns that UI path.
- **Central lab.** `create_app(held_out=HeldOutService(...))` serves the same
  operations. Hosted account mode accepts only an account-scoped service on
  the workspace (`workspace.held_out`), never a global one. `HostedRuntime(open_cyclotron=...)`
wires that service from `open_cyclotron(account_id, cyclotron_id, max_requests)`
and the one `HostedConfig.allow_live`; `create_app` refuses services whose
`allow_live` flags disagree, and `capabilities.liveEnabled` reports the shared flag.

`evaluate` needs live authority (`allow_live=True`) and the caller must send
`confirmedRequestBound` equal to the bound the overview showed; the panel's
button says "Evaluate (up to N model requests)".
