"""Existing review batches must remain stable and require no new dataset calls."""
import importlib.util
from pathlib import Path
import json


def test_a_saved_arxiv_batch_is_reused_without_network_or_overwriting_votes(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "scripts" / "seed_arxiv_reviewer.py"
    spec = importlib.util.spec_from_file_location("seed_arxiv", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def forbidden(*args, **kwargs):
        raise AssertionError("saved batches must not call the network")
    monkeypatch.setattr(module, "HfApi", forbidden)
    batch = tmp_path / "batch.jsonl"
    text = json.dumps({"id": "arxiv:one", "title": "One", "abstract": "Abstract", "submitted_at": "2026-10-05",
                       "categories": ["cs.AI"]}) + "\n"
    batch.write_text(text)
    assert module.main(["--output", str(batch), "--limit", "1"]) == 0
    assert batch.read_text() == text
