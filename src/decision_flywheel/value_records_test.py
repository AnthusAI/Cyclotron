"""Item values live once, by content, beside the journal that cites them."""

import pytest

from .batch_storage import StorageScope
from .storage_payloads import FilePayloads, PayloadCorrupt
from .value_records import REF, externalized, get_values, put_values, resolved, same_values


def store(tmp_path):
    return FilePayloads(tmp_path, StorageScope("account-a", "console"))


def test_a_large_values_document_is_stored_once_and_read_back_exactly(tmp_path):
    payloads = store(tmp_path)
    values = {"title": "T", "body": "word " * 100_000, "text": "T\nhttps://x\n\nsummary"}
    ref = put_values(payloads, values)
    assert put_values(payloads, dict(reversed(list(values.items())))) == ref  # canonical: key order is irrelevant
    assert get_values(payloads, ref) == values
    assert len(list((tmp_path).rglob("*.blob"))) == 1


def test_a_document_keeps_only_a_reference_and_resolves_to_the_same_values(tmp_path):
    payloads = store(tmp_path)
    document = {"id": "hn-1", "values": {"title": "T", "body": "b" * 300_000}, "occurred_at": "2026-10-28T00:00:00Z"}
    stored = externalized(payloads, document)
    assert "values" not in stored and set(stored[REF]) == {"digest", "length"} and len(str(stored)) < 300
    assert resolved(payloads, stored) == document
    assert same_values(stored, document["values"]) and not same_values(stored, {"title": "T"})


def test_a_document_written_before_references_reads_exactly_as_before(tmp_path):
    inline = {"id": "hn-1", "values": {"title": "T"}}
    assert resolved(store(tmp_path), inline) == inline and same_values(inline, {"title": "T"})


def test_a_tampered_record_is_refused_rather_than_read(tmp_path):
    payloads = store(tmp_path)
    ref = put_values(payloads, {"title": "T"})
    (blob,) = tmp_path.rglob("*.blob")
    blob.write_bytes(b'{"title":"X"}')
    with pytest.raises(PayloadCorrupt):
        get_values(payloads, ref)
