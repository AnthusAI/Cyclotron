import sqlite3
import json

from .trace_artifact import read_trace, render_trace


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
