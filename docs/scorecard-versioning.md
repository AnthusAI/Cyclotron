# Scorecard versions and missing-label review

A scorecard definition has an identity, a name, an ordered list of classifier
revision references, shared settings, and an active definition revision. It can
exist without any optimization runs. Editing its name, membership, ordering or
settings creates an immutable revision. Editing a classifier creates a classifier
revision and new definitions for active scorecards containing that classifier.
Existing runs do not change. Definition activation changes the configuration for
future runs, not which historical run accepts feedback.

Run editions are a separate history: each immutable edition points to one
optimization run and its frozen classifier definitions. Classifier definitions
retain their own revision numbers. Runtime checkpoints
store the complete joint classifier state, including fitted ML heads, with a
content fingerprint. Complete decision requests retain their own cache keys.

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
an explicit “Use this definition” action. A classifier edit lists all active
scorecard definitions affected by saving it and explains that joint requests can
affect their other classifiers while existing runs remain unchanged.

`scorecardDefinitionComparison` compares an active and inspected definition
directionally. The read-only comparison drawer shows name changes, added/removed
members, changed positions and pinned revisions, original classifier questions
and class roles, and changed shared settings. Missing settings are distinct from
explicit null values. This is configuration comparison, not evidence that either
definition performs better; performance comparison requires matched evaluation.

Labeling and timeline are viewport-sized app layouts. Run history opens in a
left drawer; live optimizer activity opens in a right drawer. Opening either
drawer does not restart a cycle or make model calls. Ordinary vertical scroll
navigates content; horizontal scroll pans the timeline; pinch and zoom buttons
control timeline scale. There is no separate fullscreen mode.

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

The local demo server currently instantiates the Jev decision adapter and the
configured optimizer adapter. A model identifier is not a provider switch; the
reusable worker accepts an injected model factory for other providers.

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
