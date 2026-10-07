"""The example importer is incremental, chronological and does not invent labels."""
import importlib.util
from pathlib import Path


def test_arxiv_items_keep_dates_content_and_source_provenance():
    spec=importlib.util.spec_from_file_location('updater',Path(__file__).parents[1]/'scripts/update_arxiv_items.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    item=module.article_item({'id':'2501.1','title':'Paper','abstract':'Content','categories':'cs.AI',
                             'authors':'An Author','versions':[{'created':'Wed, 01 Jan 2025 12:00:00 GMT'}]},
                            {'dataset':'fixture','revision':'abc'})
    assert item['id']=='arxiv:2501.1'
    assert item['occurred_at']=='2025-01-01T12:00:00+00:00'
    assert item['values']['authors']=='An Author'
    assert item['provenance']['revision']=='abc'
    assert 'label' not in item
