# Context-compatible ML head replay

The earlier replay fitted candidate heads, but accepted provisional rubric
changes dropped the active head. Only 9 of 87 pre-feedback predictions used a
learned head. This was a lifecycle defect, not evidence that feature learning
was ineffective.

The shared candidate fitter now rebuilds context-specific probability features,
fits on trusted training labels, calibrates using out-of-fold predictions, and
returns a complete context/head pair. Activation saves that pair together and
rejects mismatched provenance. Failed feature collection leaves the incumbent
unchanged. Below three training labels per class, fitting is explicitly deferred.
Provisional acceptance is not proof of a measured improvement.

A fresh chronological replay used the same 87 existing votes and fixed split:
51 training, 18 development, and 18 protected audit items. Optimizer outputs
were collected afresh and can differ from the earlier recording. The replay
made 528 decision requests and 11 optimizer calls. The head was used continuously
from Cycle 34 through Cycle 87: 54 predictions. Fitting occurred at Cycles
33, 40, 41, 60, 61, and 80. The final active model used nine independent
probability features from five classifications. Each question's last class
probability is recoverable from the remaining probabilities.

The endpoint audit made 36 additional decision requests and no optimizer calls.
Both the final Jev-only path and learned-head path reused the exact same final
context and cached answers. The protected audit contains 2 include and 16 exclude
labels. It was not used for fitting or proposing context changes.

| Metric | Final context, Jev alone | Same context, learned head |
| --- | ---: | ---: |
| Accuracy | 72.2% | 83.3% |
| Include precision | 20.0% | 0.0% |
| Include recall | 50.0% | 0.0% |
| Balanced accuracy | 62.5% | 46.9% |
| Multiclass Brier | 0.3048 | 0.2285 |

The accuracy and natural-weighted Brier gains do not establish improved
alignment: the head missed both positives. The lifecycle repair is verified,
but generalization of the learned decision boundary remains unresolved. Two
positive audit items give very imprecise recall estimates. Do not tune against
these protected outcomes or claim calibration quality from this comparison.
The complete flywheel's prequential agreement was 77/87 (88.5%); that is a
different statistic from final-model audit accuracy and does not isolate the
head's contribution.

Prediction events now record the original decision-model probabilities, ML
feature values, raw ML probabilities, calibrated final probabilities, and
temperature. Confidence is the final probability of the selected class.

Private recordings are under `var/head-lifecycle-replay-20261006-v1/`:
`trace.json`, `runtime.sqlite3`, `run-comparison.json`, and
`head-comparison.json`. Article text and human explanations are not published
in this note. The previous run is preserved. No new human labels were collected.
