"""Small statistics for element screening (Stage 2d), plain Python: numpy is only an optional extra of this package.

* ``auc``           Mann-Whitney area under the ROC curve, ties share their average rank.
* ``cv_gain``       repeated stratified K-fold cross-validated gain of ``logistic(logit(main) + element)`` over
                    ``logistic(logit(main))``, in log-loss and in accuracy, with a bootstrap lower bound.
* ``pearson``       plain correlation, used for redundancy between elements.

Everything is deterministic given the seed. These are screening statistics on a few hundred stories, not tests;
forward shadow evaluation stays the arbiter.
"""
import math
import random

CLIP = 1e-3          # main probabilities are clipped to [CLIP, 1 - CLIP] before the logit
L2 = 1e-2            # ridge on the slopes (not the intercept), so a perfect separator still has a finite fit


def auc(scores, positives):
    """P(score of a random positive > score of a random negative) + half the ties; None if either class is empty."""
    pairs = sorted(zip(scores, positives))
    n_pos = sum(1 for _, p in pairs if p)
    n_neg = len(pairs) - n_pos
    if not n_pos or not n_neg:
        return None
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        average_rank = (i + j) / 2 + 1
        rank_sum += average_rank * sum(1 for k in range(i, j + 1) if pairs[k][1])
        i = j + 1
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def pearson(x, y):
    n = len(x)
    if n < 2 or n != len(y):
        return None
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    if not sx or not sy:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def logit(p, clip=CLIP):
    p = min(1 - clip, max(clip, p))
    return math.log(p / (1 - p))


def sigmoid(z):
    return 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))


def _solve(matrix, vector):
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        a[col], a[pivot] = a[pivot], a[col]
        if abs(a[col][col]) < 1e-12:
            a[col][col] = 1e-12
        for r in range(col + 1, n):
            factor = a[r][col] / a[col][col]
            for c in range(col, n + 1):
                a[r][c] -= factor * a[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (a[r][n] - sum(a[r][c] * x[c] for c in range(r + 1, n))) / a[r][r]
    return x


def fit_logistic(rows, y, l2=L2, iterations=25):
    """Ridge-penalised logistic regression by Newton's method. ``rows`` are feature lists (no intercept column).
    Returns weights [intercept, *slopes]."""
    k = len(rows[0]) + 1
    w = [0.0] * k
    for _ in range(iterations):
        grad, hess = [0.0] * k, [[0.0] * k for _ in range(k)]
        for row, target in zip(rows, y):
            x = [1.0, *row]
            p = sigmoid(sum(a * b for a, b in zip(w, x)))
            weight = max(p * (1 - p), 1e-9)
            for i in range(k):
                grad[i] += (p - target) * x[i]
                for j in range(k):
                    hess[i][j] += weight * x[i] * x[j]
        for i in range(1, k):
            grad[i] += l2 * w[i]
            hess[i][i] += l2
        step = _solve(hess, grad)
        w = [a - b for a, b in zip(w, step)]
        if max(abs(s) for s in step) < 1e-9:
            break
    return w


def predict(w, row):
    return sigmoid(w[0] + sum(a * b for a, b in zip(w[1:], row)))


def log_loss(probabilities, y, eps=1e-12):
    return -sum(math.log(max(eps, p if t else 1 - p)) for p, t in zip(probabilities, y)) / len(y)


def folds(y, k, rng):
    """Stratified fold index per sample: each class is shuffled, then dealt round-robin."""
    assignment = [0] * len(y)
    for cls in (0, 1):
        members = [i for i, t in enumerate(y) if t == cls]
        rng.shuffle(members)
        for position, i in enumerate(members):
            assignment[i] = position % k
    return assignment


def percentile(values, q):
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def cv_gain(main, element, y, *, k=5, repeats=5, seed=0, bootstrap=500, level=0.90, min_per_class=5):
    """Cross-validated incremental value of ``element`` over ``main``.

    ``main`` is P(positive class) from the main decision, ``element`` is P(yes) of the element question, ``y`` is
    1 for the positive class. In each of ``repeats`` stratified ``k``-fold splits both models are fitted on the
    training folds and scored on the held-out fold: base = logistic(logit(main)), augmented = logistic(logit(main)
    + element). Per story, log-loss gain = base loss - augmented loss (positive is better), averaged over the
    repeats; accuracy gain compares the out-of-fold decisions at 0.5 (a tie at exactly 0.5 predicts positive).

    ``mean_*`` average the per-story gains. ``lower_*`` is the lower end of the central ``level`` percentile interval
    of ``bootstrap`` resamples of those per-story gains (the 5th percentile for level 0.90). It reflects which
    stories were drawn, not the variation of the fit itself. Returns None when a class has fewer than
    ``min_per_class`` stories."""
    y = [int(bool(t)) for t in y]
    n = len(y)
    if n != len(main) or n != len(element) or min(sum(y), n - sum(y)) < min_per_class:
        return None
    base_x = [[logit(p)] for p in main]
    aug_x = [[logit(p), e] for p, e in zip(main, element)]
    rng = random.Random(seed)
    gain_ll, gain_acc = [0.0] * n, [0.0] * n
    base_ll, aug_ll, base_hit, aug_hit = 0.0, 0.0, 0.0, 0.0
    for _ in range(repeats):
        assignment = folds(y, k, rng)
        for fold in range(k):
            train = [i for i in range(n) if assignment[i] != fold]
            test = [i for i in range(n) if assignment[i] == fold]
            if not test or len({y[i] for i in train}) < 2:
                continue
            wb = fit_logistic([base_x[i] for i in train], [y[i] for i in train])
            wa = fit_logistic([aug_x[i] for i in train], [y[i] for i in train])
            for i in test:
                pb, pa = predict(wb, base_x[i]), predict(wa, aug_x[i])
                lb = -math.log(max(1e-12, pb if y[i] else 1 - pb))
                la = -math.log(max(1e-12, pa if y[i] else 1 - pa))
                hb, ha = float((pb >= .5) == bool(y[i])), float((pa >= .5) == bool(y[i]))
                gain_ll[i] += (lb - la) / repeats
                gain_acc[i] += (ha - hb) / repeats
                base_ll += lb / repeats / n
                aug_ll += la / repeats / n
                base_hit += hb / repeats / n
                aug_hit += ha / repeats / n
    boot = random.Random(seed + 1)
    tail = (1 - level) / 2

    def lower(values):
        means = []
        for _ in range(bootstrap):
            means.append(sum(values[boot.randrange(n)] for _ in range(n)) / n)
        return percentile(means, tail)
    return {"n": n, "repeats": repeats, "folds": k,
            "base_log_loss": base_ll, "augmented_log_loss": aug_ll, "base_accuracy": base_hit,
            "augmented_accuracy": aug_hit,
            "mean_log_loss_gain": sum(gain_ll) / n, "lower_log_loss_gain": lower(gain_ll),
            "mean_accuracy_gain": sum(gain_acc) / n, "lower_accuracy_gain": lower(gain_acc),
            "bootstrap": bootstrap, "level": level}
