from .trace_rounds import round_details


def test_selecting_a_round_includes_later_prompt_reply_tools_and_outcome_but_not_another_round():
    events = [
        {'kind': 'step-started', 'step_id': 'a'},
        {'kind': 'optimizer-request', 'step_id': 'a', 'messages': ['prompt-a']},
        {'kind': 'step-started', 'step_id': 'b'},
        {'kind': 'optimizer-response', 'step_id': 'b', 'content': 'reply-b'},
        {'kind': 'optimizer-response', 'step_id': 'a', 'content': 'reply-a', 'tool_calls': ['tool-a']},
        {'kind': 'candidate-evaluated', 'step_id': 'a', 'candidate': {'accuracy': .8}},
        {'kind': 'step-completed', 'step_id': 'a', 'result': {'promoted': True}},
    ]
    data = round_details(events)
    assert data['owners']['0'] == 0
    assert data['rounds']['0']['optimizer_requests'] == [1]
    assert data['rounds']['0']['optimizer_responses'] == [4]
    assert data['rounds']['0']['evaluations'] == [5]
    assert data['rounds']['0']['outcomes'] == [6]
    assert data['rounds']['2']['optimizer_responses'] == [3]


def test_legacy_rounds_are_bounded_and_missing_exchanges_are_not_borrowed():
    data = round_details([{'kind': 'round-started'}, {'kind': 'optimizer-response'},
                          {'kind': 'round-started'}, {'kind': 'candidate-rejected'}])
    assert data['rounds']['2']['optimizer_responses'] == []
    assert data['rounds']['0']['optimizer_responses'] == [1]


def test_nested_numerical_round_belongs_to_its_enclosing_step():
    data = round_details([{'kind': 'step-started', 'step_id': 'a'},
        {'kind': 'round-started', 'step_id': 'a'}, {'kind': 'fit-completed', 'step_id': 'a'}])
    assert data['owners']['1'] == 0
    assert data['rounds']['0']['fits'] == [2]
