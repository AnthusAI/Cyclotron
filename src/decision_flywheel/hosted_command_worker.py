"""Ephemeral queue delivery into existing, explicitly S3-composed console runtime.

Messages identify canonical work; they contain no human authority. A worker
never steals a running claim or repeats a paid request after uncertain delivery.
"""

import asyncio
import time
from uuid import uuid4

from .command_refusal import CommandRefused
from .journal import uuid
from .prospectus_accounts import AccountAccessDenied
from .storage_errors import CommandNotReady


class CommandReconciliationRequired(RuntimeError):
    pass


class AuthorizedModel:
    def __init__(self, model, guard):
        self.model, self.guard = model, guard

    def __getattr__(self, name):
        return getattr(self.model, name)

    async def classify_many(self, *args, **kwargs):
        await self.guard()
        return await self.model.classify_many(*args, **kwargs)


class CommandEventSink:
    def __init__(self, view, run_id, check, *, batch_size=100):
        self.view, self.run_id, self.check = view, run_id, check
        self.pending = []
        self.set_batch_size(batch_size)

    def set_batch_size(self, value):
        if type(value) is not int or value < 1 or self.pending:
            raise ValueError("a positive batch size and an empty event buffer required")
        self.batch_size = value

    def ingest(self, source_id, event):
        self.pending.append((source_id, event))
        if len(self.pending) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self.pending:
            return
        self.check()
        self.view.append_batch(self.run_id, list(self.pending))
        self.pending.clear()


class _Effects:
    """Fresh human permission immediately before console business-state writes."""

    WRITES = {
        "set_status",
        "complete_run",
        "append_batch",
        "append_event",
        "update_item",
        "record_item_prediction",
        "checkpoint_cyclotron",
        "label_item",
        "retract_item_label",
        "finish_command",
        "register_standing_item",
    }

    def __init__(self, view, check):
        self._view, self._check = view, check

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        target = getattr(self._view, name)
        if name not in self.WRITES:
            return target

        def effect(*args, **kwargs):
            self._check()
            return target(*args, **kwargs)

        return effect


class HostedCommandWorker:
    def __init__(
        self,
        store_for_account,
        authorization,
        runtime_for_command,
        *,
        allow_live=False,
        publication_runtime_for_command=None,
        command_policy=None,
        data_command=None,
        decision_events=None,
        clock=time.monotonic,
    ):
        self.decision_events = decision_events
        self.clock = clock
        self.last_job_seconds = 0.0
        self.allow_live = allow_live
        self.publication_runtime_for_command = publication_runtime_for_command
        self.command_policy = command_policy
        self.data_command = data_command
        self.store_for_account, self.authorization, self.runtime_for_command = (
            store_for_account,
            authorization,
            runtime_for_command,
        )

    def _ring(self, job, actor, status):
        """Doorbell after the terminal state is durable; best effort, never raises."""
        if self.decision_events is not None:
            self.decision_events.notify(job, actor, status)

    def _has_time(self, remaining):
        """Start another job only with room for twice the last one; an invocation never runs out mid job."""
        return remaining is None or remaining() > 2 * self.last_job_seconds

    async def deliver(self, message, remaining=None):
        """Run the delivered job, then the jobs queued behind it in its run, while this invocation has time.

        The worker never writes to its own queue: AWS stops a worker -> SQS -> worker chain as a recursive
        loop and drops the deliveries. A job's one wake-up comes from the BFF that accepted it; the jobs behind
        a finished one run here, and the scheduled sweep starts any head left waiting when time runs out.
        """
        started = self.clock()
        result = await self.process(message)
        self.last_job_seconds = self.clock() - started
        while result["status"] in ("completed", "failed") and self._has_time(remaining):
            store = await asyncio.to_thread(self.store_for_account, message["accountId"])
            head = await asyncio.to_thread(store.queue_head, message["runId"])
            if head is None:
                break
            message = {"accountId": message["accountId"], "runId": message["runId"], "jobId": head["id"]}
            started = self.clock()
            result = await self.process(message)
            self.last_job_seconds = self.clock() - started
        return result

    async def sweep(self, account_id, remaining=None):
        """The scheduled safety net: start every run's waiting head in the account, then drain each run.

        A run whose head fails here is reported by class only and does not stop the other runs.
        """
        store = await asyncio.to_thread(self.store_for_account, account_id)
        outcomes = []
        for head in await asyncio.to_thread(store.ready_heads):
            if not self._has_time(remaining):
                break
            message = {"accountId": account_id, "runId": head["run_id"], "jobId": head["id"]}
            try:
                outcomes.append((await self.deliver(message, remaining))["status"])
            except Exception as error:
                print(f"queue_sweep_failed {type(error).__name__}")  # class only: no job content or credentials
                outcomes.append("error")
        return outcomes

    async def process(self, message):
        if not self.allow_live and self.data_command is None:
            raise ValueError("live worker requires separate operator authority")
        keys = {"accountId", "runId", "jobId"}
        if not isinstance(message, dict) or set(message) not in (keys, keys | {"attempt"}):
            raise ValueError("delivery contains references only")
        for key in keys:
            uuid(message[key])
        # Retired re-sends carried an attempt count; such a message still delivers its job.
        attempt = message.get("attempt", 0)
        if type(attempt) is not int or not 0 <= attempt <= 100000:
            raise ValueError("delivery attempt must be a bounded count")
        store = await asyncio.to_thread(self.store_for_account, message["accountId"])
        job, actor = await asyncio.to_thread(store.command_envelope, message["jobId"])
        if actor.account_id != message["accountId"] or actor.run_id != message["runId"] or job["run_id"] != actor.run_id:
            raise AccountAccessDenied("delivery target does not match canonical actor")
        if not self.allow_live and not self.data_command(job["kind"], job["payload"]):
            raise ValueError("live worker requires separate operator authority")
        # One job per run at a time, in acceptance order. A job that is not at the head is neither claimed nor
        # failed, and its wake-up is settled: the job ahead runs it when it finishes, or the sweep starts it.
        # This is a read of queue order only, so it needs no authority.
        if job["status"] == "pending" and not (await asyncio.to_thread(store.queue_state, job["id"]))["ready"]:
            return {"status": "queued", "jobId": job["id"]}
        guard = self.authorization.guard(actor)
        await guard()
        if self.command_policy is not None:
            self.command_policy(actor, job["kind"])
        if job["status"] in ("completed", "failed", "interrupted"):
            return {"status": job["status"], "jobId": job["id"]}
        if job["status"] != "pending":
            raise CommandReconciliationRequired("running command requires scoped reconciliation")
        owner = str(uuid4())
        try:
            claim = await asyncio.to_thread(store.claim_command, owner=owner, job_id=job["id"])
        except CommandNotReady:
            return {"status": "queued", "jobId": job["id"]}
        if claim is None:
            raise CommandReconciliationRequired("command already claimed; never infer a receipt")
        view = await asyncio.to_thread(store.for_command, job["id"], owner, claim["claim_fence"])
        current = await asyncio.to_thread(store.command_actor, job["id"], owner, claim["claim_fence"])
        if current != actor:
            raise CommandReconciliationRequired("canonical queued actor changed")

        def check():
            self.authorization.check(actor)
            if self.command_policy is not None:
                self.command_policy(actor, job["kind"])
            view.run(actor.run_id)  # Fresh execution fence as well as human permission.

        async def owned_guard():
            await asyncio.to_thread(check)

        await owned_guard()  # After confirmed execution ownership, before opening operation state.
        effects = _Effects(view, check)
        if job["kind"] == "publication":
            if self.publication_runtime_for_command is None:
                raise ValueError("publication worker composition is unavailable")
            runtime = self.publication_runtime_for_command(effects, owned_guard, check, actor)
        else:
            runtime = self.runtime_for_command(effects, owned_guard, check)
        run = await asyncio.to_thread(effects.run, actor.run_id)
        items = await asyncio.to_thread(effects.items, actor.run_id)
        shown = await asyncio.to_thread(effects.current_item, actor.run_id)
        session = None
        try:
            session = await asyncio.to_thread(runtime.open_session, actor.run_id, run["config"], items, shown)
            try:
                result = await runtime.execute(
                    session, job["kind"], job["payload"], shown, run["config"], request_id=job["request_id"]
                )
            except CommandRefused as refusal:
                # Refused before any provider dispatch: record a terminal, non-secret receipt.
                await owned_guard()
                await asyncio.to_thread(
                    effects.finish_command,
                    job["id"],
                    "failed",
                    {"refusal": {"code": refusal.code, "message": refusal.public_message}},
                )
                self._ring(job, actor, "failed")
                return {"status": "failed", "jobId": job["id"], "code": refusal.code}
            await owned_guard()
        finally:
            if session is not None:
                await asyncio.to_thread(session.close)
        # API updates are owned by WorkspaceSession, exactly as in the native runner.
        if result.updates:
            raise ValueError("console runtime returned unsupported external item updates")
        await asyncio.to_thread(effects.finish_command, job["id"], "completed", dict(result.result))
        self._ring(job, actor, "completed")
        return {"status": "completed", "jobId": job["id"]}
