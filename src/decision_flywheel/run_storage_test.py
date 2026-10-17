"""Native run/command storage behind the existing shared consumer API."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from .web_store import WebStore


def test_run_api_uses_injected_domain_storage_without_facade_sql(tmp_path):
    source=WebStore(tmp_path/'source.sqlite')
    class Storage:
        def __getattr__(self,name): return getattr(source.run_storage,name)
    facade=WebStore(tmp_path/'facade.sqlite',run_storage=Storage())
    facade.connect=lambda: (_ for _ in ()).throw(AssertionError('facade opened SQL'))
    run=facade.create_run('Synthetic','live',{},items=[{'id':'one','text':'same item'}])
    event=facade.append_event(run['id'],'first',{'kind':'prediction','explanation':'same explanation'})
    assert facade.events(run['id'])==[event]
    job=facade.command(run['id'],'request','prepare',{})
    assert facade.command(run['id'],'request','prepare',{})==job
    assert facade.claim_command()['id']==job['id']
    facade.finish_command(job['id'],'completed',{'finished':True})
    assert facade.run(run['id'])['status']=='completed'
    assert facade.items(run['id'])==[{'id':'one','text':'same item'}]


def test_two_native_workers_claim_once_and_restart_never_requeues_uncertain_work(tmp_path):
    path=tmp_path/'shared.sqlite'
    source=WebStore(path)
    run=source.create_run('Synthetic','live',{})
    pending=source.command(run['id'],'request','prepare',{})
    clients=[WebStore(path),WebStore(path)]
    barrier=Barrier(2)
    def claim(index): barrier.wait(); return clients[index].claim_command()
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(claim,(0,1)))
    assert sum(result is not None for result in results)==1
    restarted=WebStore(path)
    restarted.recover_interrupted()
    assert restarted.jobs(run['id'])[0]['status']=='interrupted'
    assert restarted.claim_command() is None
    assert pending['request_id']=='request'


def test_concurrent_event_and_summary_retries_return_one_durable_result(tmp_path):
    path=tmp_path/'shared.sqlite'
    source=WebStore(path)
    run=source.create_run('Synthetic','recorded',{})
    clients=[WebStore(path),WebStore(path)]
    barrier=Barrier(2)
    def append(index):
        barrier.wait()
        return clients[index].append_event(run['id'],'same-source',{'kind':'synthetic','explanation':'Same'})
    with ThreadPoolExecutor(max_workers=2) as pool: first,second=pool.map(append,(0,1))
    assert first==second and source.all_events(run['id'])==[first]
    barrier=Barrier(2)
    def complete(index): barrier.wait(); return clients[index].complete_run(run['id'],{'exact':'same'})
    with ThreadPoolExecutor(max_workers=2) as pool: first,second=pool.map(complete,(0,1))
    assert first==second
    assert [event['source_id'] for event in source.all_events(run['id'])]==['same-source','run:completed']
