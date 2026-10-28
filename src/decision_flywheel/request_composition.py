"""Fit a decision-model request into one configured request size, never refusing it.

``DECISION_REQUEST_BYTES`` is the single request size every decision request is
composed to fit, single-classifier and batched alike. Composition is deterministic
and recorded with the request:

1. Examples go in compact form: id, title and summary (when present), label and
   the editor's explanation. Never the full body.
2. The target goes in whole.
3. If the request is still over the size, examples are dropped by the recorded
   rule: the newest example of each label is kept longest, then the rest newest
   first; whatever is dropped goes from the end of that order. With several
   classifiers the orders are interleaved, one example from each classifier in
   turn.
4. If the request with no examples is still over, the end of the target's body
   is trimmed until it fits.

The returned ``composition`` names the size, the rule, the examples shown and
available, and the target's original and sent body lengths. It is recorded with
every decision. When the composer dropped an example or trimmed the target, the
composition is also part of the request, and so of its fingerprint; a request the
composer left whole is reproduced exactly by its own content.
"""

import json

DECISION_REQUEST_BYTES = 65536
EXAMPLE_RULE = "compact-examples; newest-per-label-kept-longest-then-newest-first; interleaved-by-classifier"


def request_bytes(request):
    """Bytes of what is sent: the state and questions, as compact UTF-8 JSON."""
    sent = {key: value for key, value in request.items() if key != "composition"}
    return len(json.dumps(sent, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode())


def altered(composition):
    """Whether the composer dropped an example or trimmed the target (the request then carries its composition)."""
    return composition["target_trimmed"] or composition["examples_shown"] < composition["examples_available"]


def compact_example(row, label):
    example = {"id": row.item.id}
    for field in ("title", "summary"):
        value = row.item.values.get(field)
        if value not in (None, ""):
            example[field] = value
    example["label"] = label
    explanation = row.context.get("human_feedback")
    if isinstance(explanation, str) and explanation.strip():
        example["explanation"] = explanation
    return example


def keep_order(candidates, labels):
    """Example ids in keep-longest order. ``candidates`` are (id, label, recency) with larger recency newer."""
    newest = sorted(candidates, key=lambda row: (-row[2], row[0]))
    first = []
    for label in labels:
        match = next((row for row in newest if row[1] == label), None)
        if match is not None:
            first.append(match[0])
    return first + [row[0] for row in newest if row[0] not in first]


def interleave(orders):
    """One id from each classifier in turn, as (classifier, id) pairs."""
    out, depth = [], max((len(order) for order in orders.values()), default=0)
    for index in range(depth):
        for identifier, order in orders.items():
            if index < len(order):
                out.append((identifier, order[index]))
    return out


def _largest(low, high, fits):
    """Largest n in [low, high] with fits(n), given fits is monotone non-increasing; low - 1 when none."""
    best = low - 1
    while low <= high:
        middle = (low + high) // 2
        if fits(middle):
            best, low = middle, middle + 1
        else:
            high = middle - 1
    return best


def fit(build, priority, target_values, body_field, available, *, budget=DECISION_REQUEST_BYTES):
    """``build(kept, target_values)`` composes a request from a set of kept priority entries."""
    priority = list(priority)
    sent_values = dict(target_values)
    body = target_values.get(body_field)

    def fits(count, values=sent_values):
        return request_bytes(build(set(priority[:count]), values)) <= budget

    shown = len(priority)
    if not fits(shown):
        shown = max(0, _largest(0, len(priority), fits))
    trimmed = False
    if not fits(shown) and isinstance(body, str):
        length = _largest(0, len(body), lambda n: fits(0, {**target_values, body_field: body[:n]}))
        sent_values = {**target_values, body_field: body[: max(0, length)]}
        trimmed, shown = True, 0
    request = build(set(priority[:shown]), sent_values)
    composition = {
        "budget_bytes": budget,
        "request_bytes": request_bytes(request),
        "example_rule": EXAMPLE_RULE,
        "examples_available": available,
        "examples_shown": shown,
        "target_field": body_field,
        "target_trimmed": trimmed,
        "target_original_chars": len(body) if isinstance(body, str) else None,
        "target_sent_chars": len(sent_values[body_field]) if isinstance(body, str) else None,
    }
    composition["fits"] = composition["request_bytes"] <= budget
    return request, composition, set(priority[:shown])
