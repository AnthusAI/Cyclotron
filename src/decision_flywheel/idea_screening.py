"""Opt-in idea screening wired into the live optimization loop (hypothesis portfolio, Stage 2).

Several ideas are screened per round against the incumbent on development-eligible labeled items only.
Protected-evaluation items are never scored. A hash-defined reserve of development items is touched only by
the confirmation, and its evidence accumulates across rounds. Promotion still goes through the flywheel's
fitted-trial path, so the selection policy stays the final gate.
"""
import hashlib
import json
import random
from dataclasses import dataclass

from .flywheel import _hash, _json
from .hypothesis_ledger import HypothesisLedger, sign_test
from .idea_screen import holm, screen
from .optimizer_agent import parse_proposals

SCREENED_CONTROLS = ("rubric", "example_ids")
# Bounded optimizer payload: newest prior ideas, ledger digest rows, fixed/broke ids per row, stubborn ids.
CAPS = {"prior_ideas": 20, "ledger_ideas": 10, "item_ids_per_idea": 5, "stubborn_items": 20}


@dataclass(frozen=True)
class IdeaScreeningConfig:
    enabled: bool = False
    max_candidates: int = 8
    proposals_per_round: int = 3
    finalists: int = 2
    reserve_fraction: float = 0.2
    alpha: float = 0.05
    max_screen_calls: int = 2000
    stages: tuple = ((50, 0.5), (200, 0.5), (None, None))

    def __post_init__(self):
        for name in ("max_candidates", "proposals_per_round", "finalists", "max_screen_calls"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be boolean")
        if isinstance(self.reserve_fraction, bool) or not 0 < self.reserve_fraction < 1:
            raise ValueError("reserve_fraction must be in (0,1)")
        if isinstance(self.alpha, bool) or not 0 < self.alpha < 1:
            raise ValueError("alpha must be in (0,1)")
        stages = tuple((None if s is None else s, k) for s, k in (tuple(x) for x in self.stages))
        for size, keep in stages:
            if (size is not None and (type(size) is not int or size < 1)) or \
                    (keep is not None and (isinstance(keep, bool) or not 0 < keep <= 1)):
                raise ValueError("each stage is (items or None, kept fraction or None)")
        object.__setattr__(self, "stages", stages)

    def as_json(self):
        return {**{k: getattr(self, k) for k in ("enabled", "max_candidates", "proposals_per_round", "finalists",
                                                  "reserve_fraction", "alpha", "max_screen_calls")},
                "stages": [list(s) for s in self.stages]}

    @classmethod
    def from_json(cls, raw):
        return cls(**{**raw, "stages": tuple(tuple(s) for s in raw["stages"])})


def reserve_member(salt, item_id, fraction):
    """Stable, label-independent reserve role: a later-arriving item joins without ever having been screened."""
    return int(hashlib.sha256(f"{salt}:reserve:{item_id}".encode()).hexdigest(), 16) / 2 ** 256 < fraction


def validate_proposals(proposals, control, count):
    """Strict, like the single-proposal check: only rationale and the requested control, no duplicates."""
    if not isinstance(proposals, list) or not 1 <= len(proposals) <= count:
        raise ValueError(f"expected 1 to {count} proposals")
    seen = set()
    for proposal in proposals:
        if not isinstance(proposal, dict) or set(proposal) - {"rationale", control} or control not in proposal:
            raise ValueError("each proposal must contain exactly the requested control and a rationale")
        if "rationale" in proposal and not isinstance(proposal["rationale"], str):
            raise ValueError("rationale must be a string")
        key = _json(proposal[control])
        if key in seen:
            raise ValueError("duplicate proposals are not allowed")
        seen.add(key)
    return proposals


def ask_proposals(wheel, briefing, control, count):
    """One optimizer call for up to ``count`` distinct proposals; optimizers without propose_many give one."""
    many = getattr(wheel.optimizer, "propose_many", None)
    proposals = many(briefing) if many else [wheel.optimizer.propose(briefing)]
    return validate_proposals(proposals, control, count)


def incumbent_id(wheel):
    return "incumbent-" + wheel.active.config.fingerprint[:16]


def compact_ideas(ideas, ledger):
    rows = sorted(ideas, key=lambda i: (i.get("created_cycle", 0), i["id"]))[-CAPS["prior_ideas"]:]
    out = []
    for idea in rows:
        last = (ledger.verdicts(idea["id"]) or [None])[-1]
        out.append({"id": idea["id"], "proposal": idea["proposal"], "rationale": idea.get("rationale", ""),
                    "created_cycle": idea.get("created_cycle", 0), "attempts": len(idea.get("attempts", ())),
                    "last_verdict": last and last["verdict"]})
    return out


def optimizer_context(wheel, ledger, ideas, reserve_ids):
    """Prior ideas plus what each did to which items; reserve outcomes are hidden from the optimizer."""
    hidden = frozenset(reserve_ids)
    return {"prior_control_ideas": compact_ideas(ideas, ledger),
            "hypothesis_ledger": ledger.summary_for_optimizer(
                limit=CAPS["ledger_ideas"], incumbent_id=incumbent_id(wheel),
                max_item_ids=CAPS["item_ids_per_idea"], exclude_items=hidden),
            "stubborn_items": ledger.stubborn_items(exclude_items=hidden)[:CAPS["stubborn_items"]]}


class DevelopmentScorer:
    """scorer(idea, row): the idea's configuration applied through the flywheel's own decision-answer path.

    Only development rows are accepted. Each call goes through ``wheel._answers``, which honors the request
    byte ceiling, the per-open request ceiling, the answer cache and the durable answer record, so usage
    reporting stays truthful (cache hits cost no request). ``calls`` counts scorer calls for max_screen_calls.
    """

    def __init__(self, wheel, training, now, configs, development_ids):
        self.wheel, self.training, self.now = wheel, training, now
        self.configs, self.development_ids, self.calls = configs, frozenset(development_ids), 0

    async def __call__(self, idea, row):
        if row.item.id not in self.development_ids:
            raise ValueError("idea screening may only score development-eligible labeled items")
        self.calls += 1
        batch = await self.wheel._answers(self.configs[idea["id"]], row.item, self.training, self.now)
        answer = batch.answers["decision"]
        confidence = answer.probabilities[answer.label] if answer.probabilities else answer.confidence
        return {"choice": answer.label, "confidence": confidence, "label": row.label,
                "correct": answer.label == row.label}


def _wrong_objective_event(wheel):
    wheel._emit({"kind": "idea-screening-unsupported-objective",
                 "primary": wheel.selection_policy.primary if wheel.selection_policy else None,
                 "reason": "idea screening ranks ideas by accuracy; falling back to one trial per control"})


async def confirm(finalists, ideas, incumbent, reserve, scorer, ledger, *, alpha, max_calls, calls_used,
                  item_id=lambda row: row.item.id):
    """Paired exact sign test of each finalist against the incumbent on ALL reserve items either has been
    evaluated on, across rounds. Only missing (idea, item) pairs are scored. Holm across the few finalists."""
    result = {"cleared": [], "paired": {}, "truncated": False, "calls": 0, "reserve_size": len(reserve)}
    if not finalists or not reserve:
        return result
    ids = [item_id(r) for r in reserve]
    todo = {}
    for name, idea in [*((n, ideas[n]) for n in finalists), (incumbent["id"], incumbent)]:
        have = ledger.items_evaluated(name)
        todo[name] = (idea, [r for r in reserve if item_id(r) not in have])
    if calls_used + sum(len(rows) for _, rows in todo.values()) > max_calls:
        result["truncated"] = True
        return result
    for name, (idea, rows) in todo.items():
        scored = [(r, await scorer(idea, r)) for r in rows]
        result["calls"] += len(scored)
        ledger.record_evaluations(name, [{"item_id": item_id(r), "correct": bool(s["correct"]), "label": s["label"],
                                          "choice": s["choice"], "confidence": s["confidence"]} for r, s in scored],
                                  "reserve")
    base = ledger.correctness_vector(incumbent["id"], ids)
    stats = {}
    for name in finalists:
        mine = ledger.correctness_vector(name, ids)
        pairs = [(a, b) for a, b in zip(mine, base) if a is not None and b is not None]
        gained, lost = sum(a and not b for a, b in pairs), sum(b and not a for a, b in pairs)
        stats[name] = {"gained": gained, "lost": lost, "net": gained - lost, "shared": len(pairs),
                       "p_value": sign_test(gained, lost)}
    for name, adj in zip(finalists, holm([stats[n]["p_value"] for n in finalists])):
        stats[name]["p_adjusted"] = adj
        stats[name]["cleared"] = stats[name]["net"] > 0 and adj < alpha
    result["paired"] = stats
    result["cleared"] = sorted((n for n in finalists if stats[n]["cleared"]),
                               key=lambda n: (-stats[n]["net"], stats[n]["p_adjusted"], n))
    return result


def _pending(ideas, ledger):
    """Ideas last confirmed as positive but not yet significant: re-confirmed as the reserve grows."""
    out = []
    for idea in ideas:
        last = (ledger.verdicts(idea["id"]) or [None])[-1]
        evidence = last["evidence"] if last else {}
        if last and last["verdict"] == "advanced" and evidence.get("confirmation") and evidence.get("net", 0) > 0:
            out.append((evidence["net"], idea["id"]))
    return [i for _, i in sorted(out, key=lambda r: (-r[0], r[1]))]


async def screen_round(wheel, control, candidates, pool_ideas, training, development, *, protected, cycle, now,
                       config, proposal_of):
    """Screen ``candidates`` (idea dicts), confirm the finalists, return who cleared. Mutates only the ledger.

    ``pool_ideas`` are all of the control's ideas (carried positives are re-confirmed from them).
    ``proposal_of(idea)`` is the full proposal an idea applies. Returns a plain dict for events and results.
    """
    ledger = HypothesisLedger(wheel.db)
    protected_ids = {item.id for item in protected}
    if any(row.item.id in protected_ids for row in development):
        raise ValueError("development and protected records must be disjoint")
    salt = f"{wheel.initial.task.fingerprint}:idea-screening"
    reserve = [r for r in development if reserve_member(salt, r.item.id, config.reserve_fraction)]
    reserve = sorted(reserve, key=lambda r: r.item.id)
    reserve_ids = {r.item.id for r in reserve}
    pool = sorted((r for r in development if r.item.id not in reserve_ids), key=lambda r: r.item.id)
    inc = {"id": incumbent_id(wheel), "config": wheel.active.config}
    by_id = {i["id"]: i for i in pool_ideas}
    carried = []
    for name in _pending(pool_ideas, ledger):
        try:
            wheel.active.config.apply(proposal_of(by_id[name]), training)
            carried.append(name)
        except ValueError:
            pass
    fresh_ids = [i["id"] for i in candidates if i["id"] not in carried]
    configs = {inc["id"]: wheel.active.config}
    for name in {*fresh_ids, *carried}:
        configs[name] = wheel.active.config.apply(proposal_of(by_id[name]), training)
    scorer = DevelopmentScorer(wheel, training, now, configs, [r.item.id for r in development])
    remaining = wheel.max_requests - wheel.requests - (len(training) + 2 * len(development))
    max_calls = min(config.max_screen_calls, max(0, remaining))
    out = {"control": control, "candidates": fresh_ids[:], "carried": carried[:], "finalists": [], "cleared": [],
           "paired": {}, "truncated": False, "calls": 0, "reserve_size": len(reserve), "screen_items": len(pool),
           "stages": [], "max_calls": max_calls}
    from .observability import RequestBudgetExhausted
    try:
        finalists_fresh = []
        if fresh_ids and pool:
            outcome = await screen([by_id[n] for n in fresh_ids], inc, pool, scorer, stages=config.stages,
                                   reserve_fraction=0, alpha=config.alpha, ledger=ledger, seed=cycle,
                                   max_calls=max_calls, context_version=f"screen:{cycle}",
                                   item_id=lambda row: row.item.id)
            out["stages"], out["truncated"] = outcome.stages, outcome.truncated
            if outcome.truncated:
                out["calls"] = scorer.calls
                return out
            slots = min(len(outcome.finalists), max(1, config.finalists - len(carried)))
            finalists_fresh = outcome.finalists[:slots]
            for name in outcome.finalists[slots:]:
                ledger.record_verdict(name, "screened_out",
                                      f"survived screening but ranked below the {config.finalists} finalist slots", {})
        finalists = [*carried[:config.finalists - len(finalists_fresh)], *finalists_fresh]
        out["finalists"] = finalists
        verdict = await confirm(finalists, by_id, inc, reserve, scorer, ledger, alpha=config.alpha,
                                max_calls=max_calls, calls_used=scorer.calls)
    except RequestBudgetExhausted:
        out["truncated"], out["calls"] = True, scorer.calls
        return out
    out["calls"], out["truncated"] = scorer.calls, verdict["truncated"]
    if verdict["truncated"]:
        return out
    out["paired"], out["cleared"] = verdict["paired"], verdict["cleared"]
    for name in finalists:
        s = verdict["paired"].get(name)
        if s is None:
            continue
        text = (f"reserve confirmation over {s['shared']} reserve items: fixed {s['gained']} and broke "
                f"{s['lost']} versus the incumbent (Holm-adjusted p={s['p_adjusted']:.3f}); ")
        if s["net"] > 0:
            ledger.record_verdict(name, "advanced", text + ("cleared the gate" if s["cleared"] else
                                  "positive but not yet significant; kept and re-confirmed as the reserve grows"),
                                  {**s, "confirmation": True})
        else:
            ledger.record_verdict(name, "screened_out", text + "no paired advantage on the reserve so far",
                                  {**s, "confirmation": True})
    return out


def candidate_set(new_ideas, ledger, cycle, config, ideas, valid):
    """New ideas, then revived ones, then overdue ones (untested for three cycles), capped; ``valid`` filters."""
    chosen, seen = [], set()

    def add(idea):
        if idea["id"] not in seen and valid(idea):
            seen.add(idea["id"])
            chosen.append(idea)
    for idea in new_ideas:
        add(idea)
    by_id = {i["id"]: i for i in ideas}
    for row in ledger.revive_candidates():
        if row["idea_id"] in by_id:
            add(by_id[row["idea_id"]])
    for idea in sorted(ideas, key=lambda i: (i.get("last_cycle", 0), i["id"])):
        if cycle - max(idea.get("last_cycle", 0), idea.get("created_cycle", 0)) >= 3:
            add(idea)
    return chosen[:config.max_candidates]


async def screening_stage(wheel, proposals, training, development, *, protected, propensities, evaluation_time,
                          retry_interrupted):
    """Rubric stage with screening: save the proposals as ideas, screen them, confirm, and promote the best
    cleared finalist through the usual fitted-trial path (improve with promotion)."""
    from .control_scheduler import ControlScheduler
    control, config = "rubric", wheel.idea_screening
    scheduler, ledger = ControlScheduler(wheel), HypothesisLedger(wheel.db)
    cycle = wheel.db.execute("SELECT count(*) FROM optimization_stages").fetchone()[0]
    scheduler.admit_proposals(control, proposals, training, cycle)
    ideas = [i for i in scheduler.ideas() if i["control"] == control]

    def valid(idea):
        try:
            return (wheel.active.config.apply(idea["proposal"], training).briefing_state()[control]
                    != wheel.active.config.briefing_state()[control])
        except ValueError:
            return False
    new = [i for i in ideas if i.get("created_cycle") == cycle]
    new += [i for i in ideas if i not in new and not i["last_cycle"] and not ledger.verdicts(i["id"])]
    candidates = candidate_set(new, ledger, cycle, config, ideas, valid)
    report = await screen_round(wheel, control, candidates, ideas, training, development, protected=protected,
                                cycle=cycle, now=evaluation_time, config=config,
                                proposal_of=lambda idea: idea["proposal"])
    wheel._emit({"kind": "idea-screening-completed", **{k: v for k, v in report.items() if k != "stages"},
                 "stage_count": len(report["stages"])})
    by_id = {i["id"]: i for i in ideas}
    result = {"promoted": False, "idea_screening": report,
              "reason": ("screening stopped at its call budget; nothing promoted" if report["truncated"] else
                         "no finalist cleared the reserve confirmation")}
    for name in ([] if report["truncated"] else report["cleared"][:config.finalists]):
        if wheel.max_requests - wheel.requests < len(training) + 2 * len(development):
            result["reason"] = "session request budget; nothing promoted"
            break
        trial = await wheel.improve(training, development, protected=protected, propensities=propensities,
                                    candidate_proposal=by_id[name]["proposal"], retry_interrupted=retry_interrupted,
                                    require_recall_safeguards=True)
        for idea in (by_id[name],):
            idea["attempts"].append({"feedback_fingerprint": trial.get("trial_fingerprint"), "result": trial})
            idea["last_cycle"] = cycle
            scheduler.save(idea)
        if trial.get("promoted"):
            stats = report["paired"][name]
            ledger.record_verdict(name, "promoted",
                f"cleared the reserve gate and improved the fitted development trial; fixed {stats['gained']} and "
                f"broke {stats['lost']} reserve items (Holm-adjusted p={stats['p_adjusted']:.3f})", stats)
            result = {**trial, "idea_screening": report, "promoted_idea": name}
            break
        result = {**trial, "idea_screening": report, "promoted": False}
    for name in report["candidates"]:
        if by_id[name]["last_cycle"] != cycle:
            by_id[name]["last_cycle"] = cycle
            scheduler.save(by_id[name])
    return result
