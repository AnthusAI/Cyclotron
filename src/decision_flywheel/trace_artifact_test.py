import sqlite3
import json

from .trace_artifact import read_trace, render_trace, recover_configurations, exchanges_at


def test_the_shadcn_shell_is_bundled_offline_with_no_external_script_or_font_requests():
    html=render_trace([])
    assert 'data-ui="shadcn"' in html
    assert 'font-src data:' in html
    assert '__VIEWER_JS__' not in html
    assert '<script src=' not in html
    assert 'SIL OPEN FONT LICENSE' in html
    assert '<html lang="en" class="dark">' not in html
    assert 'prefers-color-scheme:dark' in html


def test_latest_transcripts_are_visible_without_selecting_their_raw_events():
    events = [{'event_id': 1, 'kind': 'optimizer-request', 'messages': [{'content': 'exact prompt'}]},
              {'event_id': 2, 'kind': 'optimizer-response', 'content': 'exact reply'},
              {'event_id': 3, 'kind': 'decision-request', 'state': {'examples': [{'text': 'actual example'}]}, 'questions': {}},
              {'event_id': 4, 'kind': 'step-completed'}]
    assert exchanges_at(events, 3)['optimizer_request'] == events[0]
    assert exchanges_at(events, 3)['optimizer_response'] == events[1]
    assert exchanges_at(events, 3)['decision_request']['state']['examples'][0]['text'] == 'actual example'
    assert exchanges_at(events, 0)['optimizer_response'] is None


def test_a_new_request_does_not_display_an_old_response_as_its_answer():
    events = [{'kind': 'optimizer-request'}, {'kind': 'optimizer-response'}, {'kind': 'optimizer-request'}]
    assert exchanges_at(events, 2)['optimizer_response'] is None


def test_round_configuration_is_recovered_from_its_actual_optimizer_input_not_a_later_round():
    events = [{'event_id': 1, 'kind': 'step-started'},
        {'event_id': 2, 'kind': 'optimizer-request', 'messages': [{'content': json.dumps({'task': {'name': 'include'}, 'current': {'rubric': 'knowledge', 'example_ids': ['a'], 'tasks': [], 'version': 'v1'}})}]},
        {'event_id': 3, 'kind': 'step-completed'},
        {'event_id': 4, 'kind': 'step-started'},
        {'event_id': 5, 'kind': 'optimizer-request', 'messages': [{'content': json.dumps({'current': {'rubric': 'different', 'version': 'v2'}})}]}]
    recovered = recover_configurations(events)
    assert recovered[0]['configuration']['rubric'] == 'knowledge'
    assert recovered[0]['source_event_id'] == 2
    assert recovered[3]['configuration']['rubric'] == 'different'
    assert 'head' not in recovered[0]['configuration']
    assert 'recovered_configuration' not in events[0]


def test_recorded_snapshot_is_not_replaced_by_reconstruction():
    events = [{'kind': 'step-started', 'classifier_snapshot': {'head': 'recorded'}},
              {'kind': 'optimizer-request', 'messages': [{'content': '{"current":{"rubric":"other"}}'}]}]
    assert 0 not in recover_configurations(events)


def test_embedded_recording_preserves_original_events_without_presentation_fields():
    events = [{'event_id': 1, 'kind': 'round-started'}, {'event_id': 2,
        'kind': 'optimizer-request', 'messages': [{'content': '{"current":{"rubric":"x"}}'}]}]
    html = render_trace(events)
    recording = html.split('<script id="recording" type="application/json">')[1].split('</script>')[0]
    assert json.loads(recording) == events
    assert 'recovered_configuration' not in recording


def test_export_reads_all_events_in_order_without_changing_the_database(tmp_path):
    path = tmp_path / 'runtime.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE runtime_events(id INTEGER PRIMARY KEY,payload TEXT)')
        for number in range(1200):
            db.execute('INSERT INTO runtime_events VALUES (?,?)', (number+1, json.dumps({'kind': 'prediction'})))
    before = path.read_bytes()
    events = read_trace(path)
    assert len(events) == 1200
    assert events[-1]['event_id'] == 1200
    assert path.read_bytes() == before


def test_untrusted_prompt_text_cannot_escape_the_embedded_json_or_execute_markup():
    html = render_trace([{'event_id': 1, 'kind': 'optimizer-request',
        'messages': [{'content': '</script><script>alert(1)</script>&'}]}])
    assert '</script><script>alert(1)' not in html
    assert '\\u003c/script\\u003e' in html
    assert 'textContent' in html
    assert 'fetch(' not in html
    assert 'XMLHttpRequest' not in html


def test_empty_history_is_an_explicit_empty_recording_not_a_fake_measurement():
    html = render_trace([])
    assert 'No recorded events' in html
    assert 'No measured comparison at this event' in html


def test_timeline_marker_size_tracks_visible_slot_width_and_panel_resizes():
    html = render_trace([])
    assert 'function updateMarkerScale()' in html
    assert "timeline.on('rangechange',updateMarkerScale)" in html
    assert 'new ResizeObserver(updateMarkerScale)' in html
    assert "setProperty('--timeline-marker-size'" in html
    assert 'Math.min(20,Math.max(5,slotWidth*.7))' in html
def test_a_matched_endpoint_summary_is_embedded_without_rewriting_the_recording():
    from .trace_artifact import render_trace
    comparison={'scope':'Matched audit <private>','sample_count':18,'before':{'accuracy':.3},'after':{'accuracy':.9}}
    html=render_trace([],run_comparison=comparison)
    assert '<script id="run-comparison" type="application/json">' in html
    assert 'Matched audit \\u003cprivate\\u003e' in html
    assert '"sample_count": 18' in html
