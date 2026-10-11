"""Successive-halving screen of many candidate ideas against an incumbent, with a held-out reserve.

Engine-agnostic: the caller injects ``scorer(idea, item)``, an awaitable returning an object (or dict) with
``choice``, ``confidence`` and ``correct``. This module never calls a model.
"""
import asyncio
import math
import random
from dataclasses import dataclass, field

from .hypothesis_ledger import sign_test


def _ident(x):
    if isinstance(x, dict):
        return str(x["id"])
    return str(getattr(x, "id", x))


def _field(result, name, default=None):
    return result.get(name, default) if isinstance(result, dict) else getattr(result, name, default)


def holm(pvalues):
    """Holm step-down adjusted p-values (controls family-wise error; uniformly less conservative than Bonferroni)."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted, running = [1.0] * len(pvalues), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(pvalues) - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted


@dataclass
class ScreenResult:
    finalists: list = field(default_factory=list)   # idea ids, best screening accuracy first
    cleared: list = field(default_factory=list)     # finalists that passed the reserve gate
    stages: list = field(default_factory=list)      # per-stage tables
    paired: dict = field(default_factory=dict)      # idea id -> gained/lost/net/p_value/p_adjusted/cleared
    calls: int = 0
    truncated: bool = False                         # stopped early: max_calls would have been exceeded
    reserve_size: int = 0


async def screen(candidates, incumbent, items, scorer, *, stages=((50, 0.5), (200, 0.5), (None, None)),
                 reserve_fraction=0.2, alpha=0.05, ledger=None, seed=0, concurrency=8, max_calls=None,
                 context_version="screen", idea_id=_ident, item_id=_ident):
    """Each stage scores survivors (and the incumbent, once, shared) on the next block of screening items and
    keeps the top fraction by cumulative accuracy. The reserve is touched only by the final confirmation:
    a paired exact sign test per finalist against the incumbent, Holm-adjusted across finalists."""
    pool = list(items)
    random.Random(seed).shuffle(pool)
    reserve_n = min(len(pool), math.ceil(len(pool) * reserve_fraction - 1e-9)) if reserve_fraction > 0 else 0
    reserve, pool = pool[len(pool) - reserve_n:], pool[:len(pool) - reserve_n]
    result, gate = ScreenResult(reserve_size=len(reserve)), asyncio.Semaphore(concurrency)
    live = {idea_id(c): c for c in candidates}
    inc_id = idea_id(incumbent)
    hits = {name: [] for name in [*live, inc_id]}  # id -> correctness over every item scored so far

    async def run(idea, name, block):
        async def one(item):
            async with gate:
                return item, await scorer(idea, item)
        scored = await asyncio.gather(*(one(i) for i in block))
        result.calls += len(scored)
        hits[name].extend(bool(_field(r, "correct")) for _, r in scored)
        if ledger is not None:
            ledger.record_evaluations(name, [{"item_id": item_id(i), "correct": bool(_field(r, "correct")),
                                              "label": _field(r, "label"), "choice": _field(r, "choice"),
                                              "confidence": _field(r, "confidence")} for i, r in scored],
                                      context_version)

    def over_budget(extra):
        if max_calls is not None and result.calls + extra > max_calls:
            result.truncated = True
        return result.truncated

    cursor = 0
    for number, (size, keep) in enumerate(stages, 1):
        block = pool[cursor:] if size is None else pool[cursor:cursor + size]
        if not block or not live:
            continue
        if over_budget(len(block) * (len(live) + 1)):
            break
        cursor += len(block)
        await asyncio.gather(*(run(c, name, block) for name, c in live.items()), run(incumbent, inc_id, block))
        order = sorted(live, key=lambda n: (-sum(hits[n]) / len(hits[n]), n))
        kept = order if keep is None else order[:max(1, int(len(order) * keep + 0.5))]
        result.stages.append({"stage": number, "items": len(block), "scored": len(live) + 1,
                              "incumbent_accuracy": sum(hits[inc_id]) / len(hits[inc_id]),
                              "table": [{"idea_id": n, "n": len(hits[n]), "accuracy": sum(hits[n]) / len(hits[n]),
                                         "kept": n in kept} for n in order]})
        for n in order:
            if ledger is not None:
                acc = f"{sum(hits[n])}/{len(hits[n])}"
                ledger.record_verdict(n, "advanced" if n in kept else "screened_out",
                                      f"stage {number}: {acc} correct on screening items; "
                                      + ("kept in the top fraction" if n in kept else "dropped below the cut"),
                                      {"stage": number, "correct": sum(hits[n]), "n": len(hits[n])})
        live = {n: live[n] for n in kept}

    result.finalists = sorted(live, key=lambda n: (-(sum(hits[n]) / len(hits[n]) if hits[n] else 0), n))
    if result.truncated or not reserve or not live:
        return result
    if over_budget(len(reserve) * (len(live) + 1)):
        return result
    await asyncio.gather(*(run(c, name, reserve) for name, c in live.items()), run(incumbent, inc_id, reserve))
    k = len(reserve)
    stats = {}
    for name in result.finalists:
        mine, base = hits[name][-k:], hits[inc_id][-k:]
        gained = sum(a and not b for a, b in zip(mine, base))
        lost = sum(b and not a for a, b in zip(mine, base))
        stats[name] = {"gained": gained, "lost": lost, "net": gained - lost, "p_value": sign_test(gained, lost)}
    for name, adj in zip(result.finalists, holm([stats[n]["p_value"] for n in result.finalists])):
        stats[name]["p_adjusted"] = adj
        stats[name]["cleared"] = stats[name]["net"] > 0 and adj < alpha
    result.paired = stats
    result.cleared = [n for n in result.finalists if stats[n]["cleared"]]
    if ledger is not None:
        for n in result.finalists:
            s = stats[n]
            ledger.record_verdict(n, "promoted" if s["cleared"] else "advanced",
                                  f"reserve confirmation: fixed {s['gained']} and broke {s['lost']} items versus "
                                  f"the incumbent (Holm-adjusted p={s['p_adjusted']:.3f}); "
                                  + ("cleared the gate" if s["cleared"] else "did not clear the gate"), s)
    return result
