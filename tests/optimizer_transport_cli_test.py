"""All optional live entry points expose the same transport without paid calls."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize('name',['replay_arxiv_feedback','verify_arxiv_flywheel','experiment_arxiv_controls',
                                'measure_arxiv_questions','trace_arxiv_optimizer','reviewer'])
def test_live_entry_point_help_exposes_optimizer_transport_without_constructing_a_client(name, monkeypatch, capsys):
    from decision_flywheel.adapters.openai_optimizer import OpenAIOptimizer
    from decision_flywheel.adapters.litellm_optimizer import LiteLLMOptimizer
    def forbidden(**kwargs):
        raise AssertionError('help must not construct a live client')
    monkeypatch.setattr(OpenAIOptimizer,'from_environment',forbidden)
    monkeypatch.setattr(LiteLLMOptimizer,'from_environment',forbidden)
    if name=='reviewer':
        from decision_flywheel import reviewer
        module=reviewer
    else:
        scripts=Path(__file__).resolve().parents[1]/'scripts'
        monkeypatch.syspath_prepend(str(scripts))
        spec=importlib.util.spec_from_file_location(name,scripts/f'{name}.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    with pytest.raises(SystemExit) as result:
        module.main(['--help'])
    assert result.value.code==0
    output=capsys.readouterr().out
    assert '--optimizer-transport {openai,litellm}' in output
    assert '--optimizer-model' in output


def test_replay_preflight_freezes_the_transport_without_mutating_labels_or_constructing_providers(tmp_path, monkeypatch):
    from decision_flywheel.reviewer_store import Article, ReviewStore
    from decision_flywheel.adapters.jev import JevAdapter
    from decision_flywheel.adapters.litellm_optimizer import LiteLLMOptimizer
    from decision_flywheel.adapters.openai_optimizer import OpenAIOptimizer
    def forbidden(**kwargs):raise AssertionError('preflight must not construct a provider')
    for cls in (JevAdapter,LiteLLMOptimizer,OpenAIOptimizer):
        monkeypatch.setattr(cls,'from_environment',forbidden)
    source=tmp_path/'reviews.sqlite3'
    with ReviewStore(source,study_seed='offline',rolling_audit_rate=0,final_audit_rate=0) as store:
        for index in range(40):
            store.import_articles([Article(f'paper-{index}',f'Title {index}',f'Abstract {index}','2026-10-07',('cs.AI',))])
            store.record_vote(f'paper-{index}','include' if index%2 else 'exclude',comment=f'Explanation {index}')
        before=tuple(store.learning_feedback())
    scripts=Path(__file__).resolve().parents[1]/'scripts'
    spec=importlib.util.spec_from_file_location('replay_transport_preflight',scripts/'replay_arxiv_feedback.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    output=tmp_path/'preflight'
    assert module.main(['--database',str(source),'--output',str(output),
        '--optimizer-transport','litellm','--optimizer-model','ollama/fake'])==0
    execution=json.loads((output/'manifest.json').read_text())['execution']
    assert execution['optimizer_transport']=='litellm'
    assert execution['optimizer_model']=='ollama/fake'
    assert not (output/'runtime.sqlite3').exists()
    with ReviewStore(source,study_seed='offline',rolling_audit_rate=0,final_audit_rate=0) as store:
        assert tuple(store.learning_feedback())==before
    # Historical preflights had no transport field and always used OpenAI.
    historical=tmp_path/'historical'
    common=['--database',str(source),'--output',str(historical)]
    assert module.main(common)==0
    manifest=historical/'manifest.json'
    saved=json.loads(manifest.read_text());del saved['execution']['optimizer_transport']
    old=json.dumps(saved,indent=2)+'\n';manifest.write_text(old)
    assert module.main(common)==0
    assert manifest.read_text()==old
    with pytest.raises(SystemExit):
        module.main([*common,'--optimizer-transport','litellm'])
    assert manifest.read_text()==old
