"""Classifier training preflight cannot contact either paid model."""
import importlib.util
import json
from pathlib import Path

from decision_flywheel import ClassifierConfig, DecisionFlywheel
from decision_flywheel.flywheel_test import FakeModel, agent
from decision_flywheel.reviewer_core import reviewer_task
from decision_flywheel.reviewer_store import Article, ReviewStore


def test_training_preflight_preserves_source_and_never_constructs_a_paid_client(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    with ReviewStore(source / "reviews.sqlite3", study_seed="fixture", rolling_audit_rate=0., final_audit_rate=0.) as store:
        records = [Article(str(i), f"Title {i}", f"Abstract {i}", "2026-10-05", ("cs.AI",)) for i in range(40)]
        store.import_articles(records)
        for i, article in enumerate(records):
            store.record_vote(article.id, "include" if i % 2 else "exclude")
    wheel = DecisionFlywheel(source / "runtime.sqlite3", ClassifierConfig(reviewer_task()), FakeModel(), agent([]))
    wheel.close()
    original = [(source / name).read_bytes() for name in ("reviews.sqlite3", "runtime.sqlite3")]
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("training_script", scripts / "train_arxiv_classifier.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight cannot create a paid client")
    monkeypatch.setattr(module.JevAdapter, "from_environment", forbidden)
    output = tmp_path / "training"
    module.main(["--source", str(source), "--output", str(output)])
    protocol = json.loads((output / "preflight.json").read_text())
    assert protocol["optimizer_calls"] == 0
    assert not protocol["final_audit_used"]
    assert original == [(source / name).read_bytes() for name in ("reviews.sqlite3", "runtime.sqlite3")]
