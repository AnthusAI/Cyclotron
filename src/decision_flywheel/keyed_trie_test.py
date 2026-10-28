"""A keyed trie keeps a map out of the journal head: changing a key rewrites only its path."""

import random

from .batch_storage import StorageScope
from .keyed_trie import KeyedTrie
from .storage_payloads import FilePayloads


def trie(tmp_path):
    return KeyedTrie(FilePayloads(tmp_path, StorageScope("account-a", "workspace-a")))


def publish(t, root, changes):
    root, written = t.update(root, changes)
    for content in written.values():
        t.payloads.put(content)
    return root, written


def test_values_set_and_removed_are_read_back_and_a_missing_key_reads_as_none(tmp_path):
    t = trie(tmp_path)
    root, _ = publish(t, None, {f"key-{n}": f"value {n}" for n in range(300)})
    root, _ = publish(t, root, {"key-7": "changed", "key-8": None, "new": "雪"})
    fresh = KeyedTrie(t.payloads)
    assert fresh.get(root, "key-7") == "changed" and fresh.get(root, "key-8") is None and fresh.get(root, "new") == "雪"
    assert fresh.get(root, "never-written") is None
    assert dict(fresh.items(root)) == {
        **{f"key-{n}": f"value {n}" for n in range(300) if n not in (7, 8)},
        "key-7": "changed",
        "new": "雪",
    }


def test_the_same_contents_give_the_same_root_whatever_order_they_were_written_in(tmp_path):
    t = trie(tmp_path)
    keys = [f"item-{n}" for n in range(200)]
    one, _ = publish(t, None, {key: key.upper() for key in keys})
    shuffled = keys[:]
    random.Random(4).shuffle(shuffled)
    other = None
    for key in shuffled + ["extra-1", "extra-2"]:
        other, _ = publish(t, other, {key: key.upper()})
    other, _ = publish(t, other, {"extra-1": None})
    other, _ = publish(t, other, {"extra-2": None})
    assert one == other


def test_removing_every_key_leaves_no_root_and_an_unchanged_update_writes_nothing(tmp_path):
    t = trie(tmp_path)
    root, _ = publish(t, None, {"a": "1", "b": "2"})
    again, written = t.update(root, {"a": "1", "absent": None})
    assert again == root and written == {}
    empty, _ = publish(t, root, {"a": None, "b": None})
    assert empty is None


def test_changing_one_key_writes_only_the_nodes_on_its_path(tmp_path):
    t = trie(tmp_path)
    root, _ = publish(t, None, {f"item-{n}": "x" * 40 for n in range(4000)})
    _, written = t.update(root, {"item-17": "y" * 40})
    assert 1 <= len(written) <= 5
    assert sum(len(content) for content in written.values()) < 12_000
