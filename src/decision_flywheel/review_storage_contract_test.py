"""Reviewer source behaviour that SQLite and S3 must share, run against both back ends.

Articles are imported inside ``transaction()``, the unit of durability both
back ends honour.
"""
import json

import pytest


@pytest.fixture
def storage(review_backend):
    return review_backend.open()


def article_record(identifier, title='Title', assignment='train', authors='Not provided', journal=None):
    return (identifier, title, 'Abstract', '2026-01-01', json.dumps(['cs.AI']), authors, journal, assignment)


def presentation_row(identifier, article_id, shown_at='2026-01-02T00:00:00'):
    return (identifier, article_id, 'include', .75, 'synthetic', 'fingerprint', 3, shown_at)


def action_row(identifier, article_id, action='vote', label='include', created_at='2026-01-03T00:00:00',
               undoes_event_id=None, presentation_id=None):
    return (identifier, article_id, action, label, None, created_at, undoes_event_id, presentation_id)


def import_articles(storage, *records):
    return storage.import_articles(list(records))


def record_action(storage, row):
    with storage.transaction():
        storage.append_action(row)


def record_presentation(storage, row):
    with storage.transaction():
        storage.append_presentation(row)


def test_study_metadata_binds_once_and_refuses_a_different_value(storage):
    storage.bind_study({'seed': 'one', 'rate': '0.1'})
    storage.bind_study({'seed': 'one'})
    with pytest.raises(ValueError, match='different seed'):
        storage.bind_study({'seed': 'two'})


def test_a_conflicting_bind_rolls_back_the_new_keys_it_carried(storage):
    storage.bind_study({'seed': 'one'})
    with pytest.raises(ValueError):
        storage.bind_study({'fresh': 'value', 'seed': 'two'})
    storage.bind_study({'fresh': 'other'})


def test_importing_articles_counts_new_ones_and_ignores_identical_repeats(storage):
    assert import_articles(storage, article_record('b'), article_record('a')) == 2
    assert import_articles(storage, article_record('a'), article_record('c')) == 1
    assert [article['id'] for article in storage.articles()] == ['a', 'b', 'c']


def test_an_article_identity_cannot_be_reused_for_different_content(storage):
    import_articles(storage, article_record('a', title='Original'))
    with pytest.raises(ValueError, match='different content'):
        import_articles(storage, article_record('a', title='Changed'))
    assert storage.article('a')['title'] == 'Original'


def test_legacy_authorless_articles_gain_authors_only_while_unreviewed(storage):
    import_articles(storage, article_record('a'), article_record('b'))
    record_action(storage, action_row('vote-b', 'b'))
    import_articles(storage, article_record('a', authors='Ada', journal='Journal'),
                    article_record('b', authors='Bo', journal='Journal'))
    assert (storage.article('a')['authors'], storage.article('a')['journal_ref']) == ('Ada', 'Journal')
    assert (storage.article('b')['authors'], storage.article('b')['journal_ref']) == ('Not provided', None)


def test_a_single_article_is_found_by_identity_or_absent(storage):
    import_articles(storage, article_record('a', assignment='rolling_audit'))
    found = storage.article('a')
    assert found['assignment'] == 'rolling_audit' and found['title'] == 'Title'
    assert storage.article('absent') is None


def test_actions_are_ordered_by_time_then_arrival_and_can_be_filtered_by_article(storage):
    import_articles(storage, article_record('a'), article_record('b'))
    record_action(storage, action_row('late', 'a', created_at='2026-01-05T00:00:00'))
    record_action(storage, action_row('early', 'b', created_at='2026-01-04T00:00:00'))
    record_action(storage, action_row('tied-first', 'a', created_at='2026-01-06T00:00:00'))
    record_action(storage, action_row('tied-second', 'b', created_at='2026-01-06T00:00:00'))
    assert [action['id'] for action in storage.actions()] == ['early', 'late', 'tied-first', 'tied-second']
    assert [action['id'] for action in storage.actions('a')] == ['late', 'tied-first']
    assert storage.actions('absent') == []


def test_presentations_are_ordered_by_time_then_arrival_and_can_be_looked_up(storage):
    import_articles(storage, article_record('a'), article_record('b'))
    record_presentation(storage, presentation_row('second', 'a', shown_at='2026-01-03T00:00:00'))
    record_presentation(storage, presentation_row('first', 'b', shown_at='2026-01-02T00:00:00'))
    record_presentation(storage, presentation_row('tied', 'a', shown_at='2026-01-03T00:00:00'))
    assert [row['id'] for row in storage.presentations()] == ['first', 'second', 'tied']
    assert [row['id'] for row in storage.presentations('a')] == ['second', 'tied']
    assert storage.presentation('first')['article_id'] == 'b'
    assert storage.presentation('absent') is None


def test_an_action_keeps_the_prediction_it_answered_and_an_undo_points_at_its_target(storage):
    import_articles(storage, article_record('a'))
    record_presentation(storage, presentation_row('shown', 'a'))
    record_action(storage, action_row('vote', 'a', presentation_id='shown'))
    record_action(storage, action_row('undo', 'a', action='undo', label=None, undoes_event_id='vote',
                                      created_at='2026-01-04T00:00:00'))
    vote, undo = storage.actions('a')
    assert vote['presentation_id'] == 'shown' and vote['label'] == 'include'
    assert undo['undoes_event_id'] == 'vote' and undo['label'] is None


def test_records_survive_reopening_the_same_store(review_backend):
    storage = review_backend.open()
    storage.bind_study({'seed': 'one'})
    import_articles(storage, article_record('a'))
    record_presentation(storage, presentation_row('shown', 'a'))
    record_action(storage, action_row('vote', 'a', presentation_id='shown'))
    reopened = review_backend.reopen(storage)
    assert [article['id'] for article in reopened.articles()] == ['a']
    assert [row['id'] for row in reopened.presentations()] == ['shown']
    assert [row['id'] for row in reopened.actions()] == ['vote']
    with pytest.raises(ValueError):
        reopened.bind_study({'seed': 'two'})


def test_a_transaction_commits_every_write_together(storage):
    with storage.transaction():
        import_articles(storage, article_record('a'))
        storage.append_presentation(presentation_row('shown', 'a'))
        storage.append_action(action_row('vote', 'a', presentation_id='shown'))
    assert len(storage.articles()) == 1
    assert len(storage.presentations()) == 1
    assert len(storage.actions()) == 1


def test_a_failed_transaction_leaves_no_partial_writes(storage):
    import_articles(storage, article_record('a'))
    with pytest.raises(RuntimeError, match='synthetic failure'):
        with storage.transaction():
            import_articles(storage, article_record('b'))
            storage.append_action(action_row('vote', 'a'))
            raise RuntimeError('synthetic failure')
    assert [article['id'] for article in storage.articles()] == ['a']
    assert storage.actions() == []


def test_a_duplicate_action_identity_is_refused_and_changes_nothing(storage):
    import_articles(storage, article_record('a'))
    record_action(storage, action_row('vote', 'a'))
    with pytest.raises(ValueError):
        with storage.transaction():
            storage.append_action(action_row('vote', 'a', label='exclude'))
    assert [action['label'] for action in storage.actions()] == ['include']
