"""Optional curated examples never enter the catalog until human acceptance."""
import pytest
from .web_store import WebStore
from .priming import Priming, Transformer


@pytest.fixture
def setup(tmp_path):
    store = WebStore(tmp_path/'web.sqlite')
    store.save_item_list('papers', 'Research')
    store.save_classifier('editor', 'Editorial', {'question':'Publish?', 'classes':[{'label':'publish'}, {'label':'not publish'}]})
    def transform(inputs, previous, feedback):
        if inputs['content'] == 'fail': raise RuntimeError('private provider details')
        return {'id':'paper', 'occurred_at':'2026-10-14', 'values':{'text':inputs['content'], 'revision_note':feedback}}
    service = Priming(store, [Transformer('paper', 'Paper', ({'name':'content','label':'Content','required':True},), transform)])
    return store, service


def preview(service, **kwargs):
    return service.preview('draft', 0, 'paper', {'content':'original'}, '', **kwargs)


def test_preview_revision_and_atomic_acceptance_with_separate_provenance(setup):
    store, service = setup
    first = preview(service)
    assert store.list_items('papers') == []
    revised = service.preview('draft', first['version'], 'paper', {'content':'corrected'}, 'Use the abstract')
    assert revised['candidate']['values']['text'] == 'corrected'
    with pytest.raises(ValueError): service.accept('draft', first['version'], 'papers','editor',1,'publish','Relevant')
    result = service.accept('draft', revised['version'], 'papers','editor',1,'publish','Novel threat evidence')
    assert result == service.accept('draft', revised['version'], 'papers','editor',1,'publish','Novel threat evidence')
    item = store.list_items('papers')[0]
    assert item['provenance']['priming']['history'][0]['inputs']['content'] == 'original'
    assert item['provenance']['priming']['history'][1]['feedback'] == 'Use the abstract'
    assert store.item_labels('papers','paper',1)[0]['comment'] == 'Novel threat evidence'
    with pytest.raises(ValueError): service.accept('draft', revised['version'], 'papers','editor',1,'not publish','Changed')


@pytest.mark.parametrize('label,reason', [('', 'why'), ('invented','why'), ('publish','  ')])
def test_invalid_classification_never_partially_imports(setup,label,reason):
    store, service = setup
    draft = preview(service)
    with pytest.raises(ValueError): service.accept('draft',draft['version'],'papers','editor',1,label,reason)
    assert store.list_items('papers') == []


def test_failure_and_cancellation_prevent_stale_acceptance_and_allow_revision_retry(setup):
    store, service = setup
    draft = preview(service)
    failed = service.preview('draft',draft['version'],'paper',{'content':'fail'},'Try extraction again')
    assert failed['status'] == 'failed' and 'private' not in str(failed)
    with pytest.raises(ValueError): service.accept('draft',failed['version'],'papers','editor',1,'publish','why')
    retried = service.preview('draft',failed['version'],'paper',{'content':'recovered'},'Retry')
    assert retried['status'] == 'preview'
    assert service.cancel('draft')['status'] == 'cancelled'
    assert service.cancel('draft')['status'] == 'cancelled'
    with pytest.raises(ValueError): service.accept('draft',retried['version'],'papers','editor',1,'publish','why')
    assert store.list_items('papers') == []


def test_cancellation_during_transform_discards_late_result(setup):
    store, service = setup
    original = service.transformers['paper']
    service.transformers['paper'] = Transformer('paper','Paper',original.fields,lambda *args:(service.cancel('draft'), {'id':'paper','occurred_at':'2026-10-14','values':{}})[1])
    assert preview(service)['status'] == 'cancelled'
    assert store.list_items('papers') == []


def test_acceptance_rolls_back_item_if_label_insert_fails(setup):
    store,service=setup
    draft=preview(service)
    with store.connect() as db:
        db.execute("CREATE TRIGGER reject_label BEFORE INSERT ON item_feedback BEGIN SELECT RAISE(ABORT,'failure'); END")
    with pytest.raises(Exception,match='failure'):service.accept('draft',draft['version'],'papers','editor',1,'publish','why')
    assert store.list_items('papers') == []
    assert service.get('draft')['status']=='preview'
