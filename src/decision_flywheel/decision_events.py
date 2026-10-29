"""Doorbell event for a finished machine decision job; it carries no decision content.

A consumer rings back with the signed ``GET /machine/decisions/{jobId}`` to fetch the receipt.
Delivery is at-least-once and best effort: publishing never fails or delays the job.
"""

import json

CONTRACT = "cyclotron-publication/v1"
DECISION_CONTRACT = "cyclotron-decision/v1"
SOURCE = "cyclotron.lab"
DETAIL_TYPE = "Decision Ready"
BUS_VARIABLE = "CYCLOTRON_DECISION_EVENT_BUS_ARN"
STATUSES = {"completed": "complete", "failed": "failed"}


def is_machine_decision(job, actor):
    from .decision_kinds import is_decision
    from .machine_access import MachineActor

    payload = job.get("payload")
    return (
        isinstance(actor, MachineActor)
        and is_decision(job.get("kind"))
        and isinstance(payload, dict)
        and set(payload) in ({"bindings", "item"}, {"bindings", "item", "contractVersion"})
    )


class DecisionEventNotifier:
    def __init__(self, client, bus_arn, *, log=print):
        self.client, self.bus_arn, self.log = client, bus_arn, log

    @classmethod
    def from_environment(cls, environment, client_factory, *, log=print):
        """None when no bus is configured; the client factory is then never called."""
        arn = (environment.get(BUS_VARIABLE) or "").strip()
        return cls(client_factory(), arn, log=log) if arn else None

    def detail(self, job, actor, status):
        request_id = job.get("request_id") or ""
        # The decision contract names the bindings it decided for; the publication contract calls them publications.
        contract = job["payload"].get("contractVersion", CONTRACT)
        return {
            "contractVersion": contract,
            "accountId": actor.account_id,
            "runId": job["run_id"],
            "jobId": job["id"],
            "requestId": request_id.removeprefix("machine:"),
            "itemId": job["payload"]["item"]["id"],
            "bindings" if contract == DECISION_CONTRACT else "publications": sorted(job["payload"]["bindings"]),
            "status": STATUSES[status],
        }

    def notify(self, job, actor, status):
        """Ring once for a machine decision job that reached `completed` or `failed`; never raises."""
        try:
            if not self.bus_arn or status not in STATUSES or not is_machine_decision(job, actor):
                return
            response = self.client.put_events(
                Entries=[
                    {
                        "EventBusName": self.bus_arn,
                        "Source": SOURCE,
                        "DetailType": DETAIL_TYPE,
                        "Detail": json.dumps(self.detail(job, actor, status), separators=(",", ":")),
                    }
                ]
            )
            failed = response.get("FailedEntryCount", 0) if isinstance(response, dict) else 0
            if failed:
                self.log(json.dumps({"event": "decision_event_failed_entries", "failedEntryCount": failed}))
        except Exception as error:
            self.log(json.dumps({"event": "decision_event_failed", "error": type(error).__name__}))
