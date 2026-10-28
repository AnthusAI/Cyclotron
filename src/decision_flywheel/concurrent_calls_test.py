import threading
import time
from contextvars import ContextVar

import pytest

from .concurrent_calls import call_concurrently, call_serially


def test_calls_run_at_the_same_time_and_results_keep_the_order_given():
    barrier = threading.Barrier(3, timeout=5)

    def call(value):
        def run():
            barrier.wait()  # Only passes when all three are running at once.
            time.sleep(0.01 * (3 - value))
            return value

        return run

    assert call_concurrently([call(1), call(2), call(3)]) == [1, 2, 3]


def test_every_call_finishes_before_the_first_failure_in_order_is_raised():
    finished = []

    def fail(name):
        def run():
            time.sleep(0.02)
            finished.append(name)
            raise ValueError(name)

        return run

    def ok():
        finished.append("ok")
        return "ok"

    with pytest.raises(ValueError, match="first"):
        call_concurrently([ok, fail("first"), fail("second")])
    assert sorted(finished) == ["first", "ok", "second"]


def test_each_call_sees_the_callers_context():
    marker = ContextVar("marker", default=None)
    marker.set("caller")
    assert call_concurrently([marker.get, marker.get]) == ["caller", "caller"]


def test_no_calls_and_one_call_need_no_thread():
    assert call_concurrently([]) == []
    assert call_concurrently([threading.get_ident]) == [threading.get_ident()]


def test_the_serial_form_also_runs_every_call_before_raising_the_first_failure():
    seen = []

    def fail():
        seen.append("fail")
        raise KeyError("one")

    with pytest.raises(KeyError):
        call_serially([fail, lambda: seen.append("after")])
    assert seen == ["fail", "after"]
    assert call_serially([lambda: 1, lambda: 2]) == [1, 2]
