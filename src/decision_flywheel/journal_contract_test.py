"""Journal behaviour that every back end must share, plus the S3 read cost."""
import pytest

from .batch_storage import StorageScope
from .journal import Journal, JournalConflict, JournalLimit, JournalUnavailable
from .s3_journal_test import ConditionalS3
from .s3_journal_test import journal as s3_journal
from .storage_contract_backends import local_file_journal


@pytest.fixture(params=['s3', 'file'])
def open_journal(request, tmp_path):
    fake_s3 = ConditionalS3()

    def open_named(name='run-a', scope=None):
        if request.param == 's3':
            return s3_journal(fake_s3, scope, name)
        return local_file_journal(tmp_path, name, scope)

    def remove_anchor(opened):
        if request.param == 's3':
            del fake_s3.objects[opened.anchor_key]
        else:
            opened._path(opened.anchor_key).unlink()

    open_named.remove_anchor = remove_anchor
    return open_named


def test_both_back_ends_satisfy_the_journal_protocol(open_journal):
    assert isinstance(open_journal(), Journal)


def test_a_committed_value_survives_reopening_the_namespace(open_journal):
    first = open_journal().initialize({'exact': '雪', 'n': 9007199254740993})
    second = open_journal().commit(first, {'exact': 'changed'})
    reopened = open_journal().read()
    assert reopened.value == {'exact': 'changed'}
    assert (first.revision, second.revision, reopened.revision) == (1, 2, 2)


def test_initializing_an_existing_namespace_returns_its_current_record(open_journal):
    opened = open_journal()
    first = opened.initialize({'v': 1})
    opened.commit(first, {'v': 2})
    assert open_journal().initialize({'v': 0}).value == {'v': 2}


def test_a_stale_record_cannot_replace_its_successor(open_journal):
    opened = open_journal()
    first = opened.initialize({'budget': 1})
    opened.commit(first, {'budget': 2})
    with pytest.raises(JournalConflict):
        opened.commit(first, {'budget': 0})
    assert opened.read().value == {'budget': 2}


def test_a_commit_from_another_handle_makes_the_first_handle_stale(open_journal):
    first = open_journal().initialize({'n': 0})
    open_journal().commit(first, {'n': 1})
    with pytest.raises(JournalConflict):
        open_journal().commit(first, {'n': 2})


def test_existence_is_false_before_initialization_and_true_after(open_journal):
    opened = open_journal()
    assert opened.exists() is False
    opened.initialize({'x': 1})
    assert open_journal().exists() is True


def test_a_head_without_an_anchor_is_reported_unavailable_not_absent(open_journal):
    opened = open_journal()
    opened.initialize({'x': 1})
    open_journal.remove_anchor(opened)
    with pytest.raises(JournalUnavailable):
        open_journal().exists()


def test_explicit_byte_budget_refuses_oversized_values_without_mutation(open_journal):
    opened = open_journal()
    head = opened.initialize({'x': 1})
    with pytest.raises(JournalLimit):
        opened.commit(head, {'x': 'a' * 1000001})
    assert opened.read().value == {'x': 1}


def test_a_foreign_scope_cannot_commit_another_scopes_record(open_journal):
    head = open_journal().initialize({'x': 1})
    with pytest.raises(PermissionError):
        open_journal(scope=StorageScope('foreign', 'workspace-a')).commit(head, {'x': 2})


def test_the_boundary_tells_same_storage_from_different_storage(open_journal):
    assert open_journal('a').boundary == open_journal('b').boundary
    assert open_journal('a').boundary != open_journal('a', StorageScope('foreign', 'workspace-a')).boundary


def test_a_steady_state_s3_read_costs_two_gets_and_a_commit_costs_none_for_the_anchor():
    fake = ConditionalS3()
    opened = s3_journal(fake)
    record = opened.initialize({'n': 0})
    fake.calls.clear()
    opened.read()
    assert [kind for kind, _ in fake.calls].count('get') == 2
    fake.calls.clear()
    opened.commit(record, {'n': 1})
    assert [kind for kind, _ in fake.calls].count('get') == 0
    assert opened.anchor_key not in [request['Key'] for _, request in fake.calls]


def test_a_new_s3_handle_reads_the_anchor_once_then_two_gets_per_read():
    fake = ConditionalS3()
    s3_journal(fake).initialize({'n': 0})
    reopened = s3_journal(fake)
    fake.calls.clear()
    reopened.read()
    first_read_gets = len(fake.calls)
    fake.calls.clear()
    reopened.read()
    assert (first_read_gets, len(fake.calls)) == (3, 2)
