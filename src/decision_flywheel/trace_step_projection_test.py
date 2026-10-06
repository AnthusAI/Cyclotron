from .trace_step_projection import step_projection


def test_a_prediction_and_its_vote_share_an_item_step_without_changing_source_records():
    article = {'id': 'paper', 'title': 'A paper title'}
    history = [
        {'source_table': 'presentations', 'article': article, 'record': {'id': 7, 'shown_at': '2026-10-06T12:00:00Z'}},
        {'source_table': 'review_events', 'article': article, 'record': {'presentation_id': 7, 'created_at': '2026-10-06T12:01:00Z'}},
    ]
    result = step_projection([], history, [])
    assert len(result['steps']) == 1
    assert result['steps'][0]['title'] == 'A paper title'
    assert result['steps'][0]['keys'] == ['source:0', 'source:1']
    assert 0 < result['positions']['source:0'] < result['positions']['source:1'] < 1000
    assert history[0]['record']['shown_at'] == '2026-10-06T12:00:00Z'


def test_request_and_response_share_a_target_step_and_use_the_recorded_article_title():
    events = [
        {'kind': 'decision-request', 'target_id': 'paper', 'created_at': '2026-10-06T12:00:00Z'},
        {'kind': 'decision-response', 'target_id': 'paper', 'created_at': '2026-10-06T12:01:00Z'},
        {'kind': 'optimizer-request', 'step_id': 'round', 'step_stage': 'rubric', 'created_at': '2026-10-06T12:02:00Z'},
        {'kind': 'optimizer-response', 'step_id': 'round', 'step_stage': 'rubric', 'created_at': '2026-10-06T12:03:00Z'},
    ]
    items = [{'id': i, 'event_index': i, 'start': event['created_at']} for i, event in enumerate(events)]
    result = step_projection(events, [{'article': {'id': 'paper', 'title': 'Known title'}, 'record': {}}], items)
    assert [step['title'] for step in result['steps']] == ['Known title', 'Rubric optimization']
    assert result['steps'][0]['keys'] == ['0', '1']
    assert result['steps'][1]['keys'] == ['2', '3']


def test_unknown_items_are_identified_honestly_and_revisits_remain_chronological():
    events = [{'kind': 'decision-request', 'target_id': target, 'created_at': f'2026-10-06T12:0{i}:00Z'}
              for i, target in enumerate(['a', 'b', 'a'])]
    items = [{'id': i, 'event_index': i, 'start': event['created_at']} for i, event in enumerate(events)]
    result = step_projection(events, [], items)
    assert [step['item_id'] for step in result['steps']] == ['a', 'b', 'a']
    assert result['order'] == ['0', '1', '2']


def test_optimization_rounds_are_not_mislabeled_as_operational_cycles():
    events = [
        {'kind': 'step-started', 'step_id': 'cycle-one', 'step_stage': 'rubric'},
        {'kind': 'optimizer-request', 'step_id': 'cycle-one', 'step_stage': 'rubric'},
        {'kind': 'decision-request', 'step_id': 'child', 'parent_step_id': 'cycle-one', 'target_id': 'paper'},
        {'kind': 'decision-response', 'step_id': 'child', 'parent_step_id': 'cycle-one', 'target_id': 'paper'},
        {'kind': 'step-completed', 'step_id': 'cycle-one'},
        {'kind': 'step-started', 'step_id': 'cycle-two', 'step_stage': 'examples'},
    ]
    for index, event in enumerate(events):
        event['created_at'] = f'2026-10-06T12:0{index}:00Z'
    items = [{'id': i, 'event_index': i, 'start': event['created_at']} for i, event in enumerate(events)]
    history = [{'source_table': 'presentations', 'article': {'title': 'Earlier paper'},
                'record': {'id': 1, 'shown_at': '2026-10-05T12:00:00Z'}}]
    result = step_projection(events, history, items)
    assert [cycle['title'] for cycle in result['cycles']] == ['Imported review history', 'Retrospective optimization · Rubric', 'Retrospective optimization · Examples']
    assert result['cycles'][0]['recorded'] is False
    assert result['cycles'][1]['step_end'] > result['cycles'][1]['step_start']
    assert result['steps'][2]['keys'] == ['2', '3']
    assert result['cycles'][1]['end'] == result['cycles'][2]['start']


def test_operational_cycles_have_equal_outer_width_and_internal_training_requests_do_not_create_new_cycles():
    events=[{'kind':'cycle-started','cycle_id':'c1','cycle_number':1,'item':{'id':'a','values':{'text':'Title: Paper A'}}},
            {'kind':'prediction','cycle_id':'c1','cycle_number':1,'target_id':'a'},
            {'kind':'human-feedback','cycle_id':'c1','cycle_number':1,'feedback':{'item_id':'a'}},
            {'kind':'decision-request','cycle_id':'c1','cycle_number':1,'target_id':'other','step_id':'opt'},
            {'kind':'cycle-completed','cycle_id':'c1','cycle_number':1},
            {'kind':'cycle-started','cycle_id':'c2','cycle_number':2,'item':{'id':'b','values':{'text':'Title: Paper B'}}},
            {'kind':'prediction','cycle_id':'c2','cycle_number':2,'target_id':'b'}]
    for index,event in enumerate(events):event['created_at']=f'2026-10-06T12:0{index}:00Z'
    items=[{'id':i,'event_index':i,'start':event['created_at']} for i,event in enumerate(events)]
    result=step_projection(events,[],items)
    assert result['axis']=='cycle'
    assert len(result['cycles'])==2
    assert [cycle['end']-cycle['start'] for cycle in result['cycles']]==[1000,1000]
    assert result['cycles'][0]['title']=='Cycle 1 · Paper A'
    assert result['positions']['3']<1000
    assert result['positions']['6']>1000
    assert result['positions']['0']==result['positions']['3']==500
    assert result['positions']['5']==result['positions']['6']==1500
