"""Serve held-out sets and before-and-after reports to the shared UI, in either deployment mode.

The same two GraphQL operations back the held-out card whether the cyclotron
runs inside an application (the application's own API mounts this service)
or in the central lab (``create_app(held_out=...)``):

    query  heldOutOverview(cyclotronId, classifier)
    mutation heldOutCommand(action: "freeze" | "evaluate", payload)

Freezing makes no model call. Evaluating does, so it needs live authority and
the caller must confirm the request bound the overview showed.

The host supplies ``open_cyclotron(cyclotron_id, max_requests=None)``, a context
manager that opens the authorized cyclotron for one call (``Cyclotron.open`` is
one) and raises for any cyclotron the caller may not reach. Evaluating reopens
with ``max_requests`` set to the confirmed request bound, so the request budget
always covers the evaluation. Each call opens and closes
it, so a scale-to-zero host holds the store's lease only while it works.
"""
from __future__ import annotations

from typing import Any, Callable, ContextManager, Mapping

COMMANDS = ("freeze", "evaluate")


class HeldOutService:
    def __init__(self, open_cyclotron: Callable[..., ContextManager[Any]], *, allow_live: bool = False):
        self.open_cyclotron = open_cyclotron
        self.allow_live = allow_live

    async def overview(self, cyclotron_id: str, classifier: str | None = None) -> dict[str, Any]:
        with self.open_cyclotron(cyclotron_id) as cyclotron:
            async with cyclotron.execution():
                sets = cyclotron.held_out_sets(classifier)
                reports = cyclotron.held_out_reports(classifier)
                request_bound = cyclotron.held_out_request_bound(classifier) if sets else None
        return {"cyclotronId": cyclotron_id, "classifier": classifier, "sets": sets,
                "latestReport": reports[-1] if reports else None,
                "reportHistory": [{"evaluatedAt": report["evaluatedAt"], "setId": report["heldOutSet"]["id"],
                                   "afterVersion": report["after"]["version"], "headline": report["headline"],
                                   "verdict": report["change"]["verdict"]} for report in reports],
                "requestBound": request_bound, "allowLive": self.allow_live}

    async def command(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if action not in COMMANDS or not isinstance(payload, Mapping):
            raise ValueError("unknown held-out command")
        if action == "evaluate" and not self.allow_live:
            raise ValueError("held-out evaluation calls the decision model and requires explicit live authority")
        classifier, set_id = payload.get("classifier"), payload.get("setId")
        cyclotron_id = str(payload.get("cyclotronId"))
        if action == "freeze":
            with self.open_cyclotron(cyclotron_id) as cyclotron:
                async with cyclotron.execution():
                    return cyclotron.freeze_held_out_set(classifier)
        with self.open_cyclotron(cyclotron_id) as cyclotron:
            async with cyclotron.execution():
                request_bound = cyclotron.held_out_request_bound(classifier, set_id=set_id)
        if payload.get("confirmedRequestBound") != request_bound:
            raise ValueError(f"confirm up to {request_bound} decision-model requests to evaluate")
        with self.open_cyclotron(cyclotron_id, max_requests=max(request_bound, 1)) as cyclotron:
            return await cyclotron.evaluate_held_out(classifier, set_id=set_id,
                                                     minimum_per_label=payload.get("minimumPerLabel"))
