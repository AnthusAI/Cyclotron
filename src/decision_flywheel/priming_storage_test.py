"""Priming composes named transactions and complete migrations without SQL fallback."""
import base64
import json
from contextlib import contextmanager
from copy import deepcopy

import pytest

from .catalog_snapshot import validate as catalog_validate
from .console_snapshot import validate as console_validate
from .priming import PrimingService
from .priming_test import ITEM, LABELS, refs
from .web_store import WebStore


def catalog(tmp_path):
    store=WebStore(tmp_path/'source.sqlite')
    store.save_item_list('examples','Accepted examples')
    for cid,labels in [('editor',['publish','not publish']),('scope',['yes','no'])]:
        store.save_classifier(cid,cid,{'question':cid+'?','classes':[{'label':v} for v in labels]})
    return store


def preview(service,identifier='draft'):
    content=base64.b64encode(json.dumps([{**ITEM,'labels':LABELS}]).encode()).decode()
    return service.start_file(identifier,'examples',refs(),'json',content)


def test_injected_priming_transaction_has_no_sql_fallback_and_rolls_back_partial_acceptance(tmp_path):
    native=catalog(tmp_path)
    classifiers=[native.classifier(row['id'],row['revision']) for row in refs()]
    class Memory:
        def __init__(self):self.drafts={};self.items={};self.feedback=[];self.fail=False
        @contextmanager
        def transaction(self):
            before=deepcopy((self.drafts,self.items,self.feedback))
            try:yield self
            except BaseException:
                self.drafts,self.items,self.feedback=before
                raise
        def draft(self,identifier):return self.drafts.get(identifier)
        def put_draft(self,identifier,payload):self.drafts[identifier]=payload
        def latest_classifier_revision(self,identifier):return 1
        def item_exists(self,list_id,identifier):return (list_id,identifier) in self.items
        def insert_item(self,row):self.items[row[0],row[1]]=row
        def insert_feedback(self,row):
            if self.fail:raise RuntimeError('synthetic atomic failure')
            self.feedback.append(row)
        def accepted_records(self,list_id,chosen):
            return [{'payload':row[5],'revision':row[2], 'votes':{f[1]:{'label':f[6],'comment':f[7]}
                for f in self.feedback if f[4]==row[1]}} for row in self.items.values()]
    memory=Memory()
    class Host:
        @property
        def priming_storage(self):raise AssertionError("explicit injected service storage was ignored")
        def connect(self):raise AssertionError('priming used native SQL fallback')
        def item_lists(self):return [{'id':'examples','name':'Examples'}]
        def classifier(self,identifier,revision):return next(c for c in classifiers if c['id']==identifier)
    service=PrimingService(Host(),storage=memory)
    draft=preview(service)
    assert draft['status']=='preview'
    memory.fail=True
    with pytest.raises(RuntimeError,match='atomic failure'):service.accept('draft',1,None)
    assert memory.items=={} and memory.feedback==[] and service.get('draft')['status']=='preview'
    memory.fail=False
    accepted=service.accept('draft',1,None)
    assert accepted==service.accept('draft',1,None) and len(memory.feedback)==2
    assert service.examples('examples',refs())['editor'][0].context['human_feedback']=='Synthetic novelty'
    service.close()


def test_catalog_and_console_v2_migrate_draft_and_atomic_accepted_evidence(tmp_path):
    source=catalog(tmp_path);service=PrimingService(source)
    draft=preview(service)
    snapshot=source.catalog_storage.export()
    assert snapshot['schema']=='cyclotron-catalog/v2'
    assert snapshot['tables']['priming_drafts'][0]['payload']
    target=WebStore(tmp_path/'destination.sqlite')
    target.catalog_storage.restore(snapshot)
    restored=PrimingService(target)
    assert restored.get('draft')==draft
    accepted=restored.accept('draft',1,None)
    assert restored.examples('examples',refs())['editor'][0].context['human_feedback']=='Synthetic novelty'
    full=target.export_storage()
    assert full['schema']=='cyclotron-console/v2'
    final=WebStore(tmp_path/'final.sqlite')
    final.restore_storage(full)
    final_service=PrimingService(final)
    assert final_service.get('draft')==accepted
    assert final_service.accept('draft',1,None)==accepted
    assert final.list_items('examples')[0]['provenance']['priming']['draft_id']=='draft'
    assert len(final.item_labels('examples',ITEM['id'],1))==2
    for s in (service,restored,final_service):s.close()


def test_known_legacy_catalog_and_console_snapshots_upgrade_without_inventing_drafts(tmp_path):
    source=catalog(tmp_path)
    for export,validate,schema in [(source.catalog_storage.export,catalog_validate,'cyclotron-catalog'),
                                   (source.export_storage,console_validate,'cyclotron-console')]:
        snapshot=export();snapshot['schema']=schema+'/v1';snapshot['tables'].pop('priming_drafts')
        upgraded=validate(snapshot)
        assert upgraded['schema']==schema+'/v2' and upgraded['tables']['priming_drafts']==[]
        snapshot['tables']['unknown']=[]
        with pytest.raises(ValueError):validate(snapshot)


@pytest.mark.parametrize('domain',['catalog','console'])
def test_explicit_legacy_manifest_reader_upgrades_known_v1_bytes(tmp_path,domain):
    from .batch_storage import StorageScope
    from .catalog_archive import CatalogArchive, LegacyCatalogArchive
    from .console_snapshot import ConsoleArchive, LegacyConsoleArchive
    from .storage_payloads import FilePayloads
    source=catalog(tmp_path)
    new=source.catalog_storage.export() if domain=='catalog' else source.export_storage()
    old=deepcopy(new);old['schema']=f'cyclotron-{domain}/v1';old['tables'].pop('priming_drafts')
    legacy_type=LegacyCatalogArchive if domain=='catalog' else LegacyConsoleArchive
    # Reproduce the old writer against synthetic immutable bytes, not a live service.
    class OldWriter(legacy_type):
        validator=staticmethod(lambda snapshot,**_:deepcopy(snapshot))
        publish=CatalogArchive.publish
    scope=StorageScope('synthetic','priming')
    payloads=FilePayloads(tmp_path/'payloads',scope,max_bytes=64000000)
    options={'max_records':10000,'max_bytes':64000000}
    published=OldWriter(payloads,scope,**options).publish(old)
    reader=legacy_type(payloads,scope,**options)
    assert reader.read(published.manifest)==new
    with pytest.raises(ValueError,match='read-only'):reader.publish(old)
    current_type=CatalogArchive if domain=='catalog' else ConsoleArchive
    with pytest.raises(ValueError):current_type(payloads,scope,**options).read(published.manifest)


def test_latest_retracted_priming_vote_does_not_revive_old_label_or_change_frozen_context(tmp_path):
    from .priming_context import catalog_priming
    source=catalog(tmp_path);service=PrimingService(source)
    preview(service);service.accept('draft',1,None)
    classifiers=[source.classifier(r['id'],r['revision']) for r in refs()]
    frozen=catalog_priming(source,'examples',classifiers,set())
    source.label_item('editor',1,'examples',ITEM['id'],1,'not publish','Correction rationale','correction')
    assert service.examples('examples',refs())['editor'][0].label=='not publish'
    source.retract_item_label('correction','retraction')
    current=catalog_priming(source,'examples',classifiers,set())
    assert current['editor']==[]
    assert frozen['editor'][0]['label']=='publish'
    assert frozen['editor'][0]['context']['human_feedback']=='Synthetic novelty'
    with pytest.raises(ValueError,match='disjoint'):catalog_priming(source,'examples',classifiers,{ITEM['id']})
    service.close()
