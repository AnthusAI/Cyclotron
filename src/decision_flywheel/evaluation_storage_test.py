"""Frozen evaluation clocks and trace identities survive uncertain delivery."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from .evaluation_storage import SQLiteEvaluationStorage


def test_racing_clock_binds_and_trace_retries_keep_one_original_identity(tmp_path):
    path = tmp_path / "evaluation.sqlite3"
    barrier = Barrier(2)

    def record(index):
        store = SQLiteEvaluationStorage(path)
        barrier.wait()
        clock = store.bind_clock("plan", f"2026-01-0{index + 1}T00:00:00+00:00")
        event = store.append_trace("event", '{ "timestamp":"first", "note":"雪" }')
        return clock, event

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(record, (0, 1)))
    assert results[0] == results[1]
    store = SQLiteEvaluationStorage(path)
    assert store.append_trace("event", '{"timestamp":"later"}') == results[0][1]
    assert store.events(after=0, limit=1) == [results[0][1]]
