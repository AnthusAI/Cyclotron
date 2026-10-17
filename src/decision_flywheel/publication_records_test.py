"""Frozen source/binding provenance composes the existing revisioned catalog."""
import pytest

from .models import Item
from .publication_lab import PublicationBinding
from .publication_records import PublicationRecords
from .web_store import WebStore

PREDICTION={'decisionId':'decision-1','itemId':'source-1','classifiers':{'intel':{'version':1,'fingerprint':'offline-fixture'}}}


def setup_records(tmp_path):
    store=WebStore(tmp_path/'catalog.sqlite3')
    for key in ('sources','bindings','publication-records'):store.save_item_list(key,key)
    store.upsert_list_items('sources',[{'id':'source-1','occurred_at':'2026-01-01T00:00:00Z','values':{'text':'original abstract','source_uri':'https://example.invalid/paper'}}])
    store.upsert_list_items('bindings',[{'id':'publication-a','occurred_at':'2026-01-01T00:00:00Z','values':{'accountId':'account-a','publicationId':'publication-a','revision':'binding-1','outputs':['intel']}}])
    records=PublicationRecords(store,account_id='account-a',source_lists={'sources'},binding_list='bindings',record_list='publication-records')
    source=records.reference('sources','source-1',1)
    binding=records.reference('bindings','publication-a',1)
    return store,records,source,binding


def test_frozen_context_keeps_exact_source_and_binding_revisions_after_updates_and_restart(tmp_path):
    store,records,source,binding=setup_records(tmp_path)
    current=PublicationBinding('publication-a',('intel',),'binding-1')
    item=Item('source-1',{'text':'original abstract','source_uri':'https://example.invalid/paper'})
    prepared=records.prepare(current,item,{'sourceRef':source.to_json(),'bindingRef':binding.to_json()})
    frozen=records.freeze('decision-1','2026-01-02T00:00:00Z',prepared,prediction=PREDICTION)
    assert records.freeze('decision-1','2026-01-02T00:00:00Z',prepared,prediction=PREDICTION)==frozen
    store.upsert_list_items('sources',[{'id':'source-1','occurred_at':'2026-01-03T00:00:00Z','values':{'text':'new abstract'}}])
    store.upsert_list_items('bindings',[{'id':'publication-a','occurred_at':'2026-01-03T00:00:00Z','values':{'accountId':'account-a','publicationId':'publication-a','revision':'binding-2','outputs':['intel']}}])
    restarted=PublicationRecords(WebStore(tmp_path/'catalog.sqlite3'),account_id='account-a',source_lists={'sources'},binding_list='bindings',record_list='publication-records')
    context=restarted.context(frozen['contextId'],'publication-a','decision-1')
    assert context['source']['values']['text']=='original abstract'
    assert context['binding']['values']['revision']=='binding-1'
    assert len(restarted.contexts('publication-a','decision-1'))==1


def test_source_and_binding_hash_scope_and_snapshot_values_are_validated_before_freezing(tmp_path):
    store,records,source,binding=setup_records(tmp_path)
    current=PublicationBinding('publication-a',('intel',),'binding-1')
    item=Item('source-1',{'text':'original abstract','source_uri':'https://example.invalid/paper'})
    payload={'sourceRef':source.to_json(),'bindingRef':binding.to_json()}
    with pytest.raises(ValueError):records.prepare(current,item,{**payload,'sourceRef':{**source.to_json(),'contentHash':'0'*64}})
    with pytest.raises(ValueError):records.prepare(current,item,{**payload,'bindingRef':{**binding.to_json(),'listId':'sources'}})
    with pytest.raises(ValueError):records.prepare(current,Item('source-1',{'text':'changed display'}),payload)
    with pytest.raises(ValueError):records.prepare(PublicationBinding('publication-b',('intel',),'binding-1'),item,payload)
    assert store.snapshot_items('publication-records')==[]


def test_review_receipts_and_scoped_replay_keep_original_explanations_and_context(tmp_path):
    _,records,source,binding=setup_records(tmp_path)
    prepared=records.prepare(PublicationBinding('publication-a',('intel',),'binding-1'),Item('source-1',{'text':'original abstract','source_uri':'https://example.invalid/paper'}),{'sourceRef':source.to_json(),'bindingRef':binding.to_json()})
    context=records.freeze('decision-1','2026-01-02T00:00:00Z',prepared,prediction=PREDICTION)
    review={'reviewId':'review-1','decisionId':'decision-1','itemId':'source-1','classifier':'intel','label':'reject','explanation':'Human scope reason','reasonCode':'out_of_scope','reviewer':'editor-a','selectedBy':'reviewer','kind':'label','createdAt':'2026-01-02T00:00:01Z'}
    records.review(context,review,source_cursor=1);records.review(context,review,source_cursor=1)
    exported=records.replay('publication-a',{'intel'})
    assert len(exported)==1
    assert exported[0]['source']['values']['text']=='original abstract'
    assert exported[0]['review']['explanation']=='Human scope reason'
    assert records.replay('publication-b',{'intel'})==[]
    assert records.replay('publication-a',{'other'})==[]
    assert records.receipt('publication-a','review-1')['contextId']==context['contextId']


def test_modified_catalog_record_is_refused_instead_of_becoming_new_canonical_history(tmp_path):
    store,records,source,binding=setup_records(tmp_path)
    prepared=records.prepare(PublicationBinding('publication-a',('intel',),'binding-1'),Item('source-1',{'text':'original abstract','source_uri':'https://example.invalid/paper'}),{'sourceRef':source.to_json(),'bindingRef':binding.to_json()})
    context=records.freeze('decision-1','2026-01-02T00:00:00Z',prepared,prediction=PREDICTION)
    row=store.snapshot_items('publication-records')[0]
    store.upsert_list_items('publication-records',[{'id':row['id'],'occurred_at':row['occurred_at'],'values':{**row['values'],'kind':'altered'}}])
    with pytest.raises(ValueError):records.context(context['contextId'],'publication-a','decision-1')


def test_private_binding_fields_and_reused_mapping_revisions_are_refused(tmp_path):
    store,records,source,binding=setup_records(tmp_path)
    current=PublicationBinding('publication-a',('intel',),'binding-1')
    target=Item('source-1',{'text':'original abstract','source_uri':'https://example.invalid/paper'})
    prepared=records.prepare(current,target,{'sourceRef':source.to_json(),'bindingRef':binding.to_json()})
    records.freeze('decision-1','2026-01-02T00:00:00Z',prepared,prediction=PREDICTION)
    with pytest.raises(ValueError,match='revision identity'):
        records.check_binding(PublicationBinding('publication-a',('new-output',),'binding-1'))
    store.upsert_list_items('bindings',[{'id':'publication-a','occurred_at':'2026-01-03T00:00:00Z','values':{'accountId':'account-a','publicationId':'publication-a','revision':'binding-2','outputs':['intel'],'privateTransport':{'fixture':'must not be exposed'}}}])
    with pytest.raises(ValueError,match='private transport'):
        records.binding_revision(PublicationBinding('publication-a',('intel',),'binding-2'),records.reference('bindings','publication-a',2).to_json())


@pytest.mark.parametrize('budget',['records','bytes'])
def test_capacity_failure_does_not_acknowledge_an_unreadable_record(tmp_path,budget):
    from .publication_records import canonical
    store,records,source,binding=setup_records(tmp_path)
    prepared=records.prepare(PublicationBinding('publication-a',('intel',),'binding-1'),Item('source-1',{'text':'original abstract','source_uri':'https://example.invalid/paper'}),{'sourceRef':source.to_json(),'bindingRef':binding.to_json()})
    first=records.freeze('decision-1','2026-01-02T00:00:00Z',prepared,prediction=PREDICTION)
    if budget=='records':records.max_records=1
    else:records.max_bytes=len(canonical(store.snapshot_items('publication-records')).encode())
    assert records.freeze('decision-1','2026-01-02T00:00:00Z',prepared,prediction=PREDICTION)==first
    with pytest.raises(ValueError,match='budget'):
        records.put('fixture',{'must':'not be acknowledged'},'2026-01-02T00:00:01Z')
    assert len(store.snapshot_items('publication-records'))==1
    assert records.context(first['contextId'],'publication-a','decision-1')==first
