"""The workspace application composes its reviewer adapter outside WebWorker."""
import ast
from pathlib import Path

from .application_runtime import open_live_review_session
from .flywheel_test import FakeModel, agent
from .reviewer_store import Article


def test_a_live_review_session_is_composed_without_a_web_worker(tmp_path):
    article = Article("paper", "Title", "Abstract", "2026-10-07", ("cs.AI",))
    session = open_live_review_session(
        tmp_path / "run",
        {"seed": "fixture", "max_requests": 10,
         "selection_policy": {"primary": "f1", "positive_class": "include"}},
        (article,),
        model_factory=lambda _config: (FakeModel(), agent([])),
        event_sink=lambda _event: None,
    )
    assert session.reviewer.store.article("paper") == article
    assert session.reviewer.core.max_requests == 10
    session.close()


def test_closing_a_live_review_session_closes_the_core_and_review_store(tmp_path):
    session = open_live_review_session(
        tmp_path / "run", {"seed": "fixture", "max_requests": 10,
                             "selection_policy": {"primary": "f1", "positive_class": "include"}}, (),
        model_factory=lambda _config: (FakeModel(), agent([])), event_sink=lambda _event: None,
    )
    session.close()
    assert session.closed


def test_an_article_runtime_normalizes_web_run_configuration_without_a_worker(tmp_path):
    from .application_runtime import ArticleReviewRuntime
    runtime = ArticleReviewRuntime(tmp_path, sink_factory=lambda _run_id: lambda _event: None,
                                   model_factory=lambda _config: (FakeModel(), agent([])))
    config = runtime.normalize_run_config({}, ())
    assert config["max_requests"] == 500
    assert config["selection_policy"]["positive_class"] == "include"


def test_a_web_worker_can_use_an_injected_runtime_without_importing_article_types(tmp_path):
    from .web_store import WebStore
    from .web_worker import WebWorker

    class Runtime:
        def __init__(self):
            self.calls = []

        def normalize_run_config(self, config, items):
            self.calls.append((dict(config), tuple(items)))
            return {"runtime": "fake"}

        def open_session(self, run_id, config, items, current):  # pragma: no cover - no command is run here
            raise AssertionError("session should not open while creating a run")

    runtime = Runtime()
    worker = WebWorker(WebStore(tmp_path / "web.sqlite"), tmp_path / "runs", runtime=runtime, allow_live=True)
    run = worker.create_run("A generic run", {"caller": "value"})
    assert runtime.calls == [({"caller": "value"}, ())]
    assert run["config"] == {"runtime": "fake"}


def test_the_web_worker_exposes_the_runtime_seam_even_while_the_legacy_adapter_remains():
    source = Path(__file__).with_name("web_worker.py").read_text()
    imports = [node.module for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.ImportFrom) and node.module]
    # The cyclotron workspace still uses its established adapter during the
    # migration, but applications can now inject their own runtime without
    # reaching into the worker's reviewer implementation.
    assert "web_store" not in imports


def test_an_application_session_aborts_its_incomplete_cycle_without_exposing_core_cleanup(tmp_path):
    session = open_live_review_session(
        tmp_path / "run", {"seed": "fixture", "max_requests": 10,
                             "selection_policy": {"primary": "f1", "positive_class": "include"}}, (),
        model_factory=lambda _config: (FakeModel(), agent([])), event_sink=lambda _event: None,
    )
    session.abort(RuntimeError("interrupted"))
    assert session.current_cycle is None
    session.close()


def test_an_article_runtime_prepares_an_item_without_a_web_worker(tmp_path):
    from .application_runtime import ArticleReviewRuntime
    runtime = ArticleReviewRuntime(tmp_path, sink_factory=lambda _run_id: lambda _event: None,
                                   model_factory=lambda _config: (FakeModel(), agent([])))
    article = {"id": "paper", "title": "Title", "abstract": "Abstract", "submitted_at": "2026-10-07",
               "categories": ("cs.AI",)}
    config = runtime.normalize_run_config({}, (article,))
    session = runtime.open_session("run", config, (article,), None)
    command = runtime.execute(session, "prepare", {}, None, config)
    assert command.result["prediction"]["label"] == "exclude"
    assert command.updates[0].item_id == "paper"
    assert command.updates[0].prediction["presentation_id"]
    session.close()
