"""Measured model usage, cost at list prices, and wall-clock time, per window of cycles.

Counts come from a runtime's own events: every ``decision-response`` and
``optimizer-response`` carries the provider's reported usage and the cycle it
ran in. Cost is computed at list prices given by the caller, who states their
source; a cached input token is billed at the cached rate when one is given.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

CALLS = {"decision-response": "decision_model", "optimizer-response": "optimizer"}


TIER_KEYS = ("tier_requests", "tier_input_tokens", "tier_cached_input_tokens", "tier_output_tokens")


def _empty(tiered=False):
    counts = {"requests": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    if tiered:
        counts.update({key: 0 for key in TIER_KEYS})
    return counts


def _usage(usage) -> tuple[int, int, int]:
    usage = usage if isinstance(usage, Mapping) else {}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    output = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    return int(prompt), int(cached), int(output)


def cost_usd(counts: Mapping[str, int], price: Mapping[str, float]) -> float:
    """List-price cost of one model's counts; price is USD per million tokens."""
    cached_rate = price.get("cached_input_usd_per_mtok", price["input_usd_per_mtok"])
    # Requests whose input exceeded the price's ``above_input_tokens`` are billed whole at the above-tier rates
    # (counted separately in the ``tier_*`` fields); the cached rate is the same in both tiers.
    tier = {key: counts.get(key, 0) for key in TIER_KEYS}
    uncached = counts["input_tokens"] - counts["cached_input_tokens"] - (tier["tier_input_tokens"] - tier["tier_cached_input_tokens"])
    cached = counts["cached_input_tokens"] - tier["tier_cached_input_tokens"]
    output = counts["output_tokens"] - tier["tier_output_tokens"]
    total = uncached * price["input_usd_per_mtok"] + cached * cached_rate + output * price["output_usd_per_mtok"]
    if tier["tier_requests"]:
        total += ((tier["tier_input_tokens"] - tier["tier_cached_input_tokens"]) * price["above_input_usd_per_mtok"]
                  + tier["tier_cached_input_tokens"] * cached_rate + tier["tier_output_tokens"] * price["above_output_usd_per_mtok"])
    return total / 1e6


def _block(models, prices, start=None, end=None):
    block = {name: dict(values) for name, values in models.items()}
    for name, values in block.items():
        values["usd"] = round(cost_usd(values, prices[name]), 6)
    block["usd"] = round(sum(values["usd"] for values in block.values()), 6)
    if start and end:
        block["wall_clock_seconds"] = round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3)
    return block


def usage_by_window(events: Iterable[Mapping], prices: Mapping[str, Mapping[str, float]], *, window: int = 100) -> dict:
    """Per-window and total usage for ``decision_model`` and ``optimizer`` calls.

    ``prices`` maps each of those two names to its list price. Windows are
    numbered by cycle: window 1 holds cycles 1 to ``window``. Calls made
    outside any cycle are counted in the total only.
    """
    if type(window) is not int or window < 1:
        raise ValueError("window must be a positive integer")
    if set(prices) != set(CALLS.values()):
        raise ValueError("prices must give decision_model and optimizer list prices")
    tiered = {name: "above_input_tokens" in prices[name] for name in CALLS.values()}
    windows: dict[int, dict] = {}
    total = {name: _empty(tiered[name]) for name in CALLS.values()}
    first = last = None
    for event in events:
        kind, number = event.get("kind"), event.get("cycle_number")
        slot = None
        if isinstance(number, int) and number >= 1:
            slot = windows.setdefault((number - 1) // window, {"models": {name: _empty(tiered[name]) for name in CALLS.values()},
                                                                "start": None, "end": None, "cycles": 0})
        if kind == "cycle-started" and slot is not None:
            slot["start"] = slot["start"] or event.get("created_at")
            first = first or event.get("created_at")
        elif kind == "cycle-completed" and slot is not None:
            slot["end"] = event.get("created_at")
            slot["cycles"] += 1
            last = event.get("created_at")
        elif kind in CALLS:
            name = CALLS[kind]
            prompt, cached, output = _usage(event.get("usage"))
            for counts in ([total[name]] + ([slot["models"][name]] if slot is not None else [])):
                counts["requests"] += 1
                counts["input_tokens"] += prompt
                counts["cached_input_tokens"] += cached
                counts["output_tokens"] += output
                if tiered[name] and prompt > prices[name]["above_input_tokens"]:
                    counts["tier_requests"] += 1
                    counts["tier_input_tokens"] += prompt
                    counts["tier_cached_input_tokens"] += cached
                    counts["tier_output_tokens"] += output
    return {
        "window_size": window,
        "windows": [{"window": index + 1, "first_cycle": index * window + 1, "last_cycle": index * window + slot["cycles"],
                     **_block(slot["models"], prices, slot["start"], slot["end"])}
                    for index, slot in sorted(windows.items())],
        "total": _block(total, prices, first, last),
    }


def stored_usage_by_window(root_storage, output_ids: Sequence[str], prices: Mapping[str, Mapping[str, float]], *,
                           window: int = 100) -> dict:
    """Per-window and total usage recorded in an embedded cyclotron's root store, with the split by output.

    Window 1 holds decisions 1 to ``window`` in the order they were made; a
    decision-model request made while reviewing or learning counts toward the
    decision it served.
    """
    if type(window) is not int or window < 1:
        raise ValueError("window must be a positive integer")
    if set(prices) != {"decision_model", "optimizer"}:
        raise ValueError("prices must give decision_model and optimizer list prices")
    position = {decision_id: index for index, (decision_id,) in enumerate(root_storage.decision_ids())}
    times = [json.loads(payload)["createdAt"] for (payload,) in root_storage.decision_payloads()]
    windows: dict[int, dict] = {}
    total = {"decision_model": _empty(), "optimizer": _empty()}
    for decision_id, model, prompt, cached, output in root_storage.usage_counts():
        index = position.get(decision_id)
        targets = [total[model]]
        if index is not None:
            targets.append(windows.setdefault(index // window, {"decision_model": _empty(), "optimizer": _empty()})[model])
        for counts in targets:
            counts["requests"] += 1
            counts["input_tokens"] += prompt
            counts["cached_input_tokens"] += cached
            counts["output_tokens"] += output
    blocks = []
    for index in range((len(times) + window - 1) // window):
        first, last = index * window, min(len(times), (index + 1) * window) - 1
        models = windows.get(index, {"decision_model": _empty(), "optimizer": _empty()})
        blocks.append({"window": index + 1, "first_cycle": first + 1, "last_cycle": last + 1,
                       **_block(models, prices, times[first], times[last])})
    return {"window_size": window, "windows": blocks,
            "total": _block(total, prices, times[0], times[-1]) if times else _block(total, prices),
            "outputs": stored_usage_by_output(root_storage, output_ids, prices)}


def stored_usage_by_output(root_storage, output_ids: Sequence[str], prices: Mapping[str, Mapping[str, float]]) -> dict:
    """An estimated split of the total by output; the outputs sum to the total.

    Optimizer calls are measured per output (each output's learning makes
    its own calls). A shared decision-model request is split by each
    output's share of the request bytes; a request recorded before shares
    were kept is split evenly.
    """
    ids = list(output_ids)
    per = {cid: {"decision_model": _empty(), "optimizer": _empty()} for cid in ids}
    shares: dict[int, dict[str, float]] = {}
    for usage_id, classifier, share in root_storage.usage_shares():
        shares.setdefault(usage_id, {})[classifier] = share
    for usage_id, model, prompt, cached, output, source in root_storage.usage_by_output():
        if model == "optimizer":
            owner = (source or "").rsplit(":", 1)[0]
            split = {owner: 1.0} if owner in per else {cid: 1 / len(ids) for cid in ids}
        else:
            split = {cid: share for cid, share in shares.get(usage_id, {}).items() if cid in per}
            if not split:
                split = {cid: 1 / len(ids) for cid in ids}
            scale = sum(split.values())
            split = {cid: share / scale for cid, share in split.items()}
        for cid, share in split.items():
            counts = per[cid][model]
            counts["requests"] += share
            counts["input_tokens"] += prompt * share
            counts["cached_input_tokens"] += cached * share
            counts["output_tokens"] += output * share
    return {"estimate": ("Decision-model usage is split by each output's share of the shared request bytes "
                         "(evenly for requests recorded before shares were kept); optimizer usage is measured "
                         "per output. Outputs sum to the total."),
            "by_output": {cid: _block(per[cid], prices) for cid in ids}}
