"""Root-cause diagnosis and element screening (hypothesis portfolio, Stage 2d).

Before the Stage 2c proposal call of each rubric round (shadow mode, opt-in) the engine now looks at the incumbent's
mistakes instead of handing the optimizer raw feedback and a hint:

1. ERROR SUMMARY  confusion matrix of the incumbent on revealed stories, from the predictions recorded before each
                  label was revealed.
2. SAMPLES        per error cell, disjoint confident-first samples of revealed training errors (the first
                  ``independent`` ones feed independent diagnoses, the last is held aside for the cause trial) and
                  contrast sets of correctly answered stories.
3. RCA CALLS      per cell one diagnosis call per independent sample, then one reconcile-and-critique call that
                  returns strictly validated ``causes`` (and generic candidate elements).
4. CAUSE TRIAL    the decision model scores incumbent rubric + each cause's patch on the held-aside errors and
                  contrast stories (a FILTER; forward shadow evaluation stays the arbiter).
5. ELEMENTS       yes/no element questions are asked in the SAME request as the main decision over a bounded sample of
                  revealed training stories; kept elements are registered as ``element`` ideas of the tasks control.

Visibility rules (same as Stage 2c's target sample): the optimizer sees text only of TRAINING stories whose label is
revealed, minus any story whose id or normalized text equals a development or protected story. The confusion counts
may include any revealed non-protected story (training or development), but only as counts. Nothing about a
protected, unrevealed or development story's text ever reaches a prompt.
"""
import json
import random
import re
from dataclasses import replace
from datetime import datetime, timezone

from .classification_metrics import classification_metrics
from .context import _normalized_text
from .element_stats import auc, cv_gain, pearson
from .flywheel import _hash
from .hypothesis_ledger import HypothesisLedger
from .models import DecisionTask
from .optimizer_agent import FeedbackBriefing

STORY_CHARS = 400            # characters of story text shown to the optimizer
EXPLANATION_CHARS = 300
MAX_CELLS = 3                # error cells analysed per round, largest first
MAX_HYPOTHESES = 4
RETRIES = 1                  # one corrective re-ask per invalid reply
MIN_MATRIX = 20              # fewer current-version predictions than this: the matrix uses every recorded prediction
MIN_ELEMENT_STORIES = 40     # element screening needs at least this many scored stories ...
MIN_ELEMENT_CLASS = 8        # ... and at least this many of each class
REDUNDANT_CORRELATION = 0.95 # an element this correlated with a kept one is not kept as well
LIMITS = {"name": 60, "description": 500, "rule": 400, "patch": 500, "question": 300}
NAME = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
CAUSE_ID = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


def _clip(value, width):
    text = " ".join(str(value).split())
    return text if len(text) <= width else text[:width - 1] + "…"


# -- 1. error summary ------------------------------------------------------------------------------------------------

def positive_label(wheel):
    policy = wheel.selection_policy
    return (policy.positive_class if policy and policy.positive_class else None) or wheel.initial.task.labels[0]


def recorded_predictions(wheel, ids):
    """item id -> {label, probabilities, version}: the newest main-decision prediction recorded BEFORE the story's first
    human-feedback event (so never a prediction made with the label known)."""
    ids = set(ids)
    first_feedback = {}
    for event in wheel.runtime_storage.events_of(('human-feedback',)):
        feedback=event.get('feedback')
        item=feedback.get('item_id') if isinstance(feedback,dict) else None
        first_feedback.setdefault(item,event['event_id'])
    out = {}
    for event in wheel.runtime_storage.events_of(('prediction',)):
        event_id,item=event['event_id'],event.get('target_id')
        label,probabilities,version=(event.get('decision_model_label'),event.get('decision_model_probabilities'),event.get('version'))
        if item in ids and label is not None and event_id < first_feedback.get(item,float('inf')):
            out[item]={'label':label,'version':version,
                      'probabilities':json.loads(probabilities) if isinstance(probabilities,str) else probabilities}
    return out


def direction(task, positive, true, answered):
    if len(task.labels) == 2 and true == positive:
        return f"missed {positive}"
    if len(task.labels) == 2 and answered == positive:
        return f"wrongful {positive}"
    return f"true {true}, answered {answered}"


def error_summary(wheel, rows, predictions):
    """The incumbent's confusion matrix on ``rows`` (revealed, non-protected stories) that have a recorded prediction.

    Predictions made under the current classifier version are used when there are at least ``MIN_MATRIX`` of them;
    otherwise every recorded prediction counts and ``scope`` says so. -> dict (structured) with ``text`` for prompts."""
    task, positive = wheel.initial.task, positive_label(wheel)
    have = [r for r in rows if r.item.id in predictions]
    current = [r for r in have if predictions[r.item.id]["version"] == wheel.active.fingerprint]
    scope, used = ("current-version", current) if len(current) >= MIN_MATRIX else ("all-versions", have)
    n = len(used)
    base = {"scope": scope, "n": n, "labels": list(task.labels), "positive_label": positive}
    if not n:
        return {**base, "accuracy": None, "confusion": {}, "cells": [], "text": "No recorded predictions on revealed stories yet."}
    metrics = classification_metrics(task.labels, [r.label for r in used],
                                     [predictions[r.item.id]["label"] for r in used], [None] * n,
                                     allow_missing_probabilities=True)
    confusion = metrics["confusion_matrix"]
    cells = []
    for true in task.labels:
        for answered in task.labels:
            count = confusion[true][answered]
            if true != answered and count:
                cells.append({"key": f"{true}->{answered}", "true": true, "answered": answered, "errors": count,
                              "rate_of_true_label": round(count / metrics["per_class"][true]["count"], 4),
                              "rate_of_all": round(count / n, 4), "direction": direction(task, positive, true, answered)})
    cells.sort(key=lambda c: (-c["errors"], c["key"]))
    shares = {label: {"actual": round(metrics["per_class"][label]["count"] / n, 4),
                      "answered": round(metrics["per_class"][label]["predicted_count"] / n, 4)} for label in task.labels}
    lines = [f"Incumbent on {n} revealed stories ({scope} predictions, recorded before each label was revealed): "
             f"accuracy {metrics['accuracy']:.1%}.",
             "Share of stories by label, actual versus answered by the incumbent: " +
             "; ".join(f"{label} {shares[label]['actual']:.0%} vs {shares[label]['answered']:.0%}" for label in task.labels) + "."]
    lines += [f"- {c['direction']} ({c['key']}): {c['errors']} stories = {c['rate_of_true_label']:.1%} of true {c['true']}, "
              f"{c['rate_of_all']:.1%} of all" for c in cells]
    return {**base, "accuracy": round(metrics["accuracy"], 4), "confusion": confusion, "cells": cells, "shares": shares,
            "per_class": {label: {k: metrics["per_class"][label][k] for k in ("count", "correct", "recall", "precision")}
                          for label in task.labels}, "text": "\n".join(lines)}


# -- 2. samples ------------------------------------------------------------------------------------------------------

def visible_rows(training, development, protected, task):
    """Training stories the optimizer may read: not a development or protected story by id or normalized text."""
    hidden_ids = {r.item.id for r in development} | {i.id for i in protected}
    hidden_text = {_normalized_text(r.item, task) for r in development} | {_normalized_text(i, task) for i in protected}
    return {r.item.id: r for r in training if r.item.id not in hidden_ids and _normalized_text(r.item, task) not in hidden_text}


def deal(ordered, ways, size, seed):
    """Disjoint, deterministic samples: the first ``ways * size`` stories of ``ordered`` are taken in consecutive groups
    of ``ways`` and each group is dealt to the samples in a seeded random order, so every sample spans the whole
    confidence range. Stories beyond ``ways * size`` are not used. -> list of ``ways`` lists, each in ``ordered`` order."""
    rng = random.Random(seed)
    out = [[] for _ in range(ways)]
    for start in range(0, min(len(ordered), ways * size), ways):
        slots = list(range(ways))
        rng.shuffle(slots)
        for offset, story in enumerate(ordered[start:start + ways]):
            out[slots[offset]].append(story)
    return out


def draw_samples(task, rows, predictions, cells, *, ways, size, contrast, seed):
    """-> {"errors": {cell key: [sample 0..ways-1 of record dicts]}, "contrast": [per sample {label: [records]}]}.

    Errors are ordered confident-first (the probability the incumbent put on its wrong answer, highest first);
    contrast stories are correct answers closest to the boundary (lowest confidence first), because those are the ones a
    patch can break. Ties break by id. Samples are disjoint across cells and across contrast labels."""
    def record(row):
        p = predictions[row.item.id]
        probs = p["probabilities"] or {}
        return {"row": row, "id": row.item.id, "true": row.label, "answered": p["label"], "probabilities": probs,
                "confidence": probs.get(p["label"], 0.0)}
    mine = [record(r) for r in rows.values() if r.item.id in predictions]
    errors = {}
    for cell in cells:
        pool = sorted((m for m in mine if (m["true"], m["answered"]) == (cell["true"], cell["answered"])),
                      key=lambda m: (-m["confidence"], m["id"]))
        errors[cell["key"]] = deal(pool, ways, size, f"{seed}:{cell['key']}")
    contrasts = [{label: [] for label in task.labels} for _ in range(ways)]
    for label in task.labels:
        pool = sorted((m for m in mine if m["true"] == label and m["answered"] == label),
                      key=lambda m: (m["confidence"], m["id"]))
        for index, part in enumerate(deal(pool, ways, contrast, f"{seed}:contrast:{label}")):
            contrasts[index][label] = part
    return {"errors": errors, "contrast": contrasts}


def show(wheel, rec):
    """The prompt form of a story record: truncated text, labels, the revealed human explanation, probabilities."""
    task, row = wheel.initial.task, rec["row"]
    values = dict(row.item.values)
    body = values.get(task.input_field)
    if body is None:
        body = " ".join(str(v) for v in values.values())
    explanation = row.context.get("human_feedback")
    return {"id": rec["id"], "text": _clip(body, STORY_CHARS), "true_label": rec["true"], "answered": rec["answered"],
            "probabilities": {k: round(v, 3) for k, v in rec["probabilities"].items()},
            "human_explanation": _clip(explanation, EXPLANATION_CHARS) if explanation else None}


# -- 3. strict validation of optimizer replies ------------------------------------------------------------------------

def _string(value, field, limit):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise ValueError(f"{field} is longer than {limit} characters")
    return value.strip()


def _ids(value, field, allowed, *, minimum):
    if not isinstance(value, list) or len(value) < minimum or any(not isinstance(v, str) for v in value) \
            or len(set(value)) != len(value):
        raise ValueError(f"{field} must be a list of {'at least ' + str(minimum) + ' ' if minimum else ''}unique story ids")
    unknown = [v for v in value if v not in allowed]
    if unknown:
        raise ValueError(f"{field} names ids that were not shown for this purpose: {', '.join(unknown[:3])}")
    return list(value)


def validate_diagnosis(reply, shown_error_ids):
    """One independent diagnosis: {"hypotheses": [{name, description, proposed_rule, evidence_story_ids}]}; every cited
    id must be an error shown in that sample."""
    if not isinstance(reply, dict) or set(reply) != {"hypotheses"}:
        raise ValueError('reply must be {"hypotheses": [...]}')
    items = reply["hypotheses"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_HYPOTHESES:
        raise ValueError(f"hypotheses must be a list of 1 to {MAX_HYPOTHESES} objects")
    names, out = set(), []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"name", "description", "proposed_rule", "evidence_story_ids"}:
            raise ValueError("each hypothesis needs exactly name, description, proposed_rule and evidence_story_ids")
        name = _string(item["name"], "name", LIMITS["name"])
        if name.lower() in names:
            raise ValueError("hypothesis names must be unique")
        names.add(name.lower())
        out.append({"name": name, "description": _string(item["description"], "description", LIMITS["description"]),
                    "proposed_rule": _string(item["proposed_rule"], "proposed_rule", LIMITS["rule"]),
                    "evidence_story_ids": _ids(item["evidence_story_ids"], "evidence_story_ids", shown_error_ids, minimum=1)})
    return out


def _element(value, field):
    if not isinstance(value, dict) or set(value) != {"name", "question"}:
        raise ValueError(f"{field} must be an object with exactly name and question")
    name = value["name"]
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError(f"{field}.name must be lowercase letters, digits and underscores, starting with a letter")
    return {"name": name, "question": _string(value["question"], f"{field}.question", LIMITS["question"])}


def validate_reconcile(reply, *, errors_by_cell, contrast_ids, max_causes, max_elements, taken_elements=()):
    """The reconciled causes. ``errors_by_cell`` maps cell key -> ids of errors shown (in any independent sample) for
    that cell; ``contrast_ids`` are the contrast ids shown. Rejects: unknown fields or cell, ids that were not shown or
    are not errors of the cause's own cell, duplicate cause ids or names, over-length text, an empty patch, a bad
    candidate element, conflicting element names. -> {"causes": [...], "generic_elements": [...]}."""
    if not isinstance(reply, dict) or not {"causes"} <= set(reply) <= {"causes", "generic_elements"}:
        raise ValueError('reply must be {"causes": [...], "generic_elements": [...]}')
    causes = reply["causes"]
    if not isinstance(causes, list) or len(causes) > max_causes:
        raise ValueError(f"causes must be a list of at most {max_causes} objects")
    fields = {"id", "name", "cell", "description", "evidence_story_ids", "proposed_rule", "patch",
              "must_keep_story_ids", "confirmed", "candidate_element"}
    ids, names, elements, out = set(), set(), {n: q for n, q in taken_elements}, []

    def register(element):
        if element["name"] in elements and elements[element["name"]] != element["question"]:
            raise ValueError(f"element name {element['name']} is used for two different questions")
        elements[element["name"]] = element["question"]
    for cause in causes:
        if not isinstance(cause, dict) or set(cause) != fields:
            raise ValueError("each cause needs exactly: " + ", ".join(sorted(fields)))
        cid = cause["id"]
        if not isinstance(cid, str) or not CAUSE_ID.match(cid) or cid in ids:
            raise ValueError("cause ids must be unique short strings")
        name = _string(cause["name"], "name", LIMITS["name"])
        if name.lower() in names:
            raise ValueError("cause names must be unique")
        if cause["cell"] not in errors_by_cell:
            raise ValueError("cell must be one of: " + ", ".join(sorted(errors_by_cell)))
        if not isinstance(cause["confirmed"], bool):
            raise ValueError("confirmed must be true or false")
        element = None if cause["candidate_element"] is None else _element(cause["candidate_element"], "candidate_element")
        if element:
            register(element)
        ids.add(cid)
        names.add(name.lower())
        out.append({"id": cid, "name": name, "cell": cause["cell"],
                    "description": _string(cause["description"], "description", LIMITS["description"]),
                    "evidence_story_ids": _ids(cause["evidence_story_ids"], "evidence_story_ids",
                                               errors_by_cell[cause["cell"]], minimum=1),
                    "proposed_rule": _string(cause["proposed_rule"], "proposed_rule", LIMITS["rule"]),
                    "patch": _string(cause["patch"], "patch", LIMITS["patch"]),
                    "must_keep_story_ids": _ids(cause["must_keep_story_ids"], "must_keep_story_ids", contrast_ids, minimum=0),
                    "confirmed": cause["confirmed"], "candidate_element": element})
    generic = reply.get("generic_elements", [])
    if not isinstance(generic, list) or len(generic) > max_elements:
        raise ValueError(f"generic_elements must be a list of at most {max_elements} objects")
    generic = [_element(g, "generic_elements[]") for g in generic]
    for element in generic:
        register(element)
    return {"causes": out, "generic_elements": generic}


def support(cause, diagnoses):
    """True only when the cause's evidence overlaps the evidence cited by EVERY independent diagnosis of its cell,
    and all of the cell's independent diagnoses exist. ``diagnoses`` is a list of validated hypothesis lists."""
    if not diagnoses or any(d is None for d in diagnoses):
        return False
    return all(set(cause["evidence_story_ids"]) & {i for h in d for i in h["evidence_story_ids"]} for d in diagnoses)


# -- optimizer access ------------------------------------------------------------------------------------------------

class _Stop(Exception):
    """Diagnosis cannot continue (optimizer ceiling or a failed call); carries the reason."""


class Asker:
    """Counts every optimizer call and never lets diagnosis eat the call the proposal step still needs."""

    def __init__(self, wheel, development, protected, keep=1):
        self.wheel, self.development, self.protected, self.keep, self.calls = wheel, development, protected, keep, 0

    def remaining(self):
        fn = getattr(self.wheel.optimizer, "calls_remaining", None)
        return fn() if callable(fn) else None

    def briefing(self, current):
        return FeedbackBriefing.build(self.wheel.initial.task, (), current=current,
                                      protected=tuple(r.item for r in self.development) + tuple(self.protected))

    async def ask(self, current, validate):
        """-> the validated reply, or None after a corrective retry also failed. Raises _Stop on a ceiling or a failed call."""
        error = None
        for attempt in range(1 + RETRIES):
            remaining = self.remaining()
            if remaining is not None and remaining < 1 + self.keep:
                raise _Stop("the optimizer-call ceiling leaves no room for another diagnosis call")
            self.calls += 1
            body = current if error is None else {**current, "previous_reply_error": str(error)}
            try:
                from .optimizer_agent import ask_optimizer
                return validate(await ask_optimizer(self.wheel.optimizer, 'diagnose', self.briefing(body)))
            except ValueError as problem:
                error = problem
                self.wheel._emit({"kind": "diagnosis-reply-rejected", "phase": current["diagnosis_phase"],
                                  "attempt": attempt + 1, "reason": str(problem)})
            except Exception as failure:
                raise _Stop(f"an optimizer call failed ({type(failure).__name__})") from None
        return None


# -- 4. cause trial --------------------------------------------------------------------------------------------------

def passes_trial(fixed, broke, n_errors):
    """A cause passes when it fixes at least max(2, 25% of) its held-aside mismatches and breaks no more than a third as
    many contrast stories as it fixes."""
    return n_errors > 0 and fixed >= max(2, 0.25 * n_errors) and broke * 3 <= fixed


class JevMeter:
    """Decision-model requests this round, from the wheel's own counter (cache hits are free)."""

    def __init__(self, wheel, cap, training, development):
        self.wheel, self.start = wheel, wheel.requests
        reserve = len(training) + 2 * len(development)  # the same headroom idea screening keeps for normal decisions
        self.cap = min(cap, max(0, wheel.max_requests - wheel.requests - reserve))

    @property
    def used(self):
        return self.wheel.requests - self.start

    @property
    def left(self):
        return self.cap - self.used


async def _label_under(wheel, config, row, training, now, meter):
    from .observability import RequestBudgetExhausted
    if meter.left <= 0:
        return None
    try:
        batch = await wheel._answers(config, row.item, training, now)
    except RequestBudgetExhausted:
        return None
    except Exception as error:  # a failed diagnosis score must never block normal decisions
        wheel._emit({"kind": "diagnosis-scoring-failed", "item_id": row.item.id, "error_type": type(error).__name__})
        return None
    return batch.answers["decision"].label


async def cause_trial(wheel, cause, held_errors, held_contrast, training, now, meter, scorer=None):
    """Score incumbent rubric + patch on the held-aside errors of the cause's cell and the held-aside contrast stories.
    fixed = held errors now answered with their true label; broke = held contrast stories that stop being right.
    -> {fixed, broke, n_errors, n_contrast, fix_rate, break_rate, passed, status, jev_calls}."""
    n_errors, n_contrast = len(held_errors), len(held_contrast)
    base = {"n_errors": n_errors, "n_contrast": n_contrast}
    try:
        rubric = (wheel.active.config.rubric.rstrip() + "\n" + cause["patch"]).strip()
        config = wheel.active.config.apply({"rubric": rubric}, training)
    except ValueError as error:
        return {**base, "fixed": 0, "broke": 0, "fix_rate": 0.0, "break_rate": 0.0, "passed": False,
                "status": f"not applicable: {error}", "jev_calls": 0}
    if n_errors < 2:
        return {**base, "fixed": 0, "broke": 0, "fix_rate": 0.0, "break_rate": 0.0, "passed": False,
                "status": "untestable: fewer than 2 held-aside mismatches in this cell", "jev_calls": 0}
    before, fixed, broke, scored = meter.used, 0, 0, 0
    score = scorer or (lambda row: _label_under(wheel, config, row, training, now, meter))
    for rec in held_errors:
        label = await score(rec["row"])
        if label is not None:
            scored += 1
            fixed += label == rec["true"]
    for rec in held_contrast:
        label = await score(rec["row"])
        if label is not None:
            scored += 1
            broke += label != rec["true"]
    complete = scored == n_errors + n_contrast
    return {**base, "fixed": fixed, "broke": broke, "fix_rate": round(fixed / n_errors, 4),
            "break_rate": round(broke / n_contrast, 4) if n_contrast else 0.0,
            "passed": complete and passes_trial(fixed, broke, n_errors),
            "status": "complete" if complete else "incomplete: the per-round decision-call cap or ceiling was reached",
            "jev_calls": meter.used - before}


# -- 5. element screening --------------------------------------------------------------------------------------------

def element_task_name(name):
    return "el_" + name


def element_metrics(y, main, true_labels, incumbent_errors, scores, *, seed=0, repeats=5, bootstrap=500):
    """Per-element statistics. ``y`` 1 for the positive class, ``main`` P(positive) of the main decision, ``scores`` is
    {element: [P(yes) per story]}.

    * auc_label: AUC of P(yes) for separating the positive class from the rest.
    * auc_error_by_label: within each true label, AUC of P(yes) for separating the incumbent's errors from its
      correct answers (None when a label has no errors or no correct answers in the sample).
    * cv: ``element_stats.cv_gain`` (repeated 5-fold CV gain in log-loss and accuracy of logistic(logit(main) + element)
      over logistic(logit(main)), plus the bootstrap lower bound); None when a class is too small."""
    out = {}
    for name, values in scores.items():
        by_label = {}
        for label in sorted(set(true_labels)):
            idx = [i for i, t in enumerate(true_labels) if t == label]
            by_label[label] = auc([values[i] for i in idx], [incumbent_errors[i] for i in idx])
        out[name] = {"auc_label": auc(values, [bool(t) for t in y]), "auc_error_by_label": by_label,
                     "cv": cv_gain(main, values, y, seed=seed, repeats=repeats, bootstrap=bootstrap)}
    return out


def choose_elements(metrics, scores):
    """Keep an element when its mean cross-validated log-loss gain is positive and the bootstrap lower bound is above
    zero. Candidates are considered by descending mean gain; one correlated at least ``REDUNDANT_CORRELATION`` with an
    already kept element is not kept. -> (kept names, {name: {kept, reason, redundancy}})."""
    verdicts, kept = {}, []
    order = sorted(metrics, key=lambda n: (-(((metrics[n]["cv"] or {}).get("mean_log_loss_gain")) or -1e9), n))
    for name in order:
        cv = metrics[name]["cv"]
        redundancy = {k: pearson(scores[name], scores[k]) for k in kept}
        redundancy = {k: (None if v is None else round(v, 4)) for k, v in redundancy.items()}
        worst = max((abs(v) for v in redundancy.values() if v is not None), default=0.0)
        if cv is None:
            verdicts[name] = {"kept": False, "reason": "too few stories of one class to cross-validate",
                              "redundancy": redundancy}
        elif not (cv["mean_log_loss_gain"] > 0 and cv["lower_log_loss_gain"] > 0):
            verdicts[name] = {"kept": False, "reason": (
                f"mean log-loss gain {cv['mean_log_loss_gain']:.4f}, bootstrap lower bound "
                f"{cv['lower_log_loss_gain']:.4f}: not above zero"), "redundancy": redundancy}
        elif worst >= REDUNDANT_CORRELATION:
            verdicts[name] = {"kept": False, "reason": "redundant with an already kept element", "redundancy": redundancy}
        else:
            kept.append(name)
            verdicts[name] = {"kept": True, "reason": (
                f"mean log-loss gain {cv['mean_log_loss_gain']:.4f}, bootstrap lower bound "
                f"{cv['lower_log_loss_gain']:.4f} above zero"), "redundancy": redundancy}
    return kept, verdicts


def balanced_recent(rows, labels, size):
    """Most recent first, alternating between labels, at most ``size`` stories."""
    queues = {label: [r for r in reversed(rows) if r.label == label] for label in labels}
    out = []
    while len(out) < size and any(queues.values()):
        for label in labels:
            if queues[label] and len(out) < size:
                out.append(queues[label].pop(0))
    return out


async def screen_elements(wheel, candidates, rows, training, now, meter, *, sample, seed, round_number, ledger):
    """Ask every candidate element in the same request as the main decision for a bounded sample of stories.

    ``candidates`` are {name, question, source}. The active configuration is not changed: the request uses a temporary
    configuration with the elements added as classification tasks. -> report dict."""
    from .observability import RequestBudgetExhausted
    task, positive = wheel.initial.task, positive_label(wheel)
    report = {"candidates": [c["name"] for c in candidates], "kept": [], "failed": [], "scored": 0, "skipped": None}
    if not candidates:
        report["skipped"] = "no candidate elements"
        return report
    config = replace(wheel.active.config, tasks=(*wheel.active.config.tasks, *(
        DecisionTask(element_task_name(c["name"]), ("yes", "no"), c["question"], task.input_field) for c in candidates)))
    chosen = balanced_recent(list(rows), task.labels, sample)
    y, main, true_labels, errors, scores = [], [], [], [], {c["name"]: [] for c in candidates}
    for row in chosen:
        if meter.left <= 0:
            break
        try:
            batch = await wheel._answers(config, row.item, training, now)
        except RequestBudgetExhausted:
            break
        except Exception as error:  # includes a request over the byte ceiling: stop, never crash a round
            wheel._emit({"kind": "diagnosis-scoring-failed", "item_id": row.item.id, "error_type": type(error).__name__})
            if isinstance(error, ValueError):
                break
            continue
        decision = batch.answers["decision"]
        if not decision.probabilities or positive not in decision.probabilities:
            continue
        y.append(int(row.label == positive))
        main.append(decision.probabilities[positive])
        true_labels.append(row.label)
        errors.append(decision.label != row.label)
        for c in candidates:
            scores[c["name"]].append(batch.answers[element_task_name(c["name"])].probabilities["yes"])
    report["scored"] = len(y)
    if len(y) < MIN_ELEMENT_STORIES or min(sum(y), len(y) - sum(y)) < MIN_ELEMENT_CLASS:
        report["skipped"] = (f"only {len(y)} stories were scored ({sum(y)} of the positive class); element screening "
                             f"needs {MIN_ELEMENT_STORIES} with at least {MIN_ELEMENT_CLASS} of each class")
        return report
    metrics = element_metrics(y, main, true_labels, errors, scores, seed=seed)
    kept, verdicts = choose_elements(metrics, scores)
    by_name = {c["name"]: c for c in candidates}
    for name in sorted(metrics):
        ledger.record_element_evaluation(name, round_number, question=by_name[name]["question"],
                                         source=by_name[name]["source"], n=len(y), metrics=metrics[name],
                                         kept=verdicts[name]["kept"], reason=verdicts[name]["reason"])
        (report["kept"] if verdicts[name]["kept"] else report["failed"]).append(
            {"name": name, "question": by_name[name]["question"], "source": by_name[name]["source"],
             "auc_label": metrics[name]["auc_label"], "mean_log_loss_gain": (metrics[name]["cv"] or {}).get("mean_log_loss_gain"),
             "lower_log_loss_gain": (metrics[name]["cv"] or {}).get("lower_log_loss_gain"),
             "mean_accuracy_gain": (metrics[name]["cv"] or {}).get("mean_accuracy_gain"), "reason": verdicts[name]["reason"]})
    return report


def register_elements(wheel, kept, round_number):
    """Kept elements become ideas of the tasks control with operator ``element``: the existing question path (feature
    bank, tasks control, ML head) can use them. Nothing is promoted or trained here. -> idea ids."""
    from .control_scheduler import ControlScheduler
    scheduler, ids = ControlScheduler(wheel), []
    for element in kept:
        question = {"name": element_task_name(element["name"]), "instructions": element["question"], "labels": ["yes", "no"]}
        evidence = {"stage": "element-screening", "round": round_number,
                    "mean_log_loss_gain": element["mean_log_loss_gain"], "lower_log_loss_gain": element["lower_log_loss_gain"]}
        idea_id = scheduler._admit(question, f"element kept by screening: {element['reason']}", evidence, round_number)
        idea = next(i for i in scheduler.ideas() if i["id"] == idea_id)
        if idea.get("operator") != "element":
            scheduler.save({**idea, "operator": "element", "parents": [], "novelty": None, "element": element["name"]})
        wheel._emit({"kind": "proposal-operator", "idea_id": idea_id, "operator": "element", "parents": [], "novelty": None})
        ids.append(idea_id)
    return ids


# -- the round -------------------------------------------------------------------------------------------------------

def seed_for(wheel, round_number):
    return int(_hash({"task": wheel.initial.task.fingerprint, "diagnosis": round_number})[:12], 16)


def optimizer_calls_planned(config, cells):
    """Calls a round makes when every reply is valid at once: independent diagnoses per cell plus one reconcile."""
    return config.independent * cells + 1


def _skip(wheel, result, reason):
    result["skipped"] = reason
    wheel._emit({"kind": "diagnosis-skipped", "reason": reason, "round": result["round"]})


def _finish(wheel, result, asker, meter):
    result["optimizer_calls"], result["jev_calls"] = asker.calls, meter.used
    result["accounting"] = f"diagnosis used {asker.calls} optimizer calls and {meter.used} decision-model calls"
    wheel._emit({"kind": "diagnosis-summary", "round": result["round"], "summary": result["summary"],
                 "causes": [{k: c[k] for k in ("id", "name", "cell", "confirmed", "trial")} for c in result["causes"]],
                 "elements": {"kept": [e["name"] for e in result["elements"]["kept"]],
                              "failed": [e["name"] for e in result["elements"]["failed"]]},
                 "optimizer_calls": asker.calls, "jev_calls": meter.used, "accounting": result["accounting"],
                 "skipped": result["skipped"]})
    return result


async def run_diagnosis(wheel, training, development, protected, *, now=None):
    """Run the diagnosis pipeline for this round. Never raises for budget reasons; returns a dict with ``summary``
    (structured, with ``text``), ``causes`` (each with its ``trial``), ``elements`` {kept, failed}, ``skipped`` (reason or
    None), ``optimizer_calls`` and ``jev_calls``. Returns None when diagnosis is off or unsupported here."""
    config = wheel.idea_screening.diagnosis
    if not config.enabled or not wheel.shadow_enabled() or wheel.idea_screening.effective_mix() is None:
        return None
    now = now or datetime.now(timezone.utc)
    task = wheel.initial.task
    wheel.learning_storage.ensure('rubric')
    round_number = wheel.learning_storage.count('rubric')
    ledger = HypothesisLedger(storage=wheel.hypothesis_storage)
    asker, meter = Asker(wheel, development, protected), JevMeter(wheel, config.max_jev_calls, training, development)
    revealed = [r for r in (*training, *development) if r.item.id not in {i.id for i in protected}]
    predictions = recorded_predictions(wheel, {r.item.id for r in revealed})
    summary = error_summary(wheel, revealed, predictions)
    result = {"round": round_number, "summary": summary, "causes": [], "elements": {"kept": [], "failed": []},
              "skipped": None, "optimizer_calls": 0, "jev_calls": 0}
    rows = visible_rows(training, development, protected, task)
    cells = [c for c in summary["cells"] if c["errors"] >= config.min_cell][:MAX_CELLS]
    cells = [c for c in cells if sum(1 for r in rows.values() if r.item.id in predictions and r.label == c["true"]
                                     and predictions[r.item.id]["label"] == c["answered"]) >= config.min_cell]
    if not cells:
        _skip(wheel, result, f"no error cell has at least {config.min_cell} errors among revealed training stories")
        return _finish(wheel, result, asker, meter)
    if not callable(getattr(wheel.optimizer, "diagnose", None)):
        _skip(wheel, result, "the optimizer has no diagnose method")
        return _finish(wheel, result, asker, meter)
    remaining = asker.remaining()
    planned = optimizer_calls_planned(config, len(cells)) + 1  # + the Stage 2c proposal call
    if remaining is not None and remaining < planned:
        _skip(wheel, result, f"the optimizer-call ceiling leaves {remaining} calls; diagnosis needs {planned - 1} plus "
                             "the proposal call")
        return _finish(wheel, result, asker, meter)
    ways = config.independent + 1
    samples = draw_samples(task, rows, predictions, cells, ways=ways, size=config.samples, contrast=config.contrast,
                           seed=seed_for(wheel, round_number))
    held = ways - 1
    diagnoses = {}
    try:
        for cell in cells:
            diagnoses[cell["key"]] = []
            for index in range(config.independent):
                errors = samples["errors"][cell["key"]][index]
                if not errors:
                    diagnoses[cell["key"]].append(None)
                    continue
                shown = {r["id"] for r in errors}
                current = {"diagnosis_phase": "independent", "sample": "ABCD"[index], "cell": cell,
                           "confusion_summary": summary["text"], "incumbent_rubric": wheel.active.config.rubric,
                           "errors": [show(wheel, r) for r in errors],
                           "contrast": [show(wheel, r) for label in task.labels for r in samples["contrast"][index][label]],
                           "max_hypotheses": MAX_HYPOTHESES}
                diagnoses[cell["key"]].append(await asker.ask(current, lambda reply, s=shown: validate_diagnosis(reply, s)))
        usable = [c for c in cells if any(d for d in diagnoses[c["key"]])]
        if not usable:
            _skip(wheel, result, "no independent diagnosis passed validation")
            return _finish(wheel, result, asker, meter)
        errors_by_cell = {c["key"]: {r["id"] for i in range(config.independent) for r in samples["errors"][c["key"]][i]}
                          for c in usable}
        contrast_ids = {r["id"] for i in range(config.independent) for label in task.labels
                        for r in samples["contrast"][i][label]}
        taken = [(e["element"], e["question"]) for e in ledger.element_evaluations()]
        taken += [(t.name[3:], t.instructions) for t in wheel.active.config.tasks if t.name.startswith("el_")]
        current = {"diagnosis_phase": "reconcile", "confusion_summary": summary["text"],
                   "incumbent_rubric": wheel.active.config.rubric, "max_causes": config.max_causes,
                   "element_candidates": config.element_candidates, "cell_keys": sorted(errors_by_cell),
                   "cells": [{"cell": c, "diagnoses": [d for d in diagnoses[c["key"]] if d],
                              "errors_seen": [show(wheel, r) for i in range(config.independent)
                                              for r in samples["errors"][c["key"]][i]]} for c in usable],
                   "contrast_seen": [show(wheel, r) for i in range(config.independent) for label in task.labels
                                     for r in samples["contrast"][i][label]]}
        reconciled = await asker.ask(current, lambda reply: validate_reconcile(
            reply, errors_by_cell=errors_by_cell, contrast_ids=contrast_ids, max_causes=config.max_causes,
            max_elements=config.element_candidates, taken_elements=taken))
    except _Stop as stop:
        _skip(wheel, result, str(stop))
        return _finish(wheel, result, asker, meter)
    if reconciled is None:
        _skip(wheel, result, "the reconcile reply failed validation after a corrective retry")
        return _finish(wheel, result, asker, meter)
    causes = []
    for cause in reconciled["causes"]:
        verified = cause["confirmed"] and support(cause, diagnoses[cause["cell"]])
        causes.append({**cause, "local_id": cause["id"], "id": f"r{round_number}.{cause['id']}", "round": round_number,
                       "confirmed_claimed": cause["confirmed"], "confirmed": bool(verified)})
    # 4. cause trial on the held-aside sample
    held_contrast = [r for label in task.labels for r in samples["contrast"][held][label]]
    for cause in causes:
        cause["trial"] = await cause_trial(wheel, cause, samples["errors"][cause["cell"]][held], held_contrast, training,
                                           now, meter)
        ledger.record_cause(round_number, {k: v for k, v in cause.items()})
        wheel._emit({"kind": "diagnosis-cause", "cause": {k: cause[k] for k in ("id", "name", "cell", "confirmed", "trial")}})
    result["causes"] = causes
    # 5. element screening
    candidates, seen = [], set()
    for cause in causes:
        if cause["candidate_element"]:
            candidates.append({**cause["candidate_element"], "source": cause["id"]})
    candidates += [{**g, "source": "generic"} for g in reconciled["generic_elements"]]
    unique = []
    done = {(e["element"], e["question"]) for e in ledger.element_evaluations()}
    deployed = {t.name for t in wheel.active.config.tasks}
    for c in candidates:
        if c["name"] in seen or (c["name"], c["question"]) in done or element_task_name(c["name"]) in deployed:
            continue
        seen.add(c["name"])
        unique.append(c)
    report = await screen_elements(wheel, unique, list(rows.values()), training, now, meter,
                                   sample=config.element_sample, seed=seed_for(wheel, round_number),
                                   round_number=round_number, ledger=ledger)
    if report["skipped"]:
        wheel._emit({"kind": "element-screening-skipped", "reason": report["skipped"], "scored": report["scored"]})
    else:
        register_elements(wheel, report["kept"], round_number)
        wheel._emit({"kind": "element-screening-completed", "scored": report["scored"], "kept": report["kept"],
                     "failed": [{k: f[k] for k in ("name", "mean_log_loss_gain", "lower_log_loss_gain")} for f in report["failed"]]})
    result["elements"] = {"kept": report["kept"], "failed": report["failed"], "scored": report["scored"],
                          "skipped": report["skipped"]}
    return _finish(wheel, result, asker, meter)


# -- what proposals and the optimizer digest read ------------------------------------------------------------------------

def passing_causes(diagnosis):
    return [c for c in (diagnosis or {}).get("causes", ()) if c["trial"]["passed"]]


def diagnosis_block(diagnosis):
    """Compact, bounded view of this round's diagnosis for the proposal prompt."""
    return {"confusion_summary": diagnosis["summary"]["text"],
            "causes": [{"id": c["id"], "name": c["name"], "cell": c["cell"], "description": c["description"],
                        "proposed_rule": c["proposed_rule"], "patch": c["patch"],
                        "confirmed": c["confirmed"],
                        "trial": {k: c["trial"][k] for k in ("fixed", "broke", "n_errors", "n_contrast", "passed", "status")}}
                       for c in diagnosis["causes"]],
            "passing_cause_ids": [c["id"] for c in passing_causes(diagnosis)],
            "kept_elements": [{k: e[k] for k in ("name", "question", "mean_log_loss_gain", "lower_log_loss_gain")}
                              for e in diagnosis["elements"]["kept"]],
            "failed_elements": [{k: e[k] for k in ("name", "question", "mean_log_loss_gain", "lower_log_loss_gain")}
                                for e in diagnosis["elements"]["failed"]]}


def validate_addresses(value, known):
    """A proposal's ``addresses``: a list of unique cause ids from this round's diagnosis (may be empty)."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value) or len(set(value)) != len(value):
        raise ValueError("addresses must be a list of unique cause ids")
    unknown = [v for v in value if v not in known]
    if unknown:
        raise ValueError(f"addresses names unknown cause ids: {', '.join(unknown[:3])}")
    return list(value)


def diagnosis_digest(ledger, limit=10):
    """What the optimizer digest carries across rounds: how each cause's fixes fared, and the screened elements."""
    out = {}
    outcomes = ledger.cause_outcomes()
    if outcomes:
        out["cause_outcomes"] = dict(list(outcomes.items())[-limit:])
    rows = ledger.element_evaluations()
    if rows:
        pick = lambda r: {"name": r["element"], "question": r["question"], "mean_log_loss_gain": r["mean_log_loss_gain"],  # noqa: E731
                          "lower_log_loss_gain": r["lower_log_loss_gain"], "round": r["round"]}
        out["elements"] = {"kept": [pick(r) for r in rows if r["kept"]][-limit:],
                           "failed": [pick(r) for r in rows if not r["kept"]][-limit:]}
    return out
