"""The BFF persists original actor provenance before sending queue references."""
import asyncio
from uuid import uuid4

import pytest

from .hosted_command_worker_test import setup
from .prospectus_accounts import AccountAccessDenied, ProspectusUnavailable, VerifiedAccountSession


def test_same_origin_bff_enqueues_original_session_and_queue_retry_keeps_one_job():
    from .hosted_console_service import HostedConsoleService
    store,run,auth,sessions,cookie,actor,state,model,worker=setup();sent=[]
    current=sessions.get(cookie)
    verified=VerifiedAccountSession(current.access_token,current.person_id,actor.account_id,session_ref=current.session_ref)
    def send(message):
        sent.append(message)
        if len(sent)==1:raise TimeoutError('unknown queue receipt')
    service=HostedConsoleService(actor.account_id,store,auth,send,allow_live=True)
    with pytest.raises(ProspectusUnavailable):asyncio.run(service.submit_command(verified,run['id'],'once','prepare',{}))
    job=asyncio.run(service.submit_command(verified,run['id'],'once','prepare',{}))
    assert len(store.jobs(run['id']))==1 and len(sent)==2
    assert sent[0]==sent[1]=={'accountId':actor.account_id,'runId':run['id'],'jobId':job['id']}
    assert current.access_token not in str(sent) and actor.person_id not in str(sent)
    assert asyncio.run(worker.process(sent[1]))['status']=='completed' and model.calls==1


def test_bff_never_trusts_a_foreign_account_person_or_session_reference():
    from .hosted_console_service import HostedConsoleService
    store,run,auth,sessions,cookie,actor,state,model,worker=setup();sent=[]
    current=sessions.get(cookie);service=HostedConsoleService(actor.account_id,store,auth,sent.append,allow_live=True)
    for person,account,reference in [(str(uuid4()),actor.account_id,current.session_ref),
            (actor.person_id,str(uuid4()),current.session_ref),(actor.person_id,actor.account_id,'0'*64)]:
        verified=VerifiedAccountSession(current.access_token,person,account,session_ref=reference)
        with pytest.raises(AccountAccessDenied):asyncio.run(service.submit_command(verified,run['id'],'once','prepare',{}))
    assert not sent and store.jobs(run['id'])==[]


def test_live_worker_and_bff_are_disabled_without_separate_operator_authority():
    from .hosted_command_worker import HostedCommandWorker
    from .hosted_console_service import HostedConsoleService
    store,run,auth,sessions,cookie,actor,state,model,worker=setup()
    service=HostedConsoleService(actor.account_id,store,auth,lambda message:None)
    current=sessions.get(cookie)
    with pytest.raises(ValueError):asyncio.run(service.submit_command(VerifiedAccountSession(current.access_token,current.person_id,actor.account_id,session_ref=current.session_ref),run['id'],'once','prepare',{}))
    disabled=HostedCommandWorker(lambda account:store,auth,worker.runtime_for_command)
    with pytest.raises(ValueError):asyncio.run(disabled.process({'accountId':actor.account_id,'runId':run['id'],'jobId':str(uuid4())}))
    assert store.jobs(run['id'])==[] and model.calls==0
