# Embed a cyclotron in an application

This guide walks an application through one cyclotron, from opening its store
to showing reviewers what it is doing. The running example is Papyrus, the
automated newsroom: its scanners find candidate sources, and a cyclotron
decides whether each one is relevant to the publication before an editor
reviews it. Every code sample on this page is run by a spec
(`tests/embedding_docs_test.py`, `trace-ui/src/examples/embedding.test.tsx`).

## Who owns what

The application owns its items, its reviewers, its own record of reviews, and
its user interface. The cyclotron owns everything needed to learn from those
reviews: item partitions, held-out reviews, selection propensities, versions,
the ML model, the LLM optimizer's proposals and the review rate. The
application keys everything by its own item ids.

## Define the cyclotron and open its store

A cyclotron is one decision about one kind of item. Each classifier inside it
asks one question. The decision model must be batched: a cyclotron asks all
its classifiers in one request, so the model implements `classify_many`, as
the Jev adapter does.

```python
import asyncio

from decision_flywheel import ClassifierSpec, Cyclotron, CyclotronDefinition, Item, ReviewProgram
from decision_flywheel.batched_classification import BatchedAnswers
from decision_flywheel.models import DecisionResult


class ScriptedModel:
    """Stands in for a batched decision-model adapter such as Jev's."""
    model_identity = "scripted-batch-v1"

    async def classify_many(self, configs, target, training, **kwargs):
        sure = 0.9 if "security" in target.values["text"] else 0.6
        answer = DecisionResult("include", {"include": sure, "exclude": 1 - sure})
        return BatchedAnswers({name: {"decision": answer} for name in configs}, self.model_identity, {}, 0)


relevant = ClassifierSpec("relevant", ("include", "exclude"),
                          "Should this source become a reference for this publication?",
                          positive_label="include")
definition = CyclotronDefinition("papyrus-relevance", (relevant,), seed="papyrus-relevance-v1")
```

Seed the first rubric from the application's own parameters (Papyrus uses the
publication's doctrine) with `seed_rubrics`, never the question: a store
refuses a changed definition, while the rubric is what the LLM optimizer is
allowed to change. The seed is fixed when a classifier's store is first
opened; later parameter edits do not rewrite it.

Opening a store makes no model call. `max_requests` authorizes that many new
decision-model requests for this open, on top of all earlier ones. Pass an
`OptimizerAgent` as `optimizer` to let reviews drive the LLM optimizer and the
ML model fit; without one, the cyclotron decides and records.

```python
doctrine = "Publish practical AI security analysis. Prefer primary evidence over vendor marketing."
cyclotron = Cyclotron.open("var/cyclotrons/papyrus-relevance", definition, ScriptedModel(),
                           optimizer=None, max_requests=200, review_program=ReviewProgram(),
                           seed_rubrics={"relevant": doctrine})
```

## Decide

```python
decision = asyncio.run(cyclotron.decide(Item("ref-123", {"text": "A security advisory for model servers"})))
print(decision.label, decision.confidence, decision.version)
print(decision.review.selected, decision.review.reason, decision.review.detail)
```

`decision.review` says whether the decision goes to a reviewer, why
(`program`: low confidence or sampled at the review rate; `audit`: the random
audit share; `None`: not sent), with what probability, and in a sentence.
Show the sentence next to the item.

A decision a person may have seen is stable. Deciding the same unchanged item
again returns the same decision with the same label and confidence until it
is reviewed or a new version is promoted; an ML model refit does not change
it, and no model call is repeated. Answers are cached by content: two items
with identical values share one decision-model answer.

## Review

```python
review = asyncio.run(cyclotron.review(
    decision.decision_id, "exclude", explanation="Vendor marketing, not an advisory.",
    reason_code="out_of_scope", reviewer="editor-7", review_id="message-981"))
again = asyncio.run(cyclotron.review(
    decision.decision_id, "exclude", explanation="Vendor marketing, not an advisory.",
    reason_code="out_of_scope", reviewer="editor-7", review_id="message-981"))
assert again == review  # the same review id records nothing new
```

Pass the application's own review id so a retry is safe. The first label
teaches; a later label is a correction. `label=None` closes a decision
without teaching, for reasons that are not about relevance (a duplicate, an
unavailable source). `undo_review` reopens a decision.

```python
other = asyncio.run(cyclotron.decide(Item("ref-124", {"text": "A conference recap"})))
asyncio.run(cyclotron.review(other.decision_id, None, reason_code="duplicate"))
asyncio.run(cyclotron.undo_review(decision.decision_id))
asyncio.run(cyclotron.review(decision.decision_id, "include", reviewer="editor-7"))
```

Who chose the review matters. A review of a decision the cyclotron sent is
`selected_by="program"` or `"audit"` (the default). A review of a decision it
did not send is `selected_by="reviewer"`: it trains the ML model but never
counts as alignment or audit evidence.

## Status and events

```python
status = cyclotron.status().to_json()
print(status["alignment"]["accuracy"], status["calibration"]["saysSure"], status["calibration"]["isRight"])
print(status["reviewRate"]["state"], status["reviewRate"]["reason"])
print(status["reviewRate"]["nextStep"])
```

`status()` returns the `cyclotron-status/v1` snapshot
([schema](../src/decision_flywheel/schemas/cyclotron-status.v1.schema.json)).
It makes no model call; store it where the application's interface can read
it (Papyrus keeps it as a record in its own database).

```python
cursor = 0
page = cyclotron.subscribe(after=cursor, limit=100)
for event in page["events"]:
    print(event["kind"])
cursor = page["cursor"]  # keep it; it survives restarts
```

Event kinds are `decision`, `review`, `promoted` (a new version), `refit` (a
new ML model, same version), `dropped` (a candidate that failed), and
`review-rate-changed`. Their shapes are typed in `cyclotron/sdk-types`.

What the model calls cost, per window of decisions and in total, at the list
prices you state. A cached answer costs nothing; a decision-model request made
while learning counts toward the decision whose review drove it.

```python
prices = {"decision_model": {"input_usd_per_mtok": 0.042, "output_usd_per_mtok": 0.0},
          "optimizer": {"input_usd_per_mtok": 0.40, "cached_input_usd_per_mtok": 0.10, "output_usd_per_mtok": 1.60}}
usage = cyclotron.usage(prices, window=100)
print(usage["total"]["decision_model"]["requests"], "decision-model requests", usage["total"]["usd"], "USD")
print(len(cyclotron.decision_log()), "decisions in the log")
```

## The review program and its override

The default `ReviewProgram` starts by reviewing every decision. It steps the
rate for confident decisions down (100%, 50%, 25%, 10%) when, for two
consecutive windows of 100 decisions, accuracy on confident decisions met the
target with enough reviews and the calibration gap stayed small. It goes back
to full review when a window fails or a new version is promoted.
Low-confidence decisions are always reviewed, and a random audit share
remains at every rate. Every number is configuration; the defaults are
placeholders until the review-rate experiments set them.

```python
strict = ReviewProgram(rates=(1.0, 0.5, 0.2), audit_share=0.1, confidence_threshold=0.75,
                       target_accuracy=0.9, window=50, min_window_labels=15)
```

An operator can override the rate for a while. Low-confidence decisions and
the audit share are still reviewed.

```python
from datetime import datetime, timedelta, timezone

cyclotron.set_review_rate(0.5, set_by="managing-editor",
                          expires_at=datetime.now(timezone.utc) + timedelta(days=7))
print(cyclotron.status().review_rate.state, cyclotron.status().review_rate.reason)
cyclotron.clear_review_rate(set_by="managing-editor")
```

Pass `review_program=None` to review every decision.

## One writer, snapshots and restore

`Cyclotron.open` takes a lease before it touches the store. The default is a
lock file that the operating system releases when the process ends; a second
writer gets `StoreLocked` before anything is opened. Several workers supply
their own lease with `acquire()` and `release()`.

```python
from decision_flywheel import StoreLocked

try:
    Cyclotron.open("var/cyclotrons/papyrus-relevance", definition, ScriptedModel())
except StoreLocked:
    print("another writer holds the store")


class InMemoryClaims:
    """Stands in for the application's claim store (Papyrus: an Assignment claim)."""
    def __init__(self):
        self.held = set()

    def claim(self, key):
        if key in self.held:
            return False
        self.held.add(key)
        return True

    def release(self, key):
        self.held.discard(key)


class ClaimLease:
    def __init__(self, claims, key):
        self.claims, self.key = claims, key

    def acquire(self):
        if not self.claims.claim(self.key):
            raise StoreLocked(f"another worker holds {self.key}")

    def release(self):
        self.claims.release(self.key)


claims = InMemoryClaims()
```

A worker that does not keep its disk restores the store, works, and takes a
snapshot. Take snapshots between calls: `snapshot()` does not wait for a
running `decide` or `review`. A snapshot holds no provider keys, but it does
hold item values, explanations, transcripts and caches, so keep it in private
storage (Papyrus uses its private media bucket).

```python
cyclotron.snapshot("var/snapshots/papyrus-relevance.tar.gz")  # then upload it
cyclotron.close()

Cyclotron.restore("var/snapshots/papyrus-relevance.tar.gz", "var/restored/papyrus-relevance")
restored = Cyclotron.open("var/restored/papyrus-relevance", definition, ScriptedModel(),
                          lease=ClaimLease(claims, "cyclotron.relevance"))
print(restored.status().alignment.labels)
```

If a snapshot is ever lost, rebuild from the application's own record of
labels. `replay` decides each item again (a new store makes model calls) and
records the same labels with the same partitions.

```python
labels = restored.labels()
with Cyclotron.open("var/rebuilt/papyrus-relevance", definition, ScriptedModel()) as rebuilt:
    print(asyncio.run(rebuilt.replay(labels)), "labels replayed")
restored.close()
```

## Show it in the application's interface

The web package ships the review control and the status widget as data-only
React components: they never call a server. Pin the release tarball and let
the framework compile it:

```json
"dependencies": {
  "cyclotron": "https://github.com/AnthusAI/Cyclotron/releases/download/ui-v0.2.1/cyclotron-0.2.1.tgz"
}
```

```ts
// next.config.ts
const nextConfig = { transpilePackages: ["cyclotron"] }
```

Import `cyclotron/styles/components.css` once. It holds component rules only:
no global reset and no theme, and it reads the host's theme tokens when they
exist. Do not import `styles/shared.css`, the console's theme.

```tsx
import {ReviewControl,type ReviewControlReview,type ReviewReason} from '../components/review-control'
import {CyclotronStatusView} from '../components/cyclotron-status'
import type {CyclotronStatus} from '../cyclotronStatus'
import type {CyclotronDecision} from '../cyclotronSdk'
// In an application: import ... from 'cyclotron/components/review-control', and so on,
// plus `import 'cyclotron/styles/components.css'` once.

const reasons:ReviewReason[]=[
  {code:'out_of_scope',text:'Out of scope'},
  {code:'policy_exclusion',text:'Policy exclusion'},
  {code:'duplicate',text:'Duplicate',noLabel:true},
]

/** One pending item: the decision the cyclotron made and the reviewer's controls. */
export function PendingItem({itemId,decision,onReview}:{itemId:string;decision:CyclotronDecision;onReview:(review:ReviewControlReview)=>void}){
  const [classifier,result]=Object.entries(decision.classifiers)[0]
  return <ReviewControl
    itemId={itemId}
    decision={{decisionId:decision.decisionId,label:result.label,confidence:result.confidence,
               classes:['include','exclude'],version:result.version}}
    question={classifier==='relevant'?'Is this relevant to the publication?':classifier}
    reviewReason={decision.review.selected?decision.review.detail:undefined}
    positiveLabel="include"
    reasons={reasons}
    onReview={onReview} />
}

/** The page header strip and the detail card. */
export function CyclotronHeader({status,onOverride}:{status:CyclotronStatus;onOverride:()=>void}){
  return <>
    <CyclotronStatusView status={status} />
    <details><summary>What the cyclotron is doing</summary>
      <CyclotronStatusView status={status} variant="card" onOverride={onOverride} />
    </details>
  </>
}
```

`ReviewControl` takes the decision, the application's reason codes (a
`noLabel` reason sends a review without a label), and an explanation, and
reports them through `onReview`; pass that to `cyclotron.review`. In thumbs
mode, thumbs up is `positiveLabel`. `CyclotronStatusView` renders a status
snapshot as a header strip or a card; `onOverride` adds a "Change review
rate" button that opens the application's own form.

## Operational notes

- **Batched decision model.** The model needs `classify_many`.
- **Content cache.** Identical item values share one decision-model answer.
- **Stable shown decisions.** A decision keeps its label and confidence until
  it is reviewed or a new version is promoted.
- **Versions.** A new rubric, example list or set of classifier questions is
  a version; an ML model refit is not (`status().refits` counts refits).
- **One writer.** Use the default lease on one machine, or your own lease
  across workers.
- **Snapshots** go in private storage and are taken between calls.
- **Crash safety.** Reviews, versions and events are derived from the
  classifier stores in one transaction, so a crash between the stores loses
  nothing; an interrupted `decide` resumes without a new model call; a crash
  in the middle of learning keeps the label, and the next `decide` of that
  item resumes its cycle without retrying the optimization.
- **Definition changes.** A store refuses a changed definition; changing a
  question or the classifiers is planned work (decision-flywheel-ade3088c).
