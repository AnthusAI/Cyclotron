# Decision Flywheel

Decision Flywheel is a reusable Python library for decisions that improve with human feedback.
The human supplies labels and optional explanations.
An LLM optimizer uses this feedback to propose changes to the decision rubric.
The decision model answers the rubric questions.
A fitted ML model uses those answers to predict the final label.
Code measures each change before it replaces the active version.

The flywheel improves two parts of the classifier.
The LLM optimizer adjusts the rubric and defines the classification tasks for the decision model.
It sets each task's question, answer options, and criteria. It also selects labeled examples.
The ML fitter learns how to combine the answers from the decision model.
Human feedback supplies the evidence for both changes.

## Design and implementation status

The diagrams below show the intended complete system.
They define the work that the library must support.
They do not prove that the current reviewer runs all these steps.

The live reviewer now calls the reusable `DecisionFlywheel` core.
The core analyzes eligible votes and comments, validates proposals, collects Jev features,
fits and calibrates the ML head, compares development results, and promotes or rejects a version.
Private SQLite records preserve the active version, request cache, and actual transcripts.
Offline tests cover this connected path. A successful paid live demonstration is a separate verification step;
passing tests does not establish live-model quality or improvement.

| Part | Current state |
| --- | --- |
| Human votes, comments, skip, and undo | Available in the reviewer |
| Fixed example selection | Optimizer proposes a list; code validates it and measures the candidate |
| LLM analysis of feedback | Injected optimizer interface; opt-in OpenAI transport |
| Rubric and classification-task changes | Validated structured proposals, applied to the actual request |
| Feature conversion and ML fit | Connected to the core; trusted fitting and out-of-fold calibration |
| Candidate evaluation and promotion | Same-development Brier comparison; incumbent retained on failure |
| Optimizer and Jev inspection | Actual local prompts, replies, tool calls, request state/questions and answers |
| Live performance | Must be measured; no guaranteed improvement |

## Terms

Use these terms with the same meaning throughout the system.

| Term | Meaning |
| --- | --- |
| Item | One article or other record to classify |
| Label | The human's final decision for an item |
| Feedback | A label, an optional comment, and the prediction shown before the vote |
| Rubric | The criteria that explain which label an item should receive |
| Decision element | One question or programmatic input in the scorecard |
| Scorecard | The saved set of decision elements and their definitions |
| Decision model | A model, such as Jev, that answers the scorecard questions |
| Feature | A numerical input derived from a decision-element answer |
| ML model | The fitted decision head that maps features to a final prediction |
| LLM optimizer | The agent that analyzes feedback and proposes structural changes |
| Candidate | A proposed version that has not passed its evaluation |
| Active version | The saved version used for current predictions |
| Development set | Labeled items used to compare candidates |
| Audit set | Labeled items used to measure alignment outside the optimization loop |

## The classifier at the center of the flywheel

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/classifier-dark.png">
  <img src="docs/diagrams/classifier-light.png" alt="The LLM optimizer adjusts the rubric, few-shot example collection, and element classification tasks in the decision-model request. The returned main and element answers become features for the custom ML model.">
</picture>

[Editable D2 source](docs/diagrams/classifier.d2)

The request contains the new item in `state.target`.
It contains three adjustable parts:

- `state.rubric`: criteria for the main Include or Exclude decision.
- `state.examples`: the selected collection of labeled few-shot examples.
- `questions`: the main decision and optimizer-defined element classification tasks.

The main question starts as “Should this item be included?”
Its instructions can refer to the evolving rubric in `state.rubric`.
The optimizer experiments with the example collection and the element tasks.
Each element task defines its question, answer options, and criteria.
The main answer and element answers become features for the custom ML model.
Per-item example retrieval is a later exploration.

This classifier generalizes the mechanism demonstrated in Jev Flywheel.
The LLM optimizer controls two definitions: the final-label rubric and the decision model's classification tasks.
The rubric describes what the human wants included or excluded.
Each classification task asks the decision model to identify an attribute of the item.
The optimizer can add, remove, or change tasks and their answer options and criteria.
These task answers supply the features for our custom ML model.
They are intermediate classifications. The custom ML model produces the final Include or Exclude label.

For example, the rubric can favor recent papers with practical evaluation methods.
One task can classify the method as practical or theoretical.
Another task can classify the paper's age against the supplied current date.
The fitter learns how these answers relate to the human's labels.

The scorecard defines a holistic question and additional decision-element questions.
Jev answers these questions for the same item in one request.
The feature converter derives named numbers from the answers and their probabilities.
The trained decision head combines these features to predict the final label.
Calibration adjusts the head's confidence.

The holistic Jev answer can be one input to the decision head.
It does not replace the head's final classification.
Additional questions supply evidence that the holistic question can miss.
Human labels determine how the fitted head uses that evidence.

The active version is the saved classifier definition.
It contains the questions, examples, feature rules, fitted weights, and calibration.
It is configuration for the prediction path.
Human review is a separate step after classification.

The proof-of-concept mechanism is visible in
[Jev Flywheel scoring](https://github.com/AnthusAI/Jev-Flywheel/blob/main/jev_flywheel/scoring.py)
and [feature conversion](https://github.com/AnthusAI/Jev-Flywheel/blob/main/jev_flywheel/features.py).
The generalized library must preserve this mechanism through provider adapters.

## Collect human feedback

1. Load the active version.
2. Send the item, rubric questions, and selected examples to the decision model.
3. Convert the answers to numerical features.
4. Apply the fitted ML model to the features.
5. Show the item and its predicted label to the human.
6. Save the prediction before the human supplies a label.
7. Save the label and optional explanation.

For the article study, the labels are `include` and `exclude`.
The interface shows the title, abstract, date, categories, authors, and publication citation when available.
The human can skip an item or undo a vote.
An undo must invalidate dependent candidates and training records when necessary.

The active version contains the scorecard, example policy, feature rules, fitted weights, and calibration.
It also identifies the source model and training data.
These parts must remain together during save, load, and restart operations.

At the start, the system has few labels and no known personal rubric.
It must identify its initial prediction as a baseline.
Initial accuracy is unknown. It is not necessarily poor.
The system needs examples of both labels before it can learn their difference.

## Analyze feedback and improve the system

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/improvement-dark.png">
  <img src="docs/diagrams/improvement-light.png" alt="Human feedback guides the LLM optimizer to revise the rubric, few-shot example collection, and element classification tasks. Code validates the changes, collects features, retrains the custom ML model, and evaluates promotion. Prompts, responses, and measured results are recorded.">
</picture>

[Editable D2 source](docs/diagrams/improvement.d2)

The optimizer receives training items, human labels, comments, prediction errors, and the current scorecard.
It searches for criteria that explain the human's decisions.
It states a possible rule and identifies the training feedback that supports it.
The rule is a hypothesis until evaluation supports it.

For example, a human can prefer recent papers about practical evaluation methods.
The optimizer can propose a question about practical evaluation.
It can also propose a `current_datetime` element if the feedback suggests a time-dependent criterion.
This example illustrates a possible change. It is not a finding from the current study.

The optimizer controls three parts of the decision-model request:

- Select better labeled examples for the decision model.
- Revise the main decision's rubric.
- Add, change, or remove element classification tasks and their options and criteria.

Code retrains the custom ML model from the resulting features and trusted labels.

Code validates each proposal against the schema, data rules, feature limits, and request budget.
Code collects missing answers for the candidate questions.
An unchanged request can reuse a saved answer.
A changed question requires a new answer and a new feature identity.

The fitter uses trusted training labels and complete feature rows.
It derives numerical weights from the data.
It accounts for the probability that each training item was selected for review.
It calibrates confidence from out-of-fold predictions.
The optimizer cannot supply weights or calibration values.

Code compares the candidate and active version on the same development items.
It promotes the candidate only if the declared improvement rule passes.
Otherwise, it keeps the active version and records the result.
A promoted version supplies predictions for subsequent items.
Their human feedback starts the next round.

The first article study uses titles and abstracts.
Each request still needs a context-size check that includes its questions and examples.
Optimization of extraction rules for longer articles is deferred.

## Separate learning from evaluation

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/evaluation-dark.png">
  <img src="docs/diagrams/evaluation-light.png" alt="Training records feed rubric analysis, example selection, and ML fitting. Development labels compare candidates. Ongoing audit and permanent holdout labels independently score saved classifier versions and never enter learning.">
</picture>

[Editable D2 source](docs/diagrams/evaluation.d2)

Assign each item to its data partition before the human supplies a label.
Save the assignment so a restart cannot change it.
Keep partitions disjoint by item identity and normalized text.
An item must not appear among its own examples.

Training records can enter the optimizer prompt, example selection, and ML fit.
Development labels can score candidates. They must not enter the optimizer's feedback briefing or ML fit.
Repeated candidate searches can make development scores optimistic.
Use independent audit data to measure the active version.
Keep permanent holdout labels outside all optimization steps.
Use the permanent holdout for a final test after the version is frozen.

Start with full human review.
Reduce the review rate only when sufficient audit evidence supports the change.
Keep a random audit sample as the review rate decreases.
Errors on uncertain items alone do not give an unbiased accuracy estimate.
Record review-selection probabilities and report the sample size.
Increase review if recent alignment falls or the human's criteria change.

The report must show accuracy, per-class recall, probability quality, and uncertainty.
It must show recent results as well as results across the full study.
It must track votes required, active elements, example counts, training rounds, model requests, latency, and token use.
Report each result with its version and measurement partition.

## Show the optimizer's work

The application must show the latest optimizer exchange and its measured result.
The user must be able to inspect:

- The exact prompt and training feedback supplied to the optimizer.
- The returned response and structured tool calls.
- The proposed rubric rule and question changes.
- The current questions and feature definitions.
- The example list selected for each candidate.
- The ML training status, training counts, and fitted model version.
- The evaluation results and promotion decision.

Show the optimizer's stated explanation. Do not claim access to private model reasoning.
Store transcripts locally because they can contain article text and human comments.
Never put credentials in a prompt, transcript, event record, or committed artifact.

The reusable library emits events that an application can display.
The application does not own a separate optimization loop.
The saved state must preserve progress across restarts.
It must distinguish a completed step, a failed step, and a step that has not run.
It must also show when new feedback makes the active version stale.

## Try the current implementation

The offline commands make no paid calls. The live commands explicitly authorize bounded paid collection.

Create the environment and install the offline dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
make PYTHON=.venv/bin/python test
make PYTHON=.venv/bin/python demo
```

The offline demo uses synthetic labels and a fake decision model.
It makes no network calls.
It saves an example-selection artifact and a summary in `demo-output/`.
Its results describe the synthetic fixture, not live-model performance.

Install the reviewer and Jev dependencies:

```bash
.venv/bin/pip install -e '.[reviewer,jev,optimizer]'
```

For a new study, download the article batch and collect votes without paid models:

```bash
.venv/bin/python scripts/seed_arxiv_reviewer.py --output var/arxiv-review.jsonl --limit 250
.venv/bin/python -m decision_flywheel.reviewer --database var/reviewer.sqlite3 --articles var/arxiv-review.jsonl
```

The download uses the public arXiv metadata snapshot. It records the observed Hub revision,
selection parameters and local batch hash. The dataset-server rows endpoint is not revision-pinned.
The saved local batch is reused on later runs, without downloads or overwrites.
Article text, votes and comments remain in ignored `var/` files. Do not publish them.

For an existing study, resume the same queue:

```bash
make PYTHON=.venv/bin/python review-local
```

Use `I` for Include or `E` for Exclude.
Enter an optional comment after the vote.
Use `S` to skip, `B` to undo, and `Q` to quit.
Local mode uses a baseline predictor.

For the connected live labeling demo, start or resume with:

```bash
make PYTHON=.venv/bin/python review
```

This command authorizes at most 500 Jev requests and 10 optimizer requests per session.
The defaults are `DECISIONS_PROVIDER=jev`, `DECISIONS_MODEL=jev-1.13.0`, and
`OPTIMIZER_MODEL=gpt-6-luna`. Override `DECISIONS_PROVIDER`, `DECISIONS_MODEL`, `OPTIMIZER_MODEL`,
`REVIEWER_REQUESTS`, `OPTIMIZER_CALLS`, or `OPTIMIZE_EVERY` in the make command.
`make review-arxiv` reuses or downloads the batch and starts this same **paid live mode**.

The initial rubric is empty. Jev supplies the warm-up prediction; no local replacement head is used.
The first round waits for at least three training votes per declared class
and two development votes per class. A permanent ID-hash rule assigns 25% of otherwise eligible records
to development before their labels are known. Existing rolling and final audit roles remain protected.
The demo reviews all displayed items; training review propensities are recorded as 1.0.
Adaptive review-rate reduction remains deferred until there is a validated random-audit rule.

After each 10 new votes, the core attempts another measured round. Use `G` to request one sooner.
Each round can make three optimizer calls: one for the rubric, one for the
example collection, and one for supporting classification questions. Each trial
changes only its assigned control. All trials use the same incumbent; the best
qualifying trial is promoted only after the comparisons finish.
The persistent idea registry keeps unsuccessful ideas and their measured outcomes.
New feedback permits a new trial. Untested ideas get priority, but ideas that have
waited three cycles get a retry opportunity even when new ideas keep arriving.
Combination trials are not implemented yet.
The display reports class counts, active rubric/questions/features, fitting and promotion events.
Use `O` for the actual optimizer prompt, response and tool calls; `J` for the actual Jev request and answer;
and `F` for active classifier details. These inspection commands do not add votes.
Use `H` to inspect retained hypotheses and past outcomes. Use `T` to select one
and explicitly authorize a new fit/comparison using current feedback. Rejection
does not erase the rubric/questions/examples or declare the idea false forever.
New labels produce a new trial. Retrying a retained structure skips rediscovery
by the optimizer; it still validates examples, collects needed features, fits
numerical weights, and requires measured improvement before promotion.
The connected reviewer currently supports the Jev adapter. The provider-neutral
flags are `--decisions-provider` and `--decisions-model`; other adapters are not
yet connected to this full classifier-request path.
Historical prediction agreement combines the predictions you saw from different
versions. It is not current-model accuracy. Current-version rolling-audit agreement
is displayed separately, with its reviewed sample count (or explicitly not measured).
An interrupted optimizer round is shown as interrupted, not as a successful older
promotion. Use `R` and confirm to retry that round within the session's paid ceilings,
only when no other reviewer session is running. This does not silently retry failed
decision-model requests.
If a prediction fails or the paid ceiling is reached, feedback can still be saved.
The display says prediction unavailable; it does not quietly substitute another classifier.

The ML fitter uses trusted training labels with full feature coverage, propensity weighting,
and out-of-fold calibration. By default, promotion uses **equal-class development Brier**:
compute multiclass Brier loss within each true class, then average those class means
equally. No evaluation records are discarded or duplicated. The display also shows
natural-distribution accuracy/Brier, balanced accuracy, per-class recall and counts,
and descriptive Wilson 95% recall intervals. These intervals are not adjusted for
repeated candidate selection. Two items per class is only a coverage floor, not
evidence of precise performance. Missing classes have no fabricated balanced score.

The reusable library accepts `evaluation_weighting="equal_class"` (default) or
`"natural"`, independent of `training_class_weighting="natural"` (default) or
`"equal_class"`. Equal-class training scales review-selection-corrected weights
so every class has equal total influence. Each out-of-fold fit computes its weights
from its own training fold; calibration still uses natural, review-selection-corrected
out-of-fold weights. Class weighting can change probability calibration and is not
a promise of improvement. Policy settings are included in round cache fingerprints;
training weighting is recorded in fitted-head provenance.

CLI controls are `--evaluation-weighting`, `--training-class-weighting`, and
`--min-evaluation-per-class` (reviewer default: 2). No code depends on labels named
Include or Exclude. This repeated development score is selection evidence, not unbiased accuracy.
The make variables are `EVALUATION_WEIGHTING`, `TRAINING_CLASS_WEIGHTING`, and
`MIN_EVALUATION_PER_CLASS`. To opt into training balance as well, use
`make PYTHON=.venv/bin/python review TRAINING_CLASS_WEIGHTING=equal_class`.
Displayed pre-vote agreement is also affected by showing recommendations to the reviewer.
Do not use either number as the sealed holdout score.

Restart reloads the active classifier and private transcripts. A completed round is not repeated
for identical feedback. Interrupted requests require explicit retry authorization; they are not silently repaid.
Undo or corrected training feedback invalidates dependent inferred configuration and fitted state.

### Replay existing human feedback from scratch

This diagnostic replay uses the earlier single-proposal round. The connected
reviewer and the isolated-control experiment below use the three-control scheduler.

Freeze a private snapshot and print the request budget without contacting models:

```bash
.venv/bin/python scripts/replay_arxiv_feedback.py --output var/arxiv-replay-v1
```

Then authorize a bounded real replay with `--confirm-live --max-requests 500
--max-optimizer-calls 5`. Use the same output directory as its preflight.
The source database is opened read-only and is never changed. Existing original
audit labels remain excluded; this retrospective experiment reserves new disjoint
training, development, and audit roles from training-eligible historical votes.
It starts without a rubric, examples, supporting questions, or fitted head.
Votes and comments are revealed in recorded arrival order, in batches of 20.
Development feedback is delayed too; insufficient class coverage postpones a round.

Recent evaluation selects the newest available audit items per class, up to five
per class. It walks backward to find scarce-class votes and drops older surplus
majority-class votes. An absent class produces no balanced score. No previously
learned item moves into evaluation. A separate, fixed balanced audit curve uses
the final audit selection at every checkpoint for like-for-like comparisons;
this retrospective curve can use labels not yet available to the simulated user,
but those labels never reach learning. Small per-class counts are shown explicitly.

Private `results.json` contains each active rubric, question definitions, examples,
round outcome, and both evaluation views. The separate runtime database contains
actual optimizer/decision exchanges and fitted-head evidence. Paid runs are not
mock optimization; offline specs use fake transports only. A previously started
live output directory is refused rather than silently replayed and repaid.
These historical labels may reflect recommendations shown during live review,
and they informed prior experiments. This is a retrospective diagnostic, not an
independent prospective performance claim. A fresh future audit is still needed.

### Compare the three controls on frozen feedback

After freezing a feedback snapshot with replay preflight, inspect the separate
experiment without contacting models:

```bash
.venv/bin/python scripts/experiment_arxiv_controls.py --snapshot var/arxiv-replay-v2/reviews.sqlite3 --output var/arxiv-controls-v1
```

Add `--confirm-live --max-requests 227 --max-optimizer-calls 3` to authorize
collection for this 87-vote snapshot. Other snapshots can require different
ceilings; read their preflight first. Use a new output directory for each experiment.
This experiment fixes equal-class training weighting for all three trials.
Its development comparison uses equal-class Brier loss, and its separate balanced
audit does not enter discovery, fitting, or candidate selection.

The first real isolated-control experiment used 65 training labels (4 Include,
61 Exclude), four development labels (two per class), and four independent audit
labels (two per class). All trials began with the same empty rubric and no
examples, supporting questions, or fitted head.

| Isolated change | Development agreement | Development Brier loss | Outcome |
| --- | --- | --- | --- |
| Incumbent | 2/4 | 0.5795 | Shared baseline |
| Rubric only | 2/4 | 0.9327 | Retained, not promoted |
| Examples only | 3/4 | 0.3920 | Promoted in experiment runtime |
| Supporting questions only | 2/4 | 0.9169 | Retained, not promoted |

The optimizer selected eight examples. It also proposed three supporting questions:
knowledge extraction and curation, memory or data systems, and research assessment
or interactive refinement. These questions were actually sent to Jev; their answers
became features for a separately fitted ML head. Their failure to win this trial
does not establish that the ideas are false or cannot help with more labels.

On the separate audit, the selected examples-only classifier agreed with 4/4 labels,
versus 2/4 for the incumbent. Four papers cannot establish general accuracy or a
reliable effect size. This is retrospective evidence from existing human feedback,
not a prospective study. Training weighting also differs from the earlier replay,
so differences between those two experiments do not isolate the scheduler's effect.
Collection used 219 Jev requests and three GPT-6 Luna requests. Full exchanges and
human feedback remain in ignored private runtime files. This experiment does not
replace the live reviewer's active classifier. Restart the reviewer to load the new
scheduler; future rounds use that reviewer's own persisted feedback and active version.

The following older commands perform context-only search and legacy artifact serving.
They do not run the connected rubric/features/head loop:

```bash
make PYTHON=.venv/bin/python run-flywheel
make PYTHON=.venv/bin/python review-live
```

Load credentials from the environment or a gitignored `.env` file.
Do not source `.env` in the shell.

## Library interfaces and provider adapters

`DecisionTask`, `Item`, and `LabeledItem` define the decision task and its trusted data.
`ClassifierConfig` owns the rubric, fixed example IDs, classification tasks and allowlisted dynamic inputs.
`OptimizerAgent` accepts an injected completion callable and reports actual messages.
`DecisionFlywheel` owns `predict`, `improve`, `reconcile_feedback`, `history` and `close`.
`ReviewerFlywheel` is a thin article-record adapter; the terminal has no fitting or promotion logic.
`improve_example_list` and `search_context_policies` compare example policies.
Frozen artifacts preserve the selected policy and its provenance.
`JsonlRunLedger` records measured rounds.
`JsonlEventStream` records request and trial activity.
These older text-free records are separate from the core's private SQLite transcript history.

`fit_learned_head` fits the optional ML head.
`run_steering_round` tests structural changes with a scripted analyst and an application-supplied fitter.
The connected path uses these core fitting guards without importing the Jev-Flywheel proof of concept.

The complete-request byte ceiling is a conservative local safety check, not returned token usage.
The Jev demo pins a version instead of relying on an alias that can move.
See [TypeSafe's model limits and alias policy](https://docs.typesafe.ai/models).

Jev, Kev, and Laya have separate adapters.
Provider-specific behavior belongs in an adapter.
Use `[jev]`, `[kev]`, or `[laya]` to install the corresponding optional dependencies.
The Laya adapter does not claim support for labeled context examples.
Request counters include attempted calls and failures.
Returned token use is different from an estimated context size.

Study methods and cross-model results belong in
[Decision-Flywheel-Evaluations](https://github.com/AnthusAI/Decision-Flywheel-Evaluations).
The earlier [Few-Shot-Jev study](https://github.com/AnthusAI/Few-Shot-Jev) provides experimental background.

## Documentation and project tools

The explanations use short sentences, active verbs, and the technical names defined above.
They follow the writing approach in [ASD-STE100](https://www.asd-ste100.org/STE_faq.html).
A full dictionary conformity review has not been completed.

Archify generates the diagrams from saved JSON specifications.
The HTML files support interactive inspection and export.
The SVG files provide static images for this README.
The editable D2 sources are linked below each diagram. Render each source with
`d2 --layout elk --theme 0 --pad 30 --scale 2` for light mode, or use theme `200`
for dark mode. The PNGs above are the README display artifacts.

Kanbus stores project tasks in Git.
Semantic Release uses conventional commits to produce releases.
Install these tools with `make install-tools`.
Inspect the task board with `kanbus list`.

## License

The repository code has an MIT license.
Downloaded datasets and model weights retain their upstream terms.
