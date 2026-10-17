"""Native versioned definitions retain transaction composition and API shape."""
from .web_store import WebStore


def test_definition_api_uses_one_injected_domain_without_consumer_sql(tmp_path):
    source=WebStore(tmp_path/'source.sqlite')
    source.save_classifier('c','C',{'question':'Which?', 'classes':[{'label':'yes'},{'label':'no'}]})
    class Storage:
        def __getattr__(self,name): return getattr(source.definition_storage,name)
    facade=WebStore(tmp_path/'facade.sqlite',definition_storage=Storage())
    facade.connect=lambda: (_ for _ in ()).throw(AssertionError('facade opened SQL'))
    first=facade.save_cyclotron_definition('card','First',[{'id':'c','revision':1}],{})
    second=facade.save_cyclotron_definition('card','Second',[{'id':'c','revision':1}],{})
    assert facade.cyclotron_definition_versions('card')==[first,second]
    assert facade.activate_cyclotron_definition('card',1)==first
    assert facade.cyclotron_definitions()==[first]
    assert facade.cyclotron_definition_comparison('card',1,2)['after']==second
