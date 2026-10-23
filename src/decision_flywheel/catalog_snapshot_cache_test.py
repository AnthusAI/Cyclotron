"""The decoded-catalog memory is bounded and keyed by journal scope."""

from .catalog_snapshot_cache import CatalogSnapshotCache
from .s3_journal_test import journal


def test_it_keeps_one_entry_per_journal_and_replaces_it_when_a_newer_record_is_put():
    cache = CatalogSnapshotCache()
    j = journal(name="console")
    first = j.initialize({"n": 1})
    second = j.commit(first, {"n": 2})
    cache.put(j, first)
    cache.put(j, second)
    assert len(cache) == 1 and cache.get(j) is second


def test_it_never_serves_one_scope_from_another_scopes_entry():
    cache = CatalogSnapshotCache()
    first, other = journal(name="console"), journal(name="console-other")
    cache.put(first, first.initialize({"n": 1}))
    assert cache.get(other) is None


def test_it_evicts_the_least_recently_used_entry_beyond_its_entry_bound():
    cache = CatalogSnapshotCache(maximum_entries=2)
    journals = [journal(name=f"console-{n}") for n in range(3)]
    for j in journals[:2]:
        cache.put(j, j.initialize({"n": 1}))
    cache.get(journals[0])
    cache.put(journals[2], journals[2].initialize({"n": 1}))
    assert cache.get(journals[1]) is None
    assert cache.get(journals[0]) is not None and cache.get(journals[2]) is not None


def test_it_stays_within_its_serialized_byte_bound_and_skips_a_snapshot_larger_than_the_bound():
    small = journal(name="console-small")
    record = small.initialize({"n": 1})
    cache = CatalogSnapshotCache(maximum_serialized_bytes=record.reference.length * 2)
    others = [journal(name=f"console-{n}") for n in range(3)]
    for j in others:
        cache.put(j, j.initialize({"n": 1}))
    assert len(cache) == 2
    tiny = CatalogSnapshotCache(maximum_serialized_bytes=record.reference.length - 1)
    tiny.put(small, record)
    assert len(tiny) == 0
