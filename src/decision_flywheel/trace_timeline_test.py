from .trace_timeline import timeline_data


def test_a_stale_head_is_inspectable_in_the_ml_optimization_lane():
    event={'kind':'head-invalidated','created_at':'2026-10-07T12:00:00Z',
        'reason':'decision feature context changed','previous_model_context':'old','model_context':'new'}
    item=timeline_data([event])['items'][0]
    assert item['group']=='classifier' and item['event_index']==0
    assert item['content']=='ML head invalidated'


def test_the_classifier_selection_outcome_is_clickable_in_the_ml_lane():
    data=timeline_data([{'kind':'classifier-training-completed','stage':'classifier',
        'promoted':True,'created_at':'2026-10-06T12:00:00Z'}])
    assert data['items'][0]['group']=='classifier'
    assert data['items'][0]['event_index']==0


def test_configuration_count_markers_show_active_questions_not_unpromoted_proposals():
    events = [
        {'kind':'cycle-started','classifier_snapshot':{'config':{'tasks':[]}}},
        {'kind':'optimizer-response','content':'proposal only'},
        {'kind':'classifier-activated','classifier_snapshot':{'config':{'tasks':[{'name':'a'},{'name':'b'}]}}},
        {'kind':'cycle-started','classifier_snapshot':{'config':{'tasks':[{'name':'a'},{'name':'b'}]}}},
    ]
    data=timeline_data([{**event,'created_at':'2026-10-06T12:00:00Z'} for event in events])
    counts=[item for item in data['items'] if item['group']=='configuration-count']
    assert [item['content'] for item in counts]==['1','3','']
    assert [item['classification_count'] for item in counts]==[1,3,3]
    assert [item['event_index'] for item in counts]==[0,2,3]


def test_optimizer_exchanges_are_individual_clickable_points_on_their_stage_lane():
    data = timeline_data([{'kind': kind, 'created_at': '2026-10-06T12:00:00Z',
        'step_stage': 'rubric'} for kind in ('optimizer-request', 'optimizer-response', 'proposal-validated')])
    assert len(data['items']) == 3
    assert all(item['type'] == 'point' and item['group'] == 'rubric' for item in data['items'])


def test_labels_keep_values_roles_and_retractions_and_rounds_use_distinct_lanes():
    events = [
        {'kind': 'human-feedback', 'created_at': '2026-10-06T12:00:00Z', 'assignment': 'development', 'action': 'submitted', 'feedback': {'final_answer_value': 'accept', 'edit_comment_value': 'reason'}},
        {'kind': 'step-started', 'created_at': '2026-10-06T12:01:00Z', 'step_id': 's', 'step_stage': 'questions'},
        {'kind': 'step-completed', 'created_at': '2026-10-06T12:02:00Z', 'step_id': 's', 'status': 'waiting'},
    ]
    data = timeline_data(events)
    assert data['items'][0]['content'] == 'accept · development · submitted · comment'
    span = data['items'][1]
    assert span['group'] == 'questions'
    assert 'end' not in span
    assert span['event_index'] == 1
    assert span['type'] == 'point'
    assert 'started' in span['content']
    assert 'optimizer' not in {g['id'] for g in data['groups']}


def test_missing_dates_are_not_invented_and_incomplete_rounds_are_points():
    data = timeline_data([{'kind': 'human-feedback'}, {'kind': 'step-started',
        'created_at': '2026-10-06T12:01:00Z', 'step_stage': 'rubric', 'step_id': 's'}])
    assert data['undated_count'] == 1
    assert data['items'][0]['type'] == 'point'
    assert 'end' not in data['items'][0]
