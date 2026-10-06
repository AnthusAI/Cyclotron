from .trace_timeline import timeline_data


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
