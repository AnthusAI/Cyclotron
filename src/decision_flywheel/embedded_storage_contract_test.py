"""Embedded root behaviour that SQLite and S3 must share, run against both back ends.

Writes that must survive a reopen are made inside ``transaction()``, the
documented unit of durability for the root domain.
"""
import pytest

from .storage_errors import StorageMissing


@pytest.fixture
def storage(embedded_backend):
    return embedded_backend.open()


def decision_row(identifier, item_id, state='provisional', fingerprint='fingerprint'):
    return (identifier, item_id, fingerprint, state, '{"payload":"' + identifier + '"}')


def test_items_keep_first_insertion_order_when_one_is_replaced(storage):
    storage.put_item('first', '{"v":1}', 'a')
    storage.put_item('second', '{"v":2}', 'b')
    storage.put_item('first', '{"v":3}', 'c')
    assert storage.items() == [('first', '{"v":3}'), ('second', '{"v":2}')]
    assert storage.item_values('first') == '{"v":3}'
    assert storage.item_values('absent') is None


def test_decisions_are_listed_in_insertion_order_with_matching_projections(storage):
    storage.insert_decision(decision_row('d1', 'item-a'))
    storage.insert_decision(decision_row('d2', 'item-b'))
    storage.insert_decision(decision_row('d3', 'item-a'))
    assert storage.decision_count() == 3
    assert storage.decision_ids() == [('d1',), ('d2',), ('d3',)]
    assert storage.decision_items() == [('d1', 'item-a'), ('d2', 'item-b'), ('d3', 'item-a')]
    assert storage.decision_rows() == [('d1', 'provisional', '{"payload":"d1"}'),
                                       ('d2', 'provisional', '{"payload":"d2"}'),
                                       ('d3', 'provisional', '{"payload":"d3"}')]
    assert storage.decision_payloads() == [('{"payload":"d1"}',), ('{"payload":"d2"}',), ('{"payload":"d3"}',)]
    assert storage.window_decisions(2, 1) == [('d2', '{"payload":"d2"}'), ('d3', '{"payload":"d3"}')]
    assert storage.window_decisions(10, 3) == []
    assert storage.decision('d2') == ('{"payload":"d2"}',)
    assert storage.decision('absent') is None


def test_a_duplicate_decision_identity_is_refused_and_changes_nothing(storage):
    storage.insert_decision(decision_row('d1', 'item-a'))
    with pytest.raises(ValueError):
        storage.insert_decision(decision_row('d1', 'item-b'))
    assert storage.decision_count() == 1
    assert storage.decision_items() == [('d1', 'item-a')]


def test_the_latest_decision_skips_superseded_ones_but_the_latest_id_does_not(storage):
    storage.insert_decision(decision_row('d1', 'item-a'))
    storage.insert_decision(decision_row('d2', 'item-a'))
    storage.insert_decision(decision_row('d3', 'item-b'))
    storage.set_decision_state('d2', 'superseded')
    assert storage.latest_decision('item-a') == ('provisional', '{"payload":"d1"}')
    assert storage.latest_id('item-a') == ('d2',)
    assert storage.latest_id() == ('d3',)
    storage.set_decision_state('d1', 'superseded')
    assert storage.latest_decision('item-a') is None
    assert storage.latest_decision('never-seen') is None
    assert storage.latest_id('never-seen') is None


def test_a_state_change_can_refuse_to_revive_a_superseded_decision(storage):
    storage.insert_decision(decision_row('d1', 'item-a'))
    storage.set_decision_state('d1', 'superseded')
    storage.set_decision_state('d1', 'confirmed', unless_superseded=True)
    assert storage.decision_rows()[0][1] == 'superseded'
    storage.set_decision_state('d1', 'confirmed')
    assert storage.decision_rows()[0][1] == 'confirmed'


def test_a_missing_decision_fingerprint_is_storage_missing(storage):
    storage.insert_decision(decision_row('d1', 'item-a', fingerprint='known'))
    assert storage.decision_fingerprint('d1') == 'known'
    with pytest.raises(StorageMissing):
        storage.decision_fingerprint('absent')


def test_reviews_keep_append_order_per_decision_and_classifier(storage):
    storage.insert_review('r1', 'd1', 'relevant', '{"n":1}')
    storage.insert_review('r2', 'd1', 'relevant', '{"n":2}')
    storage.insert_review('r3', 'd1', 'urgent', '{"n":3}')
    assert storage.reviews('d1', 'relevant') == [('{"n":1}',), ('{"n":2}',)]
    assert storage.last_review('d1', 'relevant') == ('{"n":2}',)
    assert storage.last_review('d1', 'absent') is None
    assert storage.review('r3') == ('{"n":3}',)
    assert storage.review('absent') is None
    assert storage.review_count('relevant') == 2
    assert storage.review_count('absent') == 0


def test_a_duplicate_review_identity_is_refused_and_changes_nothing(storage):
    storage.insert_review('r1', 'd1', 'relevant', '{"n":1}')
    with pytest.raises(ValueError):
        storage.insert_review('r1', 'd2', 'urgent', '{"n":2}')
    assert storage.review_count('relevant') == 1
    assert storage.review_count('urgent') == 0


def test_metadata_round_trips_and_refuses_or_ignores_a_duplicate_key(storage):
    assert storage.meta('cursor') is None
    storage.insert_meta('cursor', '1')
    assert storage.meta('cursor') == ('1',)
    with pytest.raises(ValueError):
        storage.insert_meta('cursor', '2')
    storage.insert_meta('cursor', '3', ignore=True)
    assert storage.meta('cursor') == ('1',)
    storage.put_meta('cursor', '4')
    assert storage.meta('cursor') == ('4',)


def test_metadata_and_records_survive_reopening_the_same_store(embedded_backend):
    storage = embedded_backend.open()
    with storage.transaction():
        storage.put_meta('cursor', '7')
        storage.put_item('item-a', '{"v":1}', 'fp')
        storage.insert_decision(decision_row('d1', 'item-a'))
    reopened = embedded_backend.reopen(storage)
    assert reopened.meta('cursor') == ('7',)
    assert reopened.items() == [('item-a', '{"v":1}')]
    assert reopened.decision_count() == 1


def test_events_get_increasing_identifiers_and_page_after_a_cursor(storage):
    assert [storage.append_event(f'{{"n":{number}}}') for number in (1, 2, 3)] == [1, 2, 3]
    assert storage.events(0, 2) == [(1, '{"n":1}'), (2, '{"n":2}')]
    assert storage.events(2, 10) == [(3, '{"n":3}')]
    assert storage.events(3, 10) == []


def test_usage_rows_and_shares_keep_order_and_refuse_a_repeated_share(storage):
    first = storage.insert_usage('d1', 'model-a', 10, 2, 5, 'live')
    second = storage.insert_usage('d2', 'model-b', 20, 0, 6, None)
    assert (first, second) == (1, 2)
    storage.insert_usage_share(first, 'relevant', .25)
    storage.insert_usage_share(first, 'urgent', .75)
    with pytest.raises(ValueError):
        storage.insert_usage_share(first, 'relevant', .5)
    assert storage.usage_counts() == [('d1', 'model-a', 10, 2, 5), ('d2', 'model-b', 20, 0, 6)]
    assert storage.usage_by_output() == [(1, 'model-a', 10, 2, 5, 'live'), (2, 'model-b', 20, 0, 6, None)]
    assert sorted(storage.usage_shares()) == [(1, 'relevant', .25), (1, 'urgent', .75)]


def test_the_learning_queue_orders_dedupes_supersedes_requeues_and_deletes(storage):
    storage.enqueue_learning('relevant', 3, '2026-01-01')
    storage.enqueue_learning('urgent', 5, '2026-01-02')
    storage.enqueue_learning('relevant', 9, '2026-01-03')
    assert storage.pending_learning() == ['relevant', 'urgent']
    first_sequence, first_classifier, first_since = storage.next_learning()
    assert (first_classifier, first_since) == ('relevant', 3)
    assert storage.learning_rows() == [(first_sequence, 'relevant', 3, 0), (first_sequence + 1, 'urgent', 5, 0)]
    storage.supersede_learning(first_sequence)
    storage.supersede_learning(first_sequence)
    assert storage.learning_rows()[0] == (first_sequence, 'relevant', 3, 2)
    storage.requeue_learning(first_sequence, 12)
    assert storage.learning_rows()[0] == (first_sequence, 'relevant', 12, 0)
    storage.delete_learning(first_sequence)
    assert storage.pending_learning() == ['urgent']
    assert storage.next_learning()[1:] == ('urgent', 5)
    storage.delete_learning(storage.next_learning()[0])
    assert storage.next_learning() is None


def test_a_transaction_commits_every_write_together(storage):
    with storage.transaction():
        storage.put_meta('cursor', '1')
        storage.append_event('{"n":1}')
        storage.insert_decision(decision_row('d1', 'item-a'))
    assert storage.meta('cursor') == ('1',)
    assert storage.events(0, 10) == [(1, '{"n":1}')]
    assert storage.decision_count() == 1


def test_a_failed_transaction_leaves_no_partial_writes(storage):
    with storage.transaction():
        storage.put_meta('cursor', '1')
    with pytest.raises(RuntimeError, match='synthetic failure'):
        with storage.transaction():
            storage.put_meta('cursor', '2')
            storage.append_event('{"n":1}')
            storage.insert_decision(decision_row('d1', 'item-a'))
            raise RuntimeError('synthetic failure')
    assert storage.meta('cursor') == ('1',)
    assert storage.events(0, 10) == []
    assert storage.decision_count() == 0
