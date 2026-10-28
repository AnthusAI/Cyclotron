"""Account-scoped BFF service; persist original actor before queue publication."""

import asyncio
import hmac

from .journal import encode, uuid
from .prospectus_accounts import AccountAccessDenied, ProspectusUnavailable, VerifiedAccountSession


class HostedConsoleService:
    articles = ()

    def __init__(
        self, account_id, store, authorization, send, *, allow_live=False, redact=(), command_policy=None, data_command=None
    ):
        uuid(account_id)
        self.account_id, self.store, self.authorization, self.send = account_id, store, authorization, send
        self.allow_live, self.redact = allow_live, tuple(redact)
        self.command_policy = command_policy
        self.data_command = data_command

    async def submit_command(self, verified, run_id, request_id, kind, payload):
        if not self.allow_live and not (self.data_command and self.data_command(kind, payload)):
            raise ValueError("live commands require separate operator authority")
        uuid(run_id)
        if not isinstance(verified, VerifiedAccountSession) or verified.account_id != self.account_id or not verified.session_ref:
            raise AccountAccessDenied("verified original request session required")
        session = await asyncio.to_thread(self.authorization.sessions.get_ref, verified.session_ref)
        if (
            session is None
            or session.person_id != verified.person_id
            or not hmac.compare_digest(session.access_token, verified.access_token)
        ):
            raise AccountAccessDenied("request session changed")
        content = encode(payload)
        if (
            any(secret and secret.encode() in content for secret in (*self.redact, session.access_token))
            or not isinstance(request_id, str)
            or not request_id
        ):
            raise ValueError("credential-free command with a request identity required")
        binding = await asyncio.to_thread(self.authorization.binding_for_run, self.account_id, run_id)
        if self.command_policy is not None:
            self.command_policy(run_id, kind)
        actor = await self.authorization.capture(session, self.account_id, run_id, binding)
        await self.authorization.guard(actor)()
        job = await asyncio.to_thread(self.store.enqueue_authorized_command, run_id, request_id, kind, payload, actor)
        try:
            await asyncio.to_thread(self.send, {"accountId": self.account_id, "runId": run_id, "jobId": job["id"]})
        except Exception:
            raise ProspectusUnavailable("queue outcome unavailable; retry the same request identity") from None
        return job

    async def submit_machine_command(self, actor, request_id, kind, payload):
        """Queue a publication command for a configured service principal; no human session is involved."""
        from .machine_access import MachineActor

        if not isinstance(actor, MachineActor) or actor.account_id != self.account_id:
            raise AccountAccessDenied("verified machine actor required")
        if not self.allow_live and not (self.data_command and self.data_command(kind, payload)):
            raise ValueError("live commands require separate operator authority")
        encode(payload)  # refuses a payload that is not plain JSON
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("command with a request identity required")
        binding = await asyncio.to_thread(self.authorization.binding_for_run, self.account_id, actor.run_id)
        if binding != actor.binding:
            raise AccountAccessDenied("machine workspace binding changed")
        if self.command_policy is not None:
            self.command_policy(actor.run_id, kind)
        await self.authorization.guard(actor)()
        job = await asyncio.to_thread(self.store.enqueue_authorized_command, actor.run_id, request_id, kind, payload, actor)
        try:
            await asyncio.to_thread(self.send, {"accountId": self.account_id, "runId": actor.run_id, "jobId": job["id"]})
        except Exception:
            raise ProspectusUnavailable("queue outcome unavailable; retry the same request identity") from None
        return job

    def _runtime(self):
        from .cyclotron_runtime import CyclotronRuntime

        def unavailable(*args, **kwargs):
            raise ValueError("BFF cannot execute a provider or open a worker store")

        return CyclotronRuntime(
            self.store, None, model_factory=unavailable, sink_factory=unavailable, storage_factory=unavailable
        )

    def create_run(self, name, config):
        if not self.allow_live:
            raise ValueError("live run requires separate operator authority")
        return self._runtime().create_run(name, config)

    def create_replay(self, name, source_run_id, config):
        if not self.allow_live:
            raise ValueError("live replay requires separate operator authority")
        return self._runtime().create_replay(name, source_run_id, config)
