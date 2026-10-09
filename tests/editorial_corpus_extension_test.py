"""The extended editorial corpus keeps its first 400 items byte for byte."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BASE_SHA256 = "3ce5aacf63e4babf2a7205e29057a34f181d3539a70ae022df331978c561277e"
MANIFEST = ROOT / "docs/experiments/editorial-wikinews-1200.manifest.json"


def _script():
    spec = importlib.util.spec_from_file_location("extend_editorial_corpus", ROOT / "scripts/extend_editorial_corpus.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(number, title, text="word " * 80, categories=()):
    return {"wiki_page_id": number, "wiki_revision_id": number * 10, "title": title, "cleaned_text": text,
            "categories": list(categories), "url": f"http://en.wikinews.org/?curid={number}",
            "article_timestamp": datetime(2019, 1, 1, tzinfo=timezone.utc), "source_file": "en-2019.parquet"}


def test_the_extension_appends_after_the_unchanged_base_bytes():
    module = _script()
    base = b'{"id": "a"}\n{"id": "b"}\n'
    extended = module.extended_bytes(base, [{"id": "c", "sequence": 3}])
    assert extended.startswith(base)
    assert extended[len(base):].decode().splitlines() == ['{"id": "c", "sequence": 3}']
    with pytest.raises(ValueError, match="newline"):
        module.extended_bytes(b'{"id": "a"}', [])


def test_curation_selects_the_high_relevance_share_then_keeps_the_rest_as_a_frozen_reserve():
    module = _script()
    rows = [_row(i, f"Volunteers rescue and restore community habitat {i}") for i in range(10)]
    rows += [_row(100 + i, f"Council meets {i}", categories=("Sports",) if i % 2 else ()) for i in range(30)]
    first = [module.identifier(r) for r in module.curation_order(rows, 20, "seed")]
    again = [module.identifier(r) for r in module.curation_order(rows, 20, "seed")]
    assert first == again and len(first) == len(set(first)) == 40
    chosen = set(first[:20])
    assert sum(identifier in chosen for identifier in (module.identifier(r) for r in rows[:10])) == 10
    with pytest.raises(ValueError, match="pool"):
        module.curation_order(rows[:5], 20, "seed")


def test_a_label_must_agree_with_its_factor_and_quote_the_excerpt():
    module = _script()
    excerpt = "The clinic opened and “treated 400 children” this week."
    assert module.verified_span({"label": "publish", "factor": "benefit", "evidence_span": 'treated 400 children'}, excerpt)
    assert not module.verified_span({"label": "publish", "factor": "benefit", "evidence_span": "cured everyone"}, excerpt)
    with pytest.raises(ValueError, match="incoherent"):
        module.verified_span({"label": "reject", "factor": "benefit", "evidence_span": "treated"}, excerpt)


def test_the_manifest_records_the_unchanged_first_400_and_the_label_procedure():
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["item_count"] >= 1200
    assert manifest["first_400"] == {"corpus": "uplifting-editorial-wikinews-400-semantic-v2", "sha256": BASE_SHA256,
                                     "byte_identical_prefix": True, "lines": 400}
    assert manifest["dataset_revision"] == "b4c2ec3857fcac203c40b8d61586e934ed07c128"
    assert manifest["license"].startswith("CC BY 2.5")
    extension = manifest["extension"]
    assert hashlib.sha256(extension["label_prompt"].encode()).hexdigest() == extension["label_prompt_sha256"]
    assert extension["label_prompt"] == _script().RUBRIC


@pytest.mark.skipif(not os.environ.get("EDITORIAL_CORPUS_1200"), reason="set EDITORIAL_CORPUS_1200 to the local corpus file")
def test_the_local_corpus_matches_its_manifest_and_starts_with_the_400_item_corpus():
    data = Path(os.environ["EDITORIAL_CORPUS_1200"]).read_bytes()
    manifest = json.loads(MANIFEST.read_text())
    assert hashlib.sha256(data).hexdigest() == manifest["sha256"]
    lines = data.splitlines(keepends=True)
    assert len(lines) == manifest["item_count"]
    assert hashlib.sha256(b"".join(lines[:400])).hexdigest() == BASE_SHA256
    assert len({json.loads(line)["id"] for line in lines}) == len(lines)
