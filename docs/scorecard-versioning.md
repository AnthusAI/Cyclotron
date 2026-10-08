# Scorecard versions and missing-label review

A scorecard definition has an identity, a name, an ordered list of classifier
revision references, shared settings, and an active definition revision. It can
exist without any optimization runs. Editing its name, membership, ordering or
settings creates an immutable revision. Editing a classifier creates a classifier
revision and new definitions for active scorecards containing that classifier.
Existing runs do not change. Definition activation changes the configuration for
future runs, not which historical run accepts feedback.

Labeling cards follow the classifier order pinned in the run, not the order of
keys in a serialized prediction. Votes and explanations remain keyed by classifier
identity, so changing display order cannot transfer a draft to another classifier.

Run editions are a separate history: each immutable edition points to one
optimization run and its frozen classifier definitions. Classifier definitions
retain their own revision numbers. Runtime checkpoints
store the complete joint classifier state, including fitted ML heads, with a
content fingerprint. Complete decision requests retain their own cache keys.

Run creation takes one SQLite read snapshot of item-list membership and latest
content revisions. It records those exact references in the manifest and loads
the run's articles from those immutable revisions, not from another latest-list
query. A concurrent import cannot shift page offsets or replace frozen content.
Later imports remain available for new runs without altering existing runs.

Application trace events carry their pinned classifier revision and scorecard
definition revision and fingerprint. Shared transport events identify all pinned
classifier revisions. Displayed predictions carry the classifier revision plus
the actual learned configuration fingerprint (`version`). These are distinct:
definition edits affect future runs, while learning changes a run's configuration.
Older recorded events are not rewritten to add metadata they did not capture.

Adding a classifier creates a new edition. No learned state is copied. Previously
reviewed items come first. For each item, all classifiers predict before the
original labels and explanations are replayed. The human supplies missing labels.
Original label timestamps and identities remain in the catalog; new trace events
identify replay provenance. Protected splits keep the same seed and remain out
of optimization and training.

Recorded labels appear read-only. All missing labels on a historical item are
required before advancing; historical items cannot be skipped into the new-item
queue. Unseen items follow the historical cohort. The source run and its cached
responses remain intact. Reverting changes the active edition, not recorded data.
Inactive editions reject new commands until explicitly activated.

`Run.labelingAccess` describes that existing gate without starting work. The
labeling view displays its reason and disables votes, corrections, prediction
refresh and replay controls while inspection remains available. An inactive
edition is not a running job: its controls do not say "Submitting" or
"Processing". Explicit activation updates access on the normal state refresh;
it does not create labels, requests or fitted checkpoints. A stopped replay
requires a new Run action after activation rather than restarting itself.

The version selector can inspect either edition. **Use this version** activates
the selected edition. **Compare version metrics** shows Recall, Precision,
Accuracy, and label counts. These are rolling prediction-before-feedback metrics
over at most 200 reviewed items, not a matched held-out comparison. Replayed
historical labels must not be represented as freshly collected human judgments.

GraphQL exposes `scorecards`, `scorecardVersions`, `extendScorecard`, and
`activateScorecardVersion`. Creating a fresh replay requires explicit live-call
authority. Merely creating an edition makes no provider calls; preparing an item
does, under the edition's explicit request and optimizer ceilings.

Definition configuration uses `scorecardDefinitions`,
`scorecardDefinitionVersions`, `scorecardClassifiers`,
`saveScorecardDefinition`, and `activateScorecardDefinition`. The Scorecards
screen lists memberships and provides nested classifier editors. New runs can
pass `scorecard_id` and an optional `scorecard_definition_revision`; the server
freezes that definition fingerprint and its exact classifier revisions.

Each classifier has a read-only history drawer backed by `classifierVersions`.
It shows the original name, question, and ordered classes for each immutable
revision. “Edit from this revision” copies that revision into an edit draft;
inspection alone never publishes a configuration or starts model work. Saving
the draft creates a new revision rather than modifying the inspected revision.
Scorecard links carry `scorecard=<id>` in the URL, so reload and browser Back
restore the selected scorecard without changing its active definition.
An optional `scorecard_revision=<revision>` restores a read-only historical
definition. `scorecardClassifiers` accepts the same optional revision and resolves
the exact pinned classifier names, questions and ordered classes—not the latest
catalog versions. Historical inspection has no edit controls; activation remains
an explicit “Use this definition” action. Missing revisions and failed history
requests show an explicit status rather than loading indefinitely. The revision
selector retains the requested revision; no nested classifier query or activation
control is shown for an unavailable definition. “Return to active definition”
changes navigation only, and browser Back restores the requested revision.
A classifier edit lists all active
scorecard definitions affected by saving it and explains that joint requests can
affect their other classifiers while existing runs remain unchanged.

Unsent multi-classifier votes and explanations are stored in browser session
storage under their immutable prediction presentation. Inspecting another run
and returning restores that presentation's draft, without recording feedback or
starting model work. The draft hook also isolates presentations when the reviewer
changes items without remounting, and ignores delayed updates from an old item.
Malformed or unavailable browser storage does not prevent labeling. Drafts are
local to the browser session; they are not saved human labels or server records.

`scorecardDefinitionComparison` compares an active and inspected definition
directionally. The read-only comparison drawer shows name changes, added/removed
members, changed positions and pinned revisions, original classifier questions
and class roles, and changed shared settings. Missing settings are distinct from
explicit null values. This is configuration comparison, not evidence that either
definition performs better; performance comparison requires matched evaluation.

Labeling and timeline are viewport-sized app layouts. Run history opens in a
left drawer; live optimizer activity opens in a right drawer. Opening either
drawer does not restart a cycle or make model calls. Narrow-screen navigation
uses a full-viewport modal with the current workspace marked accessibly. The
menu traps keyboard focus, hides background controls, and restores its trigger
on Escape. Drawers also restore their external opener when it is still mounted.
Close controls and drawer footer buttons have explicit 44-pixel minimum touch
targets; drawer headers and footers include safe-area padding. Drawer content
scrolls independently of its pinned header and actions. Ordinary vertical scroll
navigates content; horizontal scroll pans the timeline; pinch and zoom buttons
control timeline scale. **Fit all cycles** restores the entire recorded range
after zooming, rather than a fixed window around the selected event. Previous,
Next and Play advance through recorded events; playback makes no model calls.
There is no separate fullscreen mode.

The labeling workspace has a compact **Flywheel activity** summary, separate
from acknowledgement that feedback was saved. It names the classifier and stage,
then shows the latest recorded optimizer, validation, evaluation, ML fitting or
selection phase. A response received is not an accepted change. Routine no-op
trigger checks remain in the drawer without hiding the last optimization outcome.
Paused and failed work is explicit; the UI does not retry it. **Inspect activity**
opens that event and its correlated model request/response, using stored step
and classifier identities rather than substituting a nearby call.

The activity inspector also shows **Before** and **After** for the same recorded
optimization step: applied rubric, main/supporting questions and class order,
example membership, dynamic elements, and raw passthrough versus learned ML
features/calibration. A proposed rubric is not substituted for the applied
snapshot. Example cards resolve labels and text only from that step's recorded
optimizer training context; missing content stays explicitly unavailable. Older
steps without matching snapshots do not borrow another step's configuration.

Live event connections resume from the last delivered sequence and discard
duplicates. Closed-connection frames cannot change the active cursor. A missing
acknowledgement, transport error or unexpectedly ended subscription reconnects
the read-only stream; it does not resubmit labels or retry model work. Leaving
the view cancels both connection and retry timers. Existing authentication/access
denials stop automatic retries and are shown in the stream status. Job state is
recovered independently through the normal API polling.

### Reproduce calibration playback without paid calls

Create a new **empty, separate** fixture directory. This script refuses an
existing workspace database, so synthetic labels cannot enter your real study.

```bash
.venv/bin/python scripts/seed_calibration_playback.py --output /tmp/cyclotron-playback-demo
.venv/bin/python -m decision_flywheel.web_server serve --database /tmp/cyclotron-playback-demo/workspace.sqlite3 --port 8784
```

Open `http://127.0.0.1:8784/` and select **OFFLINE FIXTURE — calibration playback**.
The fixture has three worker cycles, two independent classifiers, six synthetic
labels, and three shared fake decision calls. It uses the production worker,
GraphQL ingestion, and SQLite storage, but makes no network or optimizer calls.
Do not enable live calls when serving it. It is UI evidence, not experimental
evidence that optimization improves a classifier.

Open **Confidence calibration at this cycle** in the inspector. At the first
prediction, no curve is available. At the third prediction, only the first two
reviewed samples are available. Advancing through its metrics event adds the
third sample. Seek backward to restore an earlier curve. **Play** follows these
same recorded events. **Inspect classifier** switches to the other independent
history, whose opposite labels produce a different curve. Snapshot provenance
retains the prediction and feedback event IDs and identifies decision passthrough.

The selected history is part of the app URL: `classifier=<classifier-id>` in
the hash selects a member of the run's frozen scorecard. Reload, Back/Forward,
and switching between labeling and timeline retain this selection. Unknown
classifier IDs are removed from the URL and the run's first classifier is used;
they never load another run's history. Opening or changing this view does not
submit feedback or start model work.

Replay creation uses `createReplay` with an explicitly authorized request budget,
source run and scorecard definition. It freezes source trace labels and comments
for items labeled for every selected classifier at the same definition revision.
Missing labels require human backfill first. `replay-next` predicts the next item
then applies that frozen feedback through the ordinary flywheel. Step runs one
cycle; Run continues until paused, complete, or failed. Pause waits for the current
cycle. Failed paid work is not automatically retried. Source history and catalog
feedback are not rewritten or duplicated.

Pause stops replay after its current cycle. A failure clears automatic playback;
clearing that failure does not restart playback without a fresh user action.
Restart resumes from persisted item and cycle state. If feedback was already
saved before process loss, recovery completes the unfinished cycle and records
its metrics without repeating prediction or optimization. Already completed
cycles stay complete. Source-label edits after replay creation cannot change its
frozen input labels or explanations.

Calibration snapshots use the same latest 200 unique human-reviewed pre-vote
predictions as live metrics. Ten fixed confidence bins retain support, observed
accuracy and mean confidence, with ECE, multiclass Brier and log loss. Snapshot
provenance identifies prediction/feedback events, versions, and raw decision
passthrough versus temperature-calibrated ML heads. Calibration fits only on
training out-of-fold predictions, never on this descriptive rolling window.
Matched raw/calibrated comparisons exclude passthrough predictions. Mixed-version
rolling curves are not estimates of current-model held-out calibration. Playback
uses only snapshots recorded at or before its cursor; missing older evidence is
shown as unavailable rather than reconstructed from future state.

Calibration graphs include a collapsed **Calibration bins** disclosure. It shows
mean confidence, observed correctness, and support for each populated bin without
requiring hover. The same component serves individual and raw/final curves; its
native summary supports touch and keyboard interaction. Empty or missing bins
are not shown as measurements. Details remain collapsed to preserve labeling space.

An active human review stays bound to the prediction originally reviewed.
Correcting its label or explanation refreshes its position in the latest-200
window, but cannot substitute a later retrospective score, probability vector,
or model version. Retraction releases that binding for a fresh review. Undo's
explicit saved-prediction reuse restores the original binding even if other
scores for that item were recorded meanwhile. All classifier references are
resolved before retraction; missing or mismatched provenance is an error, not
permission to borrow another output. Previously recorded snapshots stay immutable.

Open **Snapshot provenance** under a confidence curve to inspect the recorded
samples. Each sample identifies its item, prediction and feedback event IDs,
model version, and whether the output came from a calibrated ML head or raw
decision passthrough. Expand a head sample to see its recorded calibration
method, fit origin, temperature, and trusted training-item IDs. Missing legacy
fields say “not recorded.” These disclosures read the immutable snapshot;
opening them does not fit a model, re-score an item, or make a provider call.

Label-only predictions still contribute to recall, precision and accuracy.
Calibration, Brier and log loss use only recorded complete probability vectors;
snapshots and the UI disclose missing-vector counts. The metric window is chosen
from the latest 200 reviewed items before omitting unavailable probabilities.
Candidate selection remains strict: it requires complete distributions rather
than comparing partial losses. Both legacy reviewer and scorecard sessions read
their original prediction traces instead of rebuilding binary probabilities from
displayed confidence.
## Output comparison and shared defaults

## Shared defaults

“Edit scorecard” includes model identifiers, partition seed, label-transition and
feedback cadences, and primary/secondary selection objectives with secondary
regression limits and an optional score floor. Blank settings use application
defaults. Saving publishes a new immutable definition; existing runs retain their
frozen settings. New-run setup inherits the scorecard objective unless explicitly
overridden. Classifier-specific policies take precedence, and each classifier's
positive class or macro aggregation is resolved separately. Saving settings does
not call a model or start optimization.

The local server dispatches `decisions_provider` explicitly: `jev` uses the Jev
SDK and `kev` uses the local Kev server at `http://127.0.0.1:8009`. Jev is the
default for older runs that did not record a provider. Both paths send the full
joint scorecard request, with per-classifier rubric, labeled examples, and
feature questions. Both preserve requests, answers, probability vectors and
usage. A model identifier alone is not a provider switch.

Provider selection belongs to immutable shared defaults and frozen run
configuration. Changing it in the editor clears the previous provider's model
identifier. An explicit run override does the same unless a new model is also
supplied. Kev defaults to `kev-latest`; Jev defaults to `jev-1.13.0`. Saving a
definition does not construct a model or open a connection. Kev must be running
before live classification. Applications can select a different endpoint with
`KevConfiguration` in their injected factory; the default workspace uses the
local endpoint above.

Custom Jev and Kev endpoints are part of the configured model's cache identity.
An opaque SHA-256 suffix distinguishes servers without exposing endpoint details
in the identity; a trailing slash does not create a different identity. Default
Jev and default local Kev identities remain unchanged. Older custom-endpoint
cache entries are retained but are not reused under the new identity: the next
request is a cache miss and still requires the normal live-call permissions and
request ceiling. Do not relabel or migrate those old answers to a new server.

The default workspace also accepts `laya`, using the optional local Laya package.
Its adapter sends the complete structured scorecard state and all scoped
questions, including rubrics, actual labeled examples, and dynamic inputs.
Conservative token-budget checks reject oversized state, questions, or options
before inference instead of relying on Laya's internal truncation. Reported
response truncation is also rejected and remains visible in the trace.
Install and load the optional package and checkpoint before live use. Offline
transport and lifecycle tests prove the wiring; they do not demonstrate that
Laya uses few-shot context effectively or establish real-model accuracy.
Unknown providers reject in the default server; custom hosts can inject their
own model factory and explicit provider/model identifiers. There is no silent
Jev fallback and no completed three-provider experiment claim.

## Correct a recorded scorecard label

The API command `correct` accepts an item ID and one or more classifier labels:

```json
{
  "item_id": "paper-id",
  "labels": [{
    "classifier_id": "classifier-id",
    "expected_feedback_id": "original-command:classifier-id",
    "label": "include",
    "comment": "The corrected explanation"
  }]
}
```

Submit this through `submitCommand` with a new, stable request ID. The expected
feedback ID must identify that classifier's current vote; stale edits reject
before any label changes. A repeat of the same command is idempotent. The item
must have completed its original feedback submission. Frozen replay labels
cannot be edited: correct the source and create another replay instead.

The correction appends catalog feedback and a traced human-correction cycle;
it never overwrites the original label event or displayed model prediction.
Changed training or development evidence invalidates dependent inferred rubric,
feature configuration and fitted head through the core's reconciliation path.
Explanation-only edits also count as evidence changes. Updated explanations
enter the next optimizer context, but protected audit labels and explanations
remain excluded. The affected classifier records a new latest-200 metrics and
calibration snapshot against its original pre-vote prediction. Other classifier
labels remain unchanged, and a new scorecard checkpoint records the active state.

The reusable Rich reviewer also sends only eligible training explanations by
default, matching the web session's firewall. Its optimizer prompt includes
training article metadata, trusted labels, comments, and recorded pre-vote answers
with disagreement indicators; absent predictions remain unknown. Programmatic
callers can explicitly enable `include_protected_guidance=True` for preference
exploration, but that permanently marks the runtime's evaluation context as
exposed. It cannot be presented as independent evaluation afterward. Historical
runs made with the former permissive default are not retroactively declared clean.

Classifier-question discovery can propose either `tasks` or `dynamic_elements`,
never both in one candidate. The dynamic-input alternative leaves the rubric,
fixed examples, and question definitions unchanged. Only `current_datetime` is
allowlisted; it supplies request-time UTC as `state.current_datetime`. An empty
list removes the input. This is state data, not generated executable code or an
extra classifier output. The candidate follows the normal development coverage,
complete-request budget, numerical refitting, and promotion checks. Its recorded
`control_under_test` distinguishes this experiment from question discovery.
Rubric and example stages cannot change dynamic inputs. Retrieval and long-text
filter optimization remain deferred.

Numerical training compares retained question measurements against the same
incumbent. Each addition, wording/answer-option revision, or removal changes one
question only; all other questions, rubric, and examples remain fixed. Alternative
measurements with the same question name are separate candidates, not silently
discarded by name. Both natural and equal-class fits use the configured objective
and safeguards. Only the winning candidate is activated, with freshly fitted
feature columns and calibration provenance. Removing a deployed question does
not delete its retained hypothesis, so later feedback can justify another trial.
Request ceilings still apply to the search; interrupted work requires explicit
recovery rather than automatic paid retries.

Before prediction or fitting sends a request, the core checks an adapter's
declared labeled-context capability against the effective demonstrations in that
request. A training pool alone does not make a request few-shot, and an example
excluded because it is the target does not count as transmitted context. An
unsupported few-shot request fails before reserving or calling the provider.
The shared-request coordinator checks every classifier scope, including sibling
examples, and rechecks prepared answers before reuse. Its per-classifier adapters
expose the underlying provider's declared capabilities rather than hiding them.

Correction itself makes no model calls and does not automatically optimize.
The next normal prediction uses the reconciled state; an explicit optimization
command can rebuild it sooner. Explicit recovery after a lost trace/API
acknowledgement finishes reconciliation without duplicating feedback or paid
requests. Current trigger cadence counts unique active items, not old and
corrected versions as separate training samples.

### Undo an item's labels

Submit `undo` with an empty payload and a stable request ID to retract all local
classifier labels for the item with the most recent active local feedback. This
is an item-level undo, not a revert to the previous label value. Append-only
catalog retractions keep original votes and corrections available as history;
superseded votes do not become active again. A later review creates fresh votes.
Inherited source labels in a missing-label/backfill run remain read-only and are
not retracted. Frozen replay inputs cannot be undone.

The runtime reconciles affected classifiers, records new metrics and calibration
snapshots, checkpoints the scorecard, and reopens the item with its original
displayed prediction. Each classifier records a `displayed-prediction-reused`
event referencing that original prediction. No decision or optimizer request is
made. Suspended review cycles let the human submit new labels normally, including
after a restart. A durable undo plan allows explicit interrupted-command recovery
without retracting another item or duplicating completed work.

If a correction or undo job is failed or interrupted, explicitly call
`resumeFeedbackCommand(runId: ..., jobId: ...)`. This requeues the original
command identity and payload, records its previous failure result in history,
and is idempotent while pending, running or complete. It refuses prediction,
label-submission, replay and optimization jobs, since those can incur new model
calls. It also refuses recovery while other run work is pending or while that
run's scorecard edition is inactive. No automatic paid retry is introduced.

The scorecard labeling UI offers **Edit recorded feedback** and **Undo item
labels** in right-hand drawers. Opening either drawer makes no changes. The
correction editor sends only changed labels or explanations and retains the
original expected feedback identity, so a concurrent update cannot silently
replace the vote being edited. Inherited source feedback remains read-only;
frozen replay runs do not offer editing controls.

Closing the correction drawer, switching reviewed items, reloading the page, or
leaving and returning to the run retains unsaved edits in browser session storage,
scoped by run. Completing a correction discards only that item's draft, not drafts
for other items. Stored snapshots are validated before use; unavailable storage
does not prevent editing but cannot provide reload durability. These local drafts
are not saved labels and do not transfer to another browser or device.

If saved feedback changes after editing began, Save is disabled and the old draft
is retained. **Discard draft and load saved feedback** explicitly replaces it with
the current recorded vote; only a new human edit can then submit a correction.
The server still checks the expected feedback identity for races after that check.
Queued commands show pending feedback; failed or interrupted
correction/undo commands expose **Recover feedback command** for explicit recovery
of the original command, not a new paid retry.

If any run-command acknowledgement is lost, the app retains the command ID and
complete intent (run, command kind and payload) in browser session storage. An
explicit retry of the same intent, including after reload, asks the API for the
original job instead of creating duplicate work. This journal never executes
commands by itself. Acknowledged submissions leave the journal; a subsequent
deliberate command receives a fresh ID. Changed labels, explanations, targets or
command kinds are different intents. With storage unavailable, this protection
lasts only while the current app instance stays open; it does not span devices.

## Paired output comparison

Each new cycle-metrics snapshot includes a matched comparison of the raw main
decision-model answer and the final outer classifier. Both use the same latest
200 human-reviewed pre-vote items. Older records without a raw answer are excluded
from both sides and counted explicitly. This is descriptive historical agreement,
not a held-out evaluation or a newly scored current model.

The labeling card's “Raw decision model vs final classifier” disclosure shows
recall, precision, accuracy, and overlaid reliability curves, with ECE and Brier
scores. This is distinct from uncalibrated versus calibrated ML-head probabilities.
Existing stored metric snapshots remain unchanged; the new comparison is captured
when subsequent cycles record their metrics, using the available historical traces.
Timeline playback also exposes this comparison at the selected cycle. It shows
only snapshots recorded at or before the cursor; reverse seeking removes future
snapshots. Metric snapshots pin each classifier's class order and positive role.

Protected scoreboard, rolling-audit, and final-audit votes do not count toward
learning cadence or rubric label-transition triggers. They can update descriptive
evaluation metrics, but cannot trigger optimizer or retraining work. This rule
applies to scorecard sessions, the older reviewer path, and operational replays.
Older runs collected before this correction may have had learning cadence affected
by protected votes; their history is not rewritten or claimed as a clean protected
experiment.

## Protected matched-run evaluation

Comparison recall and precision use the configured positive classes as one
positive-versus-rest group. If no positive class is configured, they use a macro
average across all configured classes. Results record and display this choice.
Undefined class rates remain undefined, with the unsupported classes disclosed;
the comparison does not silently drop them or substitute zero.

The read-only `matchedRunPreflight` query selects at most 200 shared items with
matching pinned item content, classifier definitions, and protected human labels.
It rejects legacy runs without the recorded protection protocol or a checkpoint
event boundary. Any item previously exposed as learnable feedback in either run
is excluded. Duplicate structured values are compared after Unicode NFC and
whitespace normalization of string values; other field values and case are
preserved. This is not semantic deduplication or a guessed source-specific text
projection. Missing previously learned content rejects preflight because its
duplicates cannot be checked. The plan records the exclusion policy; an older
approval with different rules requires a fresh preflight and comparison.
Selection favors recent items and stratifies by the first common
classifier; counts for every classifier are disclosed, not assumed balanced.

The reusable `evaluate_matched_runs` executor restores each exact frozen head and
context. It retains all classifiers in each endpoint's joint request, including
outputs not being compared. Examples come only from training feedback available
at that checkpoint. It neither fits nor promotes models. An explicit request
ceiling must cover the preflight before any model call. Complete requests are
cached durably; a recorded evaluation datetime remains fixed on resume. Failed
calls are not retried automatically. Observer events expose recorded exchanges
and the matched result for an application to persist through its API.

`createMatchedComparison` requires explicit live-call confirmation, the approved
preflight fingerprint, and a sufficient request ceiling. It atomically saves the
frozen source inputs and queues a separate read-only comparison run in SQLite.
The worker never opens a learning session for that job. Its exchanges and result
are acknowledged through the same GraphQL trace API, with durable event identities
for safe re-delivery. Source runs and active editions remain unchanged. Interrupted
or failed jobs do not automatically retry paid requests.

The app sends a stable `requestId` with initial comparison approval. SQLite
commits that identity, authorization, frozen inputs and job atomically. If the
acknowledgement is lost, retrying unchanged controls returns the original run,
including after restart or later source changes; it does not queue another job.
Changing endpoints, preflight or ceiling starts a new approval identity. Reusing
an identity with different authorization is rejected. API clients should also
send `requestId` for this guarantee; legacy calls without it create new runs.

No paid evaluation starts from the preflight query, navigation, or saving a
definition. “Compare runs” opens a drawer with endpoint selection and a read-only
protected-sample check. Changing endpoints invalidates its prior approval. The
drawer shows the shared item count, per-class balance, and request upper bound;
execution requires the paid-call checkbox and an adequate ceiling. Results open
as a separate recorded run, showing recall, precision, accuracy, ECE, Brier and
per-endpoint reliability curves. Raw-versus-final comparison disclosures for each
endpoint use exactly the same
protected targets and paired probability vectors. The display reports final-minus-
raw recall, precision and accuracy in percentage points, plus ECE and Brier
differences. Lower calibration scores are better; a difference is descriptive,
not proof of statistical significance. Live reviewed-item snapshots use the same
calculation but retain their distinct, non-held-out scope.

Each protected endpoint also exposes F1, per-class reviewed/predicted counts,
and a Wilson 95% accuracy reference interval in a collapsed detail. F1 uses the
same positive-versus-rest or macro definition as the reported recall/precision.
The interval is descriptive: it is not corrected for model selection, adaptive
labeling, or stratified sampling, and it is not an interval for a paired change.
Old reports without recorded intervals remain explicitly unavailable.

The individual-predictions table shows protected
human labels beside raw and final answers, with a disagreement filter. “Inspect”
loads only the selected recorded target trace: item content, complete joint state
with rubric and actual examples, all questions and responses, final outputs,
provider exchanges, and checkpoint/request fingerprints. Older results without
those trace links cannot be inspected or reconstructed silently. Protected labels
are recorded alongside the exchange for scoring, never sent in target state.
Failed/interrupted jobs remain visible and do not automatically retry.
`resumeMatchedComparison` requires renewed confirmation and a stable request
identity. The UI offers a total attempt ceiling and a separate opt-in to retry
failed or interrupted model requests. Complete responses always use the cache.
Duplicate resume submissions with identical content return the existing job;
changed content under that identity is rejected. Resume preserves frozen inputs,
records its authorization, and appends a new job without overwriting failure
history. Completed or already-busy comparisons cannot start new resume work.
The ceiling includes all earlier attempts from both endpoints, not merely the
calls remaining in this invocation. Failed attempts count, and prior ceilings
cannot be reduced during resume. API jobs are serialized by the workspace worker.

To recover a stopped comparison, open that comparison in run history and inspect
its failed job first. Raise the total ceiling if prior failed attempts consumed
the allowance. Enable failed-call retries only when you intend to repeat those
calls, then approve and resume. If the submission acknowledgement is lost, submit
again without changing the controls: the same submission identity prevents a
second queued job. Changing the ceiling or retry option requires new approval.
You do not need to recreate the comparison or rerun completed requests.

## Offline browser acceptance

To exercise replay creation and playback without provider credentials, use a new,
empty fixture directory:

```bash
.venv/bin/python scripts/seed_calibration_playback.py --output /tmp/cyclotron-offline-check --serve --port 8785
```

The command refuses an existing workspace database. It creates synthetic items,
labels and a versioned two-classifier scorecard, then serves the real UI and
GraphQL/SQLite worker on loopback only. Decision clients are injected fakes;
optimizer calls are forbidden. This is a technology acceptance fixture, not an
experiment or evidence of model quality. Its authorization checkbox exercises
the normal UI gate but cannot enable a paid provider in this fixture.

Open the printed URL, choose **Run history → New run → Replay existing
feedback**, select the fixture source, and create a replay. Check that it starts
at zero cycles, **Step replay** records one cycle, reload preserves that position,
and **Run replay** finishes all three. Inspect prediction and feedback markers to
see the complete joint request, response and saved explanation. An empty or
single-class history must still render all configured class filters, without
inventing labels or metric support. Stop the fixture server when finished; keep
these synthetic records separate from a real labeling workspace.

To check pause, click **Run replay** and then **Pause replay** while a cycle is
working. The current cycle completes; no next cycle should start. Reload must
leave the run paused at its saved cycle. Click **Run replay** again to finish
the remaining cycles. The three-item fixture finishes quickly, so a pause click
after completion does not prove pause behavior. Catalog feedback remains the
original six labels: replay records their use in its own trace, not duplicate
human votes in the catalog.

Add `--pending-item` to the fixture command to test labeling-space behavior.
It saves three reviews and their calibration snapshots, then leaves a fourth
item predicted but unlabeled. Open **Label items** and expand a classifier's
calibration curve. The classifier strip is bounded to half the available card
height and scrolls internally; article reading and the bottom review actions
retain their own space. This mode uses the same synthetic-data safeguards.
The classifier panel is a named Tab stop. With it focused, Page Down/Page Up
scroll expanded details inside that panel rather than the app page.

Add `--optimizer-activity --pending-item` in a new fixture directory to exercise
the learning lifecycle as well. This mode records forty alternating synthetic
reviews per classifier through the same API/worker, with cadence-triggered
example/question optimization and transition-triggered rubric refinement. The
optimizer is a local scripted callable; its recorded tool calls are fixture
data, not executed external tools. Numerical fitting and candidate selection
use the real library. A forty-first item remains ready for interactive labeling.
Inspect the trigger, request, response, fit and evaluation events, then submit
synthetic feedback and watch the command acknowledgement and activity status.
The fake decision probabilities deliberately do not establish model quality.
No mode loads provider credentials or constructs a paid client. Seeding this
larger fixture takes longer because the real numerical fits run locally.
