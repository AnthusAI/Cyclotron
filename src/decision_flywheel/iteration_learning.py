"""Learn one iteration of a shared cyclotron away from its live store, using every technique, for the sites that are due.

Each due site gets an improved rubric and decision elements from the optimizer, its votes as examples, a fitted
classifier and calibration. The votes split by item identity: the optimizer and the first fit never see the evaluation
votes, which decide acceptance. An accepted site is then fitted on all its votes. Any site with a fitted classifier is
refitted, because a sibling's new elements change what it sees. Sibling votes judge only that sibling.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .candidate_fitting import fit_candidate
from .embedded_cyclotron import Cyclotron
from .flywheel import FittedClassifier, _restore, development_assignment
from .iteration_acceptance import accept_iteration
from .iteration_transfer import iteration_record
from .models import Item
from .priming_context import decode_priming
from .staged_optimization import optimize_stage
from .vote_examples import labeled_examples, split_votes


@dataclass(frozen=True)
class IterationPlan:
    identity: str
    definition: object
    rubrics: dict
    priming: dict  # Accepted examples per output, encoded: the store's priming plus earlier iterations' votes.
    votes: dict  # Each output's current votes.
    due: tuple  # The outputs whose votes are worth learning from now.
    parent: dict | None = None  # The record this store decides with, or None before the first iteration.


def _bind(engine, pools):
    engine.shared.bind_context({cid: wheel.active.config for cid, wheel in engine.wheels.items()}, pools)


async def _labels(wheel, version, rows, pool, now):
    """What ``version`` says about each voted row: its fitted classifier when it has one, else the decision answer."""
    decided = {}
    for row in rows:
        batch = await wheel._answers(version.config, row.item, pool, now)
        decided[row.item.id] = (
            version.head.predict(wheel._features(version.config, batch)) if version.head else batch.answers["decision"].label
        )
    return decided


def _examples(primed, rows):
    """The accepted examples plus these votes, where a vote replaces an earlier example of the same item."""
    voted = {row.item.id for row in rows}
    return (*(row for row in primed if row.item.id not in voted), *rows)


async def _fit(wheel, config, pool):
    candidate, _ = await fit_candidate(
        wheel, config, pool, (), protected=(), propensities={row.item.id: 1.0 for row in pool}, validation_status="provisional"
    )
    return candidate


async def _learn_site(engine, pools, output, votes, seed, now, report):
    """One due site's whole iteration. Returns its examples if accepted, or None with the version left as it was."""
    wheel = engine.wheels[output]
    incoming = wheel.active
    primed = pools[output]
    examples, evaluation = split_votes(votes, seed)
    rows = labeled_examples(examples)
    scored = labeled_examples(evaluation)
    dev_seed = seed + ":optimizer-development"
    development = tuple(row for row in rows if development_assignment(dev_seed, row.item.id))
    held = {row.item.id for row in development}
    training = _examples(primed, tuple(row for row in rows if row.item.id not in held))
    pools[output] = training
    _bind(engine, pools)
    before = await _labels(wheel, incoming, scored, primed, now)
    wheel.set_optimizer_context(
        [row.context["human_feedback"] for row in (*training, *development) if row.context.get("human_feedback")],
        evaluation_context_exposed=False,
    )
    scoring = tuple(Item(row.item.id, dict(row.item.values)) for row in scored)
    stage = dict(protected=scoring, propensities={row.item.id: 1.0 for row in training}, min_development_per_class=1)
    rubric = await optimize_stage(wheel, "rubric", training, development, **stage)
    _bind(engine, pools)
    elements = await optimize_stage(wheel, "questions", training, development, train_after_questions=False, **stage)
    wheel._activate(replace(incoming, head=None, answer_dependencies=()))
    proposal = {
        "rubric": (rubric.get("proposal") or {}).get("rubric", incoming.config.rubric),
        "tasks": (elements.get("proposal") or {}).get("tasks", [t.__dict__ for t in incoming.config.tasks]),
    }
    fit_pool = _examples(primed, rows)
    config = incoming.config.apply({**proposal, "example_ids": [row.item.id for row in fit_pool]}, fit_pool)
    pools[output] = fit_pool
    engine.shared.bind_context({**{c: w.active.config for c, w in engine.wheels.items()}, output: config}, pools)
    candidate = await _fit(wheel, config, fit_pool)
    after = await _labels(wheel, candidate, scored, fit_pool, now)
    acceptance = accept_iteration(evaluation, before, after, total_votes=len(votes))
    report[output] = {
        "acceptance": acceptance,
        "rubric": proposal["rubric"],
        "elements": proposal["tasks"],
        "rationale": {
            "rubric": (rubric.get("proposal") or {}).get("rationale"),
            "elements": (elements.get("proposal") or {}).get("rationale"),
        },
        "votes": len(votes),
        "examples": len(fit_pool),
    }
    if not acceptance["accepted"]:
        pools[output] = primed
        wheel._activate(replace(incoming, head=None, answer_dependencies=()))
        return None
    everything = _examples(primed, labeled_examples(votes))
    pools[output] = everything
    config = incoming.config.apply({**proposal, "example_ids": [row.item.id for row in everything]}, everything)
    engine.shared.bind_context({**{c: w.active.config for c, w in engine.wheels.items()}, output: config}, pools)
    final = await _fit(wheel, config, everything)
    report[output]["version"] = final.fingerprint
    report[output]["fittedClassifier"] = final.head is not None
    wheel._activate(final)
    return labeled_examples(votes)


async def learn_iteration(plan: IterationPlan, model, optimizer, directory) -> dict:
    """The adoptable record and what was learned. ``record`` is None when no site's new version was accepted."""
    primed = decode_priming(plan.priming)
    engine = Cyclotron.open(
        Path(directory) / "store", plan.definition, model, optimizer, seed_rubrics=plan.rubrics, priming_examples=primed
    )
    report, adopted, siblings = {}, {}, {}
    now = datetime.now(timezone.utc)
    try:
        pools = {cid: tuple(primed.get(cid, ())) for cid in engine.wheels}
        fitted = set()
        async with engine.execution():
            for cid, row in ((plan.parent or {}).get("state") or {}).items():
                restored = _restore(row["classifier"])
                engine.wheels[cid]._activate(FittedClassifier(restored.config, training_evidence=restored.training_evidence))
                if restored.head:
                    fitted.add(cid)
            base = {cid: wheel.active.fingerprint for cid, wheel in engine.wheels.items()}
            _bind(engine, pools)
            before = {}
            for output, votes in plan.votes.items():
                if output not in plan.due and votes:
                    before[output] = await _labels(engine.wheels[output], engine.wheels[output].active, labeled_examples(votes), pools[output], now)
            for output in plan.due:
                rows = await _learn_site(engine, pools, output, plan.votes[output], plan.definition.seed, now, report)
                if rows is not None:
                    adopted[output] = rows
            if not adopted:
                return {"record": None, "report": report}
            for cid in fitted - set(adopted):
                _bind(engine, pools)
                engine.wheels[cid]._activate(await _fit(engine.wheels[cid], engine.wheels[cid].active.config, pools[cid]))
            _bind(engine, pools)
            for output, labels in before.items():
                wheel = engine.wheels[output]
                votes = plan.votes[output]
                after = await _labels(wheel, wheel.active, labeled_examples(votes), pools[output], now)
                siblings[output] = accept_iteration(votes, labels, after, total_votes=0)
            harmed = sorted(output for output, row in siblings.items() if not row["accepted"])
            if harmed:
                return {"record": None, "report": report, "siblings": siblings, "reason": f"significantly worse for {harmed}"}
            acceptance = {output: report[output]["acceptance"] for output in adopted}
            record = iteration_record(
                engine,
                identity=plan.identity,
                parent=(plan.parent or {}).get("id"),
                base=base,
                examples=adopted,
                acceptance=acceptance,
            )
    finally:
        engine.close()
    return {"record": record, "report": report, "siblings": siblings}
