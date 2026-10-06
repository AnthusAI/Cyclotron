"""Replay preflight freezes a private snapshot without touching live ratings or providers."""
import importlib.util
from pathlib import Path

from decision_flywheel.reviewer_store import Article, ReviewStore


def test_preflight_is_network_free_and_leaves_the_source_review_database_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "live.sqlite"
    with ReviewStore(path, study_seed="fixture", rolling_audit_rate=0., final_audit_rate=0.) as store:
        articles = [Article(str(i), f"Title {i}", f"Abstract {i}", "2026-10-05", ("cs.AI",)) for i in range(20)]
        store.import_articles(articles)
        for i, article in enumerate(articles):
            store.record_vote(article.id, "include" if i%2 else "exclude", comment=f"Comment {i}")
    original = path.read_bytes()
    spec = importlib.util.spec_from_file_location("replay_script", Path(__file__).parents[1] / "scripts/replay_arxiv_feedback.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not construct a live model client")
    monkeypatch.setattr(module.JevAdapter, "from_environment", forbidden)
    monkeypatch.setattr(module.OpenAIOptimizer, "from_environment", forbidden)
    monkeypatch.setattr(module, "GraphQLTraceSink", forbidden)
    assert module.main(["--database", str(path), "--output", str(tmp_path / "replay"),
        "--trace-api-url", "http://localhost/graphql", "--trace-api-run-id", "test"]) == 0
    assert path.read_bytes() == original
    assert (tmp_path / "replay/manifest.json").exists()
