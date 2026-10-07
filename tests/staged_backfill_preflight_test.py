"""The live stage copies current state without contacting models during preflight."""
import importlib.util
import json
from pathlib import Path

from decision_flywheel import ClassifierConfig, DecisionFlywheel, FittedClassifier
from decision_flywheel.flywheel_test import FakeModel, agent
from decision_flywheel.reviewer_core import reviewer_task
from decision_flywheel.reviewer_store import Article, ReviewStore


def test_preflight_freezes_the_current_reviewer_and_never_constructs_paid_clients(tmp_path, monkeypatch):
    reviews, runtime = tmp_path / "reviews.sqlite", tmp_path / "runtime.sqlite"
    with ReviewStore(reviews, study_seed="fixture", rolling_audit_rate=0., final_audit_rate=0.) as store:
        articles = [Article(str(i), f"Title {i}", f"Abstract {i}", "2026-10-05", ("cs.AI",)) for i in range(40)]
        store.import_articles(articles)
        for i, article in enumerate(articles):
            store.record_vote(article.id, "include" if i % 2 else "exclude")
    config = ClassifierConfig(reviewer_task(), rubric="Current criteria")
    wheel = DecisionFlywheel(runtime, config, FakeModel(), agent([]))
    wheel._activate(FittedClassifier(config))
    wheel.close()
    original = reviews.read_bytes(), runtime.read_bytes()
    spec = importlib.util.spec_from_file_location("staged_script", Path(__file__).parents[1] / "scripts/measure_arxiv_questions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not construct a paid model")
    monkeypatch.setattr(module.JevAdapter, "from_environment", forbidden)
    monkeypatch.setattr(module, "optimizer_transport", forbidden)
    output = tmp_path / "backfill"
    assert module.main(["--database", str(reviews), "--runtime", str(runtime), "--output", str(output)]) == 0
    assert (reviews.read_bytes(), runtime.read_bytes()) == original
    assert (output / "preflight.json").exists()
    preflight=output/'preflight.json'
    previous=json.loads(preflight.read_text());del previous['optimizer_transport']
    old=json.dumps(previous,indent=2)+'\n';preflight.write_text(old)
    assert module.main(["--database",str(reviews),"--runtime",str(runtime),"--output",str(output)])==0
    assert preflight.read_text()==old
    assert (reviews.read_bytes(),runtime.read_bytes())==original
