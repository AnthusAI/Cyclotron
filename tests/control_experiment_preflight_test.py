"""Individual-feature experiment preflight does not authorize live collection."""
import importlib.util
import json
from pathlib import Path

from decision_flywheel.reviewer_store import Article, ReviewStore


def test_experiment_preflight_counts_individual_trials_without_constructing_provider_clients(tmp_path, monkeypatch, capsys):
    snapshot = tmp_path / "snapshot.sqlite"
    with ReviewStore(snapshot, study_seed="fixture", rolling_audit_rate=0., final_audit_rate=0.) as store:
        articles = [Article(str(i), f"Title {i}", f"Abstract {i}", "2026-10-05", ("cs.AI",)) for i in range(20)]
        store.import_articles(articles)
        for i, article in enumerate(articles):
            store.record_vote(article.id, "include" if i % 2 else "exclude")
    spec = importlib.util.spec_from_file_location("controls_script", Path(__file__).parents[1] / "scripts/experiment_arxiv_controls.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not construct paid providers")
    monkeypatch.setattr(module.JevAdapter, "from_environment", forbidden)
    monkeypatch.setattr(module.OpenAIOptimizer, "from_environment", forbidden)
    output = tmp_path / "output"
    assert module.main(["--snapshot", str(snapshot), "--output", str(output), "--max-feature-trials", "3"]) == 0
    protocol = json.loads(capsys.readouterr().out)
    assert protocol["feature_trial_ceiling"] == 3
    assert protocol["trial_upper_bound"] == 5
    assert not output.exists()
