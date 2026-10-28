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

If you already know some supporting questions that matter, seed them the same
way with `seed_supporting_questions`. It maps each classifier id to a list of
questions, each with exactly `name`, `instructions` and `labels` (the shape the
optimizer proposes), or to `DecisionTask` values. For example,
`seed_supporting_questions={"relevant": [{"name": "primary_evidence",
"instructions": "Does the source cite primary evidence?", "labels": ["yes", "no"]}]}`.
Seeded questions are in the first decision request, and their answers are
features of the first ML model fit. They are a starting point, not a pin: the
optimizer can revise, replace or drop them like any supporting question. They
are fixed when the store is created. Reopening a store never rewrites them, and
a store created without seeds keeps none. Your decision model must answer every
supporting question in the request.

### Prime it with labeled examples

If people have already labeled some items, pass them as `priming_examples` when
the store is created. Each one is a `LabeledItem` with a configured label and
an optional explanation in `context["human_feedback"]`:

```python
from decision_flywheel.models import Item, LabeledItem

examples = {"relevant": [LabeledItem(Item("paper-1", {"text": "Primary research on supply-chain attacks."}),
                                     "include", "trusted", {"human_feedback": "Cites the vendor's own logs."})]}
primed = Cyclotron.open("var/cyclotrons/primed-relevance", definition, ScriptedModel(),
                        optimizer=None, priming_examples=examples)
assert primed.status().to_json()["cyclotron"]["primedExamples"] == 1
primed.close()
```

The first decision request already carries them as training context. They
are never decisions, reviews or measured alignment, and an item used to prime
cannot also be decided. `status().to_json()["cyclotron"]["primedExamples"]`
says how many there are, and the status widget shows "Started with N references" (the embedded components use plain editor-facing wording: references and suggestions, with no versions, calibration or onboarding).

A running store takes more examples with `add_priming_examples`. Each output
that gains examples appends them to its fixed example list as a new version,
so its next decision request carries them; an unreviewed decision is decided
again by that version. The ML model was fitted to the old context, so the new
version starts without it and the next learning pass refits it. Adding the
same examples again changes nothing; an identity that was already decided, or
that was primed with different content, is refused. `priming_additions()`
records who added which examples and when.

`retract_priming_examples({classifier_id: [item_id, ...]}, retracted_by=...)`
withdraws examples again, for instance when a person withdraws the review an
example came from. Each affected output takes them off its example list as a
new version, so the next decision no longer carries them as training and the
next learning pass refits without them. The retraction (who, when, which
identities) is stored and shown by `priming_retractions()` and under
`retractions` in `priming_additions()`. Retracting an identity that is not
primed, or already retracted, changes nothing and is reported under `absent`;
an unknown classifier is refused. A retracted identity may be added again with
`add_priming_examples`; that is a new addition and takes the content it is
given then. The rule that primed items and operational items are disjoint still
applies.

## Changing the classifier set

A store keeps its learned state when its classifiers change, so one shared
cyclotron can follow a set that grows and shrinks over time (for example one
binary classifier per newsroom section).

`add_classifiers([ClassifierSpec, ...], added_by=..., seed_rubrics={id: text})`
adds classifiers to an open store. No model call is made. A new classifier
starts unfitted with its seed rubric and no labels, and learns only from
reviews of decisions made after it was added. The other classifiers keep their
labels, rubrics and versions; the shared decision request now carries the new
classifier, so unreviewed decisions are decided again with it and the other
classifiers' ML models refit on the next decision.

`retire_classifiers([classifier_id, ...], retired_by=...)` stops classifiers
deciding and learning. Their history, labels and reviews stay in the store
(`labels(include_retired=True)`, `decision_log()`, snapshots), decisions already
made keep their results for them, and reviewing for one is refused. A retired
classifier can be added again with the same specification and returns with the
history it had; a different specification is refused. `retired_classifiers()`
lists them and `classifier_set_changes()` records who changed the set and when.

Both calls are idempotent (a repeated call changes nothing and reports the ids
under `already_active` or `already_retired`), refuse unknown or duplicate ids
and a blank `added_by` or `retired_by`, and take the same lock and
`execution_guard` as `add_priming_examples`. The saved definition describes the
current active set, so reopen with the current definition; a definition that
lacks a recorded addition or still lists a recorded retirement is refused with
an error naming the classifier and who changed it. Once the set has changed,
the order classifiers are listed in no longer matters. Cloud (S3) stores take
the same calls and replay the set on reopen.

```python
primed = Cyclotron.open("var/cyclotrons/primed-relevance", definition, ScriptedModel(), optimizer=None)
pasted = {"relevant": [LabeledItem(Item("pasted-url-1", {"text": "A vendor's product announcement."}),
                                   "exclude", "trusted", {"human_feedback": "Marketing, not research."})]}
added = asyncio.run(primed.add_priming_examples(pasted, added_by="editor-1"))
assert added["added"] == {"relevant": ["pasted-url-1"]} and added["versions"] == {"relevant": 2}
assert primed.status().to_json()["lastChange"]["summary"] == "Added 1 primed example to the example list."
primed.close()
```

Reopening with the examples the store was created with still works after
additions. Reopening with examples the store never accepted fails.

To collect examples in the application, render `PrimingPanel` from
`cyclotron/components/priming-panel` with `createPrimingClient` from
`cyclotron/priming-client` bound to your authenticated GraphQL transport. The
server side is `decision_flywheel.priming.PrimingService`: people paste a URL
or upload CSV, JSON, JSONL or a spreadsheet, check what was extracted, choose
a label for each output with an optional explanation, and accept.
`PrimingService.examples(list_id, classifiers)` then returns the accepted
examples, for `priming_examples` on a new store or `add_priming_examples` on a
running one. Fetching and extracting a URL is the
application's transformer, not Cyclotron's.

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

With several outputs, each output is routed on its own. It is selected on
its own confidence, with its own random draw at the review rate and its own
audit draw, so a confident output is not sent to review just because a sibling
is unsure. `decision.review.outputs` holds each output's selection (reason,
propensity, detail), and `decision.review.requested` names the outputs waiting
for a reviewer. `decision.review` itself summarizes them and is selected when
any output is requested. Only requested outputs read as pending, and a decision
is reviewed once each requested output has a review: a checklist that labels
just those outputs completes it. A review of an output that was not requested
is the reviewer's choice (`selected_by="reviewer"`). It trains the ML model but
is not review-rate evidence. Each requested output's review counts toward that
output's window. Draws are keyed by item, position and output, so a store
replays the same selections. A one-output decision is unchanged.
`review_rate.expected_reviews_per_week` follows the routing: a decision
counts by the chance that any of its outputs is requested. Decisions saved
before routing count as they were selected, on their lowest confidence.

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

With several outputs, save a reviewer's whole checklist as one review:
`cyclotron.review(decision_id, labels={"relevant": "include", "timely": "exclude"},
review_id="message-982")` returns `{classifier: Review}`. Every entry is
checked before anything is recorded. An output left out stays open for review,
and a `None` entry closes that output without teaching. Each output gets its
own review (`message-982:relevant`, ...) and `review` event. All labels are
recorded before any output learns, and then each labeled output runs its
learning pass once. Each labeled output's review counts toward that output's
review-rate window; an output left out does not. Resubmitting the same review
id is safe and completes any output an interruption left unrecorded.

By default each review runs the labeled outputs' learning before it returns.
With several outputs that means the reviewer waits for several optimization
passes. Open the store with `learning="deferred"` and `review()` only records
the labels and queues one learning pass per labeled output. The queue is
durable, and several reviews of one output share a pass. Run the queue
afterwards with `await cyclotron.learn()`, for example once the reviewer's
request has been answered. `cyclotron.pending_learning()` lists the queued
outputs. `learn()` runs one output at a time and holds the store lock only for
that output, so reviews and decisions interleave between outputs. Each pass is
recorded as a `deferred-learning-completed` event with its warnings and
duration. A pass covering several reviews still runs any rubric change or
feedback-cadence stage that one of those reviews made due. Every output's
optimizer stages share the cyclotron's optimizer and its call limit.

A synchronous optimizer transport blocks the event loop while its call runs.
An asynchronous one keeps the loop free, and a deferred learning pass releases
the store lock while it waits on the provider, so reviews and decisions
complete in the meantime. A transport is asynchronous when its `acomplete` is
a coroutine function, or when it is itself an `async` callable.
`OpenAIOptimizer(..., async_client=...)` and
`LiteLLMOptimizer(..., acompletion=...)` are asynchronous, as is
`from_environment(..., asynchronous=True)`. Create the asynchronous client in
the event loop that runs `learn()`. Optimizer events are recorded on the
event-loop thread, before and after the call. Events from a review or decision
made during the call can appear in an output's history between the pass's own
events.

After the call, the pass takes the store lock back, even when it is being
cancelled, and restores its cycle and shared decision context. The pass is
*superseded* if, during the call, any output's criteria changed (rubric,
examples, supporting questions or dynamic inputs; an ML model refit alone does
not count, because the pass refits ML models itself), or a label it rests on
(training or development) was corrected or undone. A superseded pass records
`optimizer-superseded`, closes its step and round in the trace, caches nothing,
returns `superseded: True` and stays queued. After `max_superseded_passes`
superseded passes in a row (an option of `Cyclotron.open`, default 3), the
output's next pass keeps the store lock through its provider calls, so it
cannot be superseded again. It records `learning-window-withheld`.

Each `learn()` call runs each queued output at most once, so a busy output
cannot starve the others. A pass during which any review of its output
arrived, held-out ones included, stays queued from the count it covered.

Cancelling `learn()` (a timeout or shutdown) records `optimizer-cancelled`,
closes the pass's cycle, step and round, and keeps the output queued.
Decisions keep working. A stopped pass removes only the pending work it
started itself. Work that other reviews or decisions left pending while the
lock was lent keeps its explicit-retry guard. One exception to "no silent
repayment" is deliberate: if the pass's own paid decision-model request was
interrupted, its guard is dropped so the next pass can ask again, and each
such request is recorded as `decision-request-abandoned` with
`repeat_charge_possible: true`, because asking again may be charged again.

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

`reason`, `nextStep` and `lastChange.summary` are engine text: they name
calibration gaps, versions and refits, and belong in operator and audit
views. Editor-facing views show the plain companions instead,
`reviewRate.plainReason`, `reviewRate.plainNextStep` and
`lastChange.plainSummary`, which never use engine vocabulary. The embedded
status card prefers them and falls back to the engine text only for a
snapshot from an older engine.

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

`usage["outputs"]["by_output"]` estimates what each output (classifier)
costs, in the same shape as the total. Each output's figures sum to the total.
Optimizer calls are measured per output, because each output's learning makes
its own calls. A shared decision-model request is billed as a whole, so it is
split by each output's share of the request bytes: the output's own context
and questions, an even share of the item, and an even share of any supporting
question it shares. Split request and token counts can be fractional.
`usage["outputs"]["estimate"]` says how the split is made. Requests recorded
by older stores have no shares and are split evenly. A deferred learning pass
counts toward the latest decision.

## Before and after on a held-out set

The status card reports agreement on recent reviews. The headline evidence
that a cyclotron learned is a separate measurement: the classifier as the
store was created versus the classifier active now, on a frozen set of audit
reviews neither has trained on.

```python
cyclotron.freeze_held_out_set()
report = asyncio.run(cyclotron.evaluate_held_out())
print(report["headline"]["before"], report["headline"]["after"], report["change"]["verdict"])
```

[held-out-measurement.md](held-out-measurement.md) defines the metric, the
held-out set, the report format and the shared GraphQL operations behind the
`HeldOutPanel` card.

## The review program and its override

The default `ReviewProgram` starts by reviewing every decision. It steps the
rate for confident decisions down (100%, 50%, 25%, 10%) when, for two
consecutive windows of 100 decisions, accuracy on confident decisions met the
target with enough reviews and the calibration gap stayed small. It goes back
to full review when a window fails or a new version is promoted.
Low-confidence decisions are always reviewed, and a random audit share
remains at every rate. Every number is configuration; the defaults are
placeholders until the review-rate experiments set them.

With several outputs there is still one review rate, because one review
covers the whole decision, but every output is watched. Each window is
checked per output, against that output's own decisions, reviews and
calibration gap. The window fails if any output with enough reviews misses the
target, and it holds if any output is short of reviews. It counts toward
stepping down only when every output meets the target. A new version of any
output raises the rate. The `review-rate-changed` event and the reason name the
output (`output`), and the event's evidence keeps every output's numbers.
`cyclotron.overview()` returns every output's status, the output with the
lowest measured alignment (`worst`) and the output that decided the last
window (`limiting`). A one-output store keeps its program state and events
exactly as before.

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
  "cyclotron": "https://github.com/AnthusAI/Cyclotron/releases/download/ui-v0.2.3/cyclotron-0.2.3.tgz"
}
```

```ts
// next.config.ts
const nextConfig = { transpilePackages: ["cyclotron"] }
```

Import `cyclotron/styles/components.css` once. It holds component rules only:
no global reset and no theme, and it reads the host's theme tokens when they
exist. Do not import `styles/shared.css`, the console's theme. That covers the
plain-CSS tier only; `priming-panel` and the other Tailwind-tier exports are
listed in the README and need Tailwind v4 in the host.

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
    <details><summary>How suggestions are doing</summary>
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

## Hosted curation routes

An application whose server calls the central lab, rather than embedding the engine, imports
accepted examples through three bearer-authenticated routes. The application's server forwards
the signed-in person's access token and the pins `X-Cyclotron-Account`, `X-Papyrus-Publication`
and `X-Papyrus-Binding`; it must check that the lab echoes the same pins.

```
GET /api/newsroom/curation/context
-> {"scope": {"accountId": "...", "publicationId": "alpha", "bindingRevision": "r1"},
    "mode": "remote",
    "priming": {"listId": "accepted", "classifiers": [{"id": "alpha", "revision": 1}], "transformers": []},
    "capabilities": ["read", "review"], "bindingRef": null}

POST /api/newsroom/curation/priming
{"query": "mutation($action:String!,$payload:JSON!){primingCommand(action:$action,payload:$payload)}",
 "variables": {"action": "file", "payload": {"identifier": "d1", "list_id": "accepted",
   "refs": [{"id": "alpha", "revision": 1}], "kind": "json", "content": "<base64 rows>"}}}
-> {"data": {"primingCommand": {"id": "d1", "status": "preview", "version": 1, "rows": [...], ...}}}

POST /api/newsroom/curation/priming   (action "accept", payload {"identifier", "version", "labels"})
-> {"data": {"primingCommand": {"id": "d1", "status": "accepted",
     "autoPrime": {"added": {"alpha": ["row-1"]}, "primedExamples": {"alpha": 1}, ...}}}}

POST /api/newsroom/curation/publication   {"operation": "status", "payload": {}}
-> the cyclotron-publication/v1 status snapshot for the pinned scope
```

Reading the context, the priming queries (`primingContract`, `primingTemplate`, `primingDraft`)
and `status` need the `console:read` role; accepting or importing needs `console:write`.
The lab re-verifies the token with the account service on every request and again after the
work. The `priming` object is `null` when the binding has no configured list. Slice 1 is file
import only: there is no hosted transformer, so `transform` answers `400 no_hosted_transformer`.
Draft names are private to the publication, an import has no size limit of its own, and accepting
is idempotent and applies the examples to the publication's cyclotron so the next decision
uses them. Refusals use stable codes (`bearer_required`, `bearer_invalid`, `access_refused`,
`authority_unavailable`, `account_changed`, `outside_publication`,
`storage_limit`, `firewall_overlap`, `auto_prime_failed`, `operation_unavailable`).
See `publication-deployment-contract.md` for the full contract.

## Operational notes

- **Batched decision model.** The model needs `classify_many`.
- **Content cache.** Identical item values share one decision-model answer.
- **Stable shown decisions.** A decision keeps its label and confidence until
  it is reviewed or a new version is promoted.
- **Versions.** A new rubric, example list or set of supporting questions is
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
