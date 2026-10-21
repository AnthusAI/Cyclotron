"""Service-principal (SigV4) access to standing publication targets; no human session exists here.

API Gateway verifies the signature (AWS_IAM). This module only maps the verified caller identity,
read from the gateway event and never from request headers or bodies, onto an exact configured
role ARN. A machine actor is bound to one account and one run, can only decide, read status and
record feedback for that target, and is re-checked against trusted configuration before every
worker side effect.
"""

import asyncio
import json
import re
from dataclasses import dataclass

from .journal import uuid
from .prospectus_accounts import AccountAccessDenied

ROLE_ARN = re.compile(r"arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_-]{1,64})")
ASSUMED_ARN = re.compile(r"arn:aws:sts::([0-9]{12}):assumed-role/([A-Za-z0-9+=,.@_-]{1,64})/([A-Za-z0-9+=,.@_-]{2,64})")
SESSION_NAME = re.compile(r"[A-Za-z0-9+=,.@_-]{2,64}")


class MachineDenied(AccountAccessDenied):
    pass


def principals_from_environment(environment):
    """JSON list of exact IAM role ARNs. No wildcards, paths, users or account patterns."""
    raw = environment.get("CYCLOTRON_MACHINE_PRINCIPALS")
    if raw is None or raw == "":
        return ()
    try:
        values = json.loads(raw)
        if (
            not isinstance(values, list)
            or len(values) > 20
            or len(set(values)) != len(values)
            or any(not isinstance(v, str) or not ROLE_ARN.fullmatch(v) for v in values)
        ):
            raise ValueError()
        return tuple(values)
    except (ValueError, TypeError):
        raise ValueError("CYCLOTRON_MACHINE_PRINCIPALS must be a JSON list of exact IAM role ARNs") from None


def verified_caller(scope, allowed):
    """(role ARN, session name) from the gateway-verified IAM identity, if exactly allow-listed."""
    event = scope.get("aws.event")
    try:
        arn = event["requestContext"]["authorizer"]["iam"]["userArn"]
    except (KeyError, TypeError):
        raise MachineDenied("signed machine caller required") from None
    match = ASSUMED_ARN.fullmatch(arn) if isinstance(arn, str) else None
    if match is None:
        raise MachineDenied("signed machine role caller required")
    role = f"arn:aws:iam::{match[1]}:role/{match[2]}"
    if role not in allowed:
        raise MachineDenied("machine principal is not configured")
    return role, match[3]


@dataclass(frozen=True)
class MachineActor:
    """Queued authority for a service principal: exactly one account/run, console:write only."""

    principal: str
    session: str
    account_id: str
    run_id: str
    binding: str
    operation: str = "console:write"
    kind: str = "machine"

    def __post_init__(self):
        uuid(self.account_id)
        uuid(self.run_id)
        if (
            not isinstance(self.principal, str)
            or not ROLE_ARN.fullmatch(self.principal)
            or not isinstance(self.session, str)
            or not SESSION_NAME.fullmatch(self.session)
            or not isinstance(self.binding, str)
            or not 1 <= len(self.binding) <= 256
            or self.operation != "console:write"
            or self.kind != "machine"
        ):
            raise ValueError("invalid machine actor")

    @property
    def service_id(self):
        return self.principal


class MachineAwareAuthorization:
    """Human sessions go to the original authorizer; machine actors to trusted configuration."""

    def __init__(self, human, machine_check):
        self._human, self._machine_check = human, machine_check

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._human, name)

    def check(self, actor):
        if isinstance(actor, MachineActor):
            return self._machine_check(actor)
        return self._human.check(actor)

    def guard(self, actor):
        if not isinstance(actor, MachineActor):
            return self._human.guard(actor)

        async def authorize():
            await asyncio.to_thread(self._machine_check, actor)

        return authorize
