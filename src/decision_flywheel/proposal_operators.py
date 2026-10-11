"""Proposal operators (hypothesis portfolio, Stage 2c).

A shadow round used to ask the optimizer for several "distinct" rubrics and got timid rewordings of one rubric, so
shadow evaluation had nothing to find. Each operator is a different way to ask for an idea:

* ``mutate``  today's multi-proposal request (careful revisions of the incumbent).
* ``bold``    a structurally different rubric; a text-distance gate rejects near-copies.
* ``target``  a rubric aimed at a bounded sample of revealed stories the tried ideas keep getting wrong.
* ``combine`` a merge of the winning clauses of two COMPLEMENTARY ideas (fix different stories).

One optimizer call per round carries every operator's block and the reply labels each proposal with its operator.
A bold proposal rejected by the novelty gate may be re-requested (``novelty_retries`` extra calls, default 1, only
when something was rejected). Everything shown to the optimizer is capped (see ``OPERATOR_CAPS``).
"""
import json
import re

from .flywheel import _json
from .hypothesis_ledger import HypothesisLedger
from .idea_screening import OPERATOR_CAPS, OPERATORS, incumbent_id, validate_proposals

SHINGLE = 3


def _shingles(text):
    words = re.findall(r"[a-z0-9']+", str(text).lower())
    if len(words) < SHINGLE:
        return set(words)
    return {tuple(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def text_novelty(a, b):
    """1 - Jaccard similarity of lowercased word 3-shingles: 0 identical, 1 nothing shared. It sees surface overlap,
    not meaning: a paraphrase scores as novel, a light edit scores near 0. Two empty texts score 0."""
    x, y = _shingles(a), _shingles(b)
    if not x and not y:
        return 0.0
    return round(1 - len(x & y) / len(x | y), 4)


def nearest_novelty(text, references):
    """Smallest distance to any reference text (1.0 when there are none)."""
    return min((text_novelty(text, r) for r in references), default=1.0)


def _clip(value, width):
    text = " ".join(str(value).split())
    return text if len(text) <= width else text[:width - 1] + "…"


def target_stories(wheel, ledger, training, development, protected, sample):
    """A bounded, balanced sample of revealed past stories to aim at.

    Candidates: stories every tried idea got wrong, then the incumbent's newest errors. Only TRAINING rows can appear
    (their text and label were already revealed to the optimizer); development and protected stories, and any story
    with the same text as one, are never shown. Groups are error directions (true label, wrong answer); the sample
    takes one story from each group in turn, stubborn stories first."""
    from .context import _normalized_text
    task = wheel.initial.task
    hidden_ids = {r.item.id for r in development} | {i.id for i in protected}
    hidden_text = {_normalized_text(r.item, task) for r in development} | {_normalized_text(i, task) for i in protected}
    rows = {r.item.id: r for r in training
            if r.item.id not in hidden_ids and _normalized_text(r.item, task) not in hidden_text}
    records = ledger.error_records(incumbent_id=incumbent_id(wheel), exclude_items=frozenset(hidden_ids), recent=200)
    groups = {}
    for rec in records:
        if rec["item_id"] in rows:
            groups.setdefault((rec["label"], rec["choice"]), []).append(rec)
    picked, order = [], sorted(groups)
    while len(picked) < sample and any(groups.values()):
        for key in order:
            if groups[key] and len(picked) < sample:
                picked.append(groups[key].pop(0))
    stories = []
    for rec in picked:
        row = rows[rec["item_id"]]
        values = dict(row.item.values)
        body = values.get(task.input_field)
        if body is None:
            body = " ".join(str(v) for v in values.values())
        stories.append({"id": row.item.id, "title": _clip(values["title"], 160) if values.get("title") else None,
                        "excerpt": _clip(body, OPERATOR_CAPS["story_chars"]), "true_label": row.label,
                        "answered": rec["choice"], "error_direction": f"true {rec['label']}, answered {rec['choice']}",
                        "found_by": rec["kind"], "human_explanation": row.context.get("human_feedback")})
    return stories


def _rubric_of(idea):
    return str(idea.get("proposal", {}).get("rubric", "")) if idea.get("control") == "rubric" else ""


def combine_block(wheel, ledger, ideas, count):
    """(request, parents, None) for the best complementary pair, or (None, None, reason)."""
    from .shadow_evaluation import SHADOW
    inc = incumbent_id(wheel)
    by_id = {i["id"]: i for i in ideas if _rubric_of(i)}
    old = {i for (i,) in wheel.db.execute("SELECT incumbent_id FROM shadow_incumbents")} - {inc} \
        if wheel.db.execute("SELECT 1 FROM sqlite_master WHERE name='shadow_incumbents'").fetchone() else set()
    done = {frozenset(i["parents"]) for i in ideas if i.get("operator") == "combine" and len(i.get("parents", ())) == 2}
    pair = ledger.complementary_pair(incumbent_id=inc, ids=[*by_id, inc], context_version=SHADOW, skip_ids=old,
                                     exclude_pairs=done)
    if "skipped" in pair:
        return None, None, pair["skipped"]
    cap = OPERATOR_CAPS["parent_item_ids"]
    parents = []
    for name, other, mine in ((pair["a"], pair["b"], pair["fixes_a_not_b"]), (pair["b"], pair["a"], pair["fixes_b_not_a"])):
        rubric = wheel.active.config.rubric if name == inc else _rubric_of(by_id[name])
        entry = {"id": name, "is_incumbent": name == inc, "rubric": _clip(rubric, OPERATOR_CAPS["parent_rubric_chars"]),
                 "fixes_that_the_other_misses": len(mine), "story_ids_fixed_only_by_this": mine[:cap]}
        if name != inc:
            vs = ledger.paired_comparison(name, inc, context_version=SHADOW)
            entry["versus_incumbent"] = {"fixed": vs["gained"], "broke": vs["lost"], "shared": vs["shared"]}
        parents.append(entry)
    return ({"count": count, "parents": parents, "shared_stories": pair["shared"], "score": pair["score"]},
            [pair["a"], pair["b"]], None)


def plan_round(wheel, ledger, ideas, training, development, protected, mix):
    """-> (requests, parents_for_combine, skipped) where requests is {operator: spec} for the operators that can run."""
    config, requests, skipped, parents = wheel.idea_screening, {}, [], []
    for op in OPERATORS:
        count = mix.get(op, 0)
        if not count:
            continue
        if op == "mutate":
            requests[op] = {"count": count}
        elif op == "bold":
            requests[op] = {"count": count, "min_novelty": config.min_novelty}
        elif op == "target":
            stories = target_stories(wheel, ledger, training, development, protected, config.target_sample)
            if stories:
                requests[op] = {"count": count, "stories": stories}
            else:
                skipped.append((op, "no revealed training story is wrong for every tried idea or for the incumbent yet"))
        else:
            spec, parents, reason = combine_block(wheel, ledger, ideas, count)
            if spec:
                requests[op] = spec
            else:
                skipped.append((op, reason))
    return requests, parents, skipped


def _screen(wheel, labeled, requests, parents, control, references, gate):
    """Strip operator labels, compute novelty, apply the novelty gate to bold. -> (kept, meta, rejected_bold)."""
    kept, meta, rejected, seen = [], [], 0, set()
    for item in labeled:
        item = dict(item)
        operator = item.pop("operator")
        proposal = validate_proposals([item], control, 1)[0]
        key = _json(proposal[control])
        if key in seen:
            wheel._emit({"kind": "proposal-operator-skipped", "operator": operator,
                         "reason": "a duplicate of another proposal in the same reply"})
            continue
        novelty = nearest_novelty(str(proposal[control]), references)
        if operator == "bold" and novelty < gate:
            wheel._emit({"kind": "proposal-novelty-rejected", "operator": operator, "novelty": novelty,
                         "min_novelty": gate})
            rejected += 1
            continue
        seen.add(key)
        references = [*references, str(proposal[control])]
        kept.append(proposal)
        meta.append({"operator": operator, "parents": list(parents) if operator == "combine" else [],
                     "novelty": novelty})
    return kept, meta, rejected


def ask_round(wheel, build_briefing, context, control, ideas, training, development, protected):
    """Ask for this round's proposals. -> (proposals, meta) where meta[i] = {operator, parents, novelty}, or None
    when the plain multi-proposal request was used (dev_screen mode, non-rubric controls, optimizers without
    ``propose_operators``). ``build_briefing(extra)`` builds the briefing; ``context`` is the optimizer context."""
    from .idea_screening import ask_proposals
    config = wheel.idea_screening
    mix = config.effective_mix()
    if mix is None or control != "rubric":
        count = config.proposals_per_round
        return ask_proposals(wheel, build_briefing({"proposals_requested": count, **context}), control, count), None
    if getattr(wheel.optimizer, "propose_operators", None) is None:
        total = sum(mix.values())
        wheel._emit({"kind": "proposal-operator-skipped", "operator": "all",
                     "reason": "the optimizer has no propose_operators; used the plain multi-proposal request"})
        return ask_proposals(wheel, build_briefing({"proposals_requested": total, **context}), control, total), None
    ledger = HypothesisLedger(wheel.db)
    requests, parents, skipped = plan_round(wheel, ledger, ideas, training, development, protected, mix)
    for op, reason in skipped:
        wheel._emit({"kind": "proposal-operator-skipped", "operator": op, "reason": reason})
    references = [wheel.active.config.rubric, *(_rubric_of(i) for i in ideas if _rubric_of(i))]
    total = sum(spec["count"] for spec in requests.values())
    briefing = build_briefing({"proposals_requested": total, "operator_requests": requests, **context})
    labeled = wheel.optimizer.propose_operators(briefing)
    proposals, meta, rejected = _screen(wheel, labeled, requests, parents, control, references, config.min_novelty)
    for attempt in range(config.novelty_retries):
        if not rejected or "bold" not in requests:
            break
        wheel._emit({"kind": "proposal-operator-retry", "operator": "bold", "rejected": rejected,
                     "attempt": attempt + 1, "min_novelty": config.min_novelty})
        retry = {"bold": {**requests["bold"], "count": rejected, "retry_note":
                          "the previous bold proposals were too close to the incumbent or a prior idea"}}
        labeled = wheel.optimizer.propose_operators(
            build_briefing({"proposals_requested": rejected, "operator_requests": retry, **context}))
        more, more_meta, rejected = _screen(wheel, labeled, retry, parents, control,
                                            [*references, *(str(p[control]) for p in proposals)], config.min_novelty)
        proposals, meta = proposals + more, meta + more_meta
    if rejected:
        wheel._emit({"kind": "proposal-operator-skipped", "operator": "bold",
                     "reason": f"{rejected} bold proposals stayed below min_novelty {config.min_novelty} after "
                               f"{config.novelty_retries} retries"})
    return proposals, meta
