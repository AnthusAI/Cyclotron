"""Per-call worker authorization from the original encrypted session.

Persist QueuedActor atomically with its canonical job, never accept one from a
browser/queue delivery as authority. Delivery identifies a canonical job only.
Invoke the returned guard after execution ownership and again before every side
effect. It grants console operations only, never publication/provider grants.
"""
import asyncio
from dataclasses import dataclass, field, asdict
import hashlib
import hmac
import re
from .prospectus_accounts import AccountAccessDenied
from .s3_journal import encode, uuid


@dataclass(frozen=True)
class QueuedActor:
    session_ref: str = field(repr=False)
    person_id: str
    subject: str
    account_id: str
    run_id: str
    binding: str
    operation: str
    proof: str = field(default='', repr=False)


def _proof(session, actor):
    fields = asdict(actor); fields.pop('proof')
    return hmac.new(session.access_token.encode(), encode(fields), hashlib.sha256).hexdigest()


class QueuedAccountAuthorization:
    def __init__(self, sessions, verifier, identities, accounts, binding_for_run):
        self.sessions, self.verifier, self.identities = sessions, verifier, identities
        self.accounts, self.binding_for_run = accounts, binding_for_run

    async def capture(self, session, account_id, run_id, binding, *, operation='console:write'):
        """Called by trusted BFF after request CSRF and account/run resolution."""
        actor = QueuedActor(session.session_ref, session.person_id, session.subject,
                            account_id, run_id, binding, operation)
        actor = QueuedActor(**{**asdict(actor), 'proof': _proof(session, actor)})
        await self.guard(actor)()
        return actor

    def guard(self, actor):
        """A new closure per operation; no mutable engine/global callback."""
        if not isinstance(actor, QueuedActor): raise AccountAccessDenied('queued actor required')
        try:
            uuid(actor.account_id); uuid(actor.person_id); uuid(actor.run_id)
            if (not isinstance(actor.session_ref, str) or not re.fullmatch(r'[a-f0-9]{64}', actor.session_ref)
                    or actor.operation not in ('console:read','console:write')
                    or not isinstance(actor.subject, str) or not 1 <= len(actor.subject) <= 256
                    or not isinstance(actor.binding, str) or not 1 <= len(actor.binding) <= 256
                    or not isinstance(actor.proof, str) or not re.fullmatch(r'[a-f0-9]{64}', actor.proof)):
                raise ValueError()
        except Exception: raise AccountAccessDenied('invalid queued actor') from None

        async def authorize():
            session = await asyncio.to_thread(self.sessions.get_ref, actor.session_ref)
            if (session is None or session.person_id != actor.person_id or session.subject != actor.subject
                    or not hmac.compare_digest(_proof(session, actor), actor.proof)):
                raise AccountAccessDenied('queued session changed or expired')
            claims = await asyncio.to_thread(self.verifier.access, session.access_token)
            if claims.get('sub') != actor.subject:
                raise AccountAccessDenied('queued identity changed')
            person = await asyncio.to_thread(self.identities.me, session.access_token)
            if not isinstance(person, dict) or person.get('id') != actor.person_id:
                raise AccountAccessDenied('queued identity enrollment changed')
            await asyncio.to_thread(self.accounts.authorize, session.access_token,
                                    actor.account_id, actor.person_id, actor.operation)
            # Resolves current account+run binding, not session's selected-account preference.
            binding = await asyncio.to_thread(self.binding_for_run, actor.account_id, actor.run_id)
            if binding != actor.binding:
                raise AccountAccessDenied('queued workspace binding changed')
        return authorize
