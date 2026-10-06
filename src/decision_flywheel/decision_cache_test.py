import asyncio
import pytest

from .decision_cache import CacheOptions, CacheMiss
from .observability_test import make_wheel
from .flywheel_test import TRAIN


def test_identical_requests_are_reused_and_explicit_refresh_preserves_the_old_answer(tmp_path):
    wheel = make_wheel(tmp_path / 'runtime.sqlite', [])
    for _ in range(2):
        asyncio.run(wheel.predict(TRAIN[0].item, TRAIN))
    assert wheel.model.calls == 1
    asyncio.run(wheel.predict(TRAIN[0].item, TRAIN, cache_options=CacheOptions('refresh')))
    assert wheel.model.calls == 2
    assert wheel.db.execute('SELECT count(*) FROM runtime_answer_history').fetchone()[0] == 1
    wheel.close()


def test_cache_only_misses_make_no_decision_model_call(tmp_path):
    wheel = make_wheel(tmp_path / 'runtime.sqlite', [])
    with pytest.raises(CacheMiss):
        asyncio.run(wheel.predict(TRAIN[0].item, TRAIN, cache_options=CacheOptions('cache_only')))
    assert wheel.model.calls == 0
    wheel.close()


def test_pending_requests_require_separate_retry_permission_even_when_refreshing(tmp_path):
    wheel = make_wheel(tmp_path / 'runtime.sqlite', [])
    asyncio.run(wheel.predict(TRAIN[0].item, TRAIN))
    wheel.db.execute("UPDATE runtime_answers SET status='pending'")
    wheel.db.commit()
    with pytest.raises(RuntimeError, match='explicit retry'):
        asyncio.run(wheel.predict(TRAIN[0].item, TRAIN, cache_options=CacheOptions('refresh')))
    assert wheel.model.calls == 1
    asyncio.run(wheel.predict(TRAIN[0].item, TRAIN, cache_options=CacheOptions(retry_failed=True)))
    assert wheel.model.calls == 2
    wheel.close()
