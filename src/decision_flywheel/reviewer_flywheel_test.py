"""The article demo delegates to the reusable core rather than implementing a loop."""
import asyncio

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel, development_assignment
from .flywheel_test import FakeModel, agent
from .reviewer_core import reviewer_task
from .reviewer_flywheel import ReviewerFlywheel
from .reviewer_store import Article, ReviewStore


def test_reviewer_feedback_is_partitioned_without_exposing_audit_votes(tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite", study_seed="fixture")
    articles = tuple(Article(str(i), f"Paper {i}", f"Text {i}", "2026-10-05", ("cs.AI",)) for i in range(40))
    store.import_articles(articles)
    for article in articles:
        store.record_vote(article.id, "include" if int(article.id) % 2 else "exclude", comment="My explanation")
    core = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(reviewer_task()), FakeModel(), agent([]))
    reviewer = ReviewerFlywheel(store, core)
    training, development, protected = reviewer.partitions()
    assert training and development and protected
    assert all(store.assignment_for(row.item.id) == "train" for row in (*training, *development))
    assert all(not development_assignment("fixture", row.item.id) for row in training)
    assert all(development_assignment("fixture", row.item.id) for row in development)
    assert not {row.item.id for row in (*training, *development)} & {item.id for item in protected}
    assert all(row.context["human_feedback"] == "My explanation" for row in training)
    core.close()
    store.close()


def test_reviewer_predictions_identify_the_core_version_without_a_local_replacement_head(tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite", study_seed="fixture")
    article = Article("one", "Paper", "Text", "2026-10-05", ("cs.AI",))
    store.import_articles((article,))
    core = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(reviewer_task()), FakeModel(), agent([]))
    reviewer = ReviewerFlywheel(store, core)
    result = reviewer.predict(article)
    assert result.kind == "jev:flywheel-warmup"
    assert result.fingerprint == core.active.fingerprint
    assert result.label == "exclude"
    assert reviewer.status()["fitted_head"] is False
    assert reviewer.history()[-1]["kind"] == "prediction"
    core.close()
    store.close()


def test_active_configuration_inspection_shows_the_exact_question_not_just_its_name(tmp_path):
    from .models import DecisionTask
    config = ClassifierConfig(reviewer_task(), tasks=(DecisionTask(
        "about_knowledge_bases", ("yes", "no"), "Is this paper about knowledge bases?"),))
    with ReviewStore(tmp_path / "reviews.sqlite", study_seed="fixture") as store:
        core = DecisionFlywheel(tmp_path / "wheel.sqlite", config, FakeModel(), agent([]))
        status = ReviewerFlywheel(store, core).status()
        definition = status["task_definitions"][0]
        assert definition["instructions"] == "Is this paper about knowledge bases?"
        assert definition["labels"] == ["yes", "no"]
        assert status["main_decision"]["instructions"] == reviewer_task().instructions
        core.close()


def test_a_stage_failure_is_visible_without_crashing_the_reviewer_or_echoing_exception_secrets(tmp_path):
    with ReviewStore(tmp_path / "reviews.sqlite", study_seed="fixture") as store:
        core = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(reviewer_task()), FakeModel(), agent([]))
        async def fail(*args, **kwargs):
            raise ValueError("private-provider-secret")
        core.optimize_stage = fail
        result = ReviewerFlywheel(store, core).improve(stage="questions")
        assert result["stage"] == "questions"
        assert result["error_type"] == "ValueError"
        assert "private-provider-secret" not in str(core.history())
        assert core.history()[-1]["kind"] == "optimization-stage-failed"
        core.close()
