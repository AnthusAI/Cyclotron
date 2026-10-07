"""The article demo delegates to the reusable core rather than implementing a loop."""
import asyncio

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel, development_assignment
from .flywheel_test import FakeModel, agent
from .reviewer_core import reviewer_task
from .reviewer_flywheel import ReviewerFlywheel
from .reviewer_store import Article, ReviewStore


def test_protected_vote_does_not_advance_non_rubric_review_cadence():
    from types import SimpleNamespace
    checks=[]
    events=[{'kind':'human-feedback','action':'submitted','assignment':'train','cycle_id':'one'},
            {'kind':'human-feedback','action':'submitted','assignment':'rolling_audit','cycle_id':'two'}]
    reviewer=SimpleNamespace(stage='examples',core=SimpleNamespace(history=lambda _:events),
        current_cycle=SimpleNamespace(check_trigger=lambda stage,**check:checks.append(check)))
    assert not ReviewerFlywheel.feedback_trigger(reviewer,1)
    assert checks[0]['details']['feedback_count']==1


def test_reviewer_transition_trigger_survives_restart_and_records_its_cause(tmp_path):
    path=tmp_path/'runtime.sqlite'
    with ReviewStore(tmp_path/'reviews.sqlite',study_seed='transitions') as store:
        articles=tuple(Article(str(i),'Paper',f'Text {i}','2026-10-06',('cs.AI',)) for i in range(3))
        store.import_articles(articles)
        core=DecisionFlywheel(path,ClassifierConfig(reviewer_task()),FakeModel(),agent([]))
        reviewer=ReviewerFlywheel(store,core)
        for article,label in zip(articles[:2],('exclude','include')):
            reviewer.predict(article)
            reviewer.record_review_event(store.record_vote(article.id,label))
            assert not reviewer.feedback_trigger(10)
            reviewer.finish_cycle()
        core.close()
        core=DecisionFlywheel(path,ClassifierConfig(reviewer_task()),FakeModel(),agent([]))
        reviewer=ReviewerFlywheel(store,core)
        reviewer.predict(articles[2])
        reviewer.record_review_event(store.record_vote(articles[2].id,'exclude'))
        assert reviewer.feedback_trigger(10)
        check=core.history()[-1]
        assert check['kind']=='trigger-evaluated' and check['stage']=='rubric'
        assert check['details']['transition_count']==2
        assert not reviewer.feedback_trigger(10)
        reviewer.finish_cycle()
        core.close()


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


def test_web_guidance_can_exclude_protected_comments_as_well_as_protected_labels(tmp_path):
    store = ReviewStore(tmp_path / 'reviews.sqlite',study_seed='fixture')
    articles = tuple(Article(str(i),'Paper',f'Text {i}','2026-10-06',('cs.AI',)) for i in range(40))
    store.import_articles(articles)
    for article in articles:
        store.record_vote(article.id,'include',comment=f'Explanation {article.id}')
    core = DecisionFlywheel(tmp_path / 'core.sqlite',ClassifierConfig(reviewer_task()),FakeModel(),agent([]))
    reviewer = ReviewerFlywheel(store,core,include_protected_guidance=False)
    training, development, protected = reviewer.partitions()
    context = reviewer.sync_optimizer_context()
    assert set(context['human_explanations']) == {f'Explanation {row.item.id}' for row in training}
    assert not context['evaluation_context_exposed']
    core.close()
    store.close()


def test_reviewer_passes_all_active_explanations_without_protected_item_text_or_labels(tmp_path):
    import json
    from .optimizer_agent import OptimizerAgent, OptimizerReply
    store = ReviewStore(tmp_path / "reviews.sqlite", study_seed="fixture")
    articles = tuple(Article(str(i), f"Paper {i}", f"Unique text {i}", "2026-10-05", ("cs.AI",)) for i in range(40))
    store.import_articles(articles)
    for article in articles:
        store.record_vote(article.id, "include" if int(article.id) % 2 else "exclude", comment=f"Preference {article.id}")
    prompts = []
    def complete(messages):
        prompts.append(json.loads(messages[-1]["content"]))
        return OptimizerReply('{"rationale":"Use explanations","rubric":"Updated preferences"}', "fake")
    core = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(reviewer_task()), FakeModel(), OptimizerAgent(complete))
    reviewer = ReviewerFlywheel(store, core)
    result = reviewer.improve()
    assert len(prompts[0]["human_explanations"]) == 40
    _, development, protected = reviewer.partitions()
    excluded_ids = {r.item.id for r in development} | {i.id for i in protected}
    assert not excluded_ids & {r["id"] for r in prompts[0]["feedback"]}
    assert all(not any(row["values"]["text"].endswith("Abstract: " + article.abstract)
                       for row in prompts[0]["feedback"])
               for article in articles if article.id in excluded_ids)
    assert not result["evaluation_independent_of_optimizer_context"]
    store.undo_last_vote()
    reviewer.sync_optimizer_context()
    assert "Preference 39" not in core.optimizer_context["human_explanations"]
    assert core.optimizer_context["evaluation_context_exposed"]
    core.close()
    store.close()


def test_reviewer_predictions_identify_the_core_version_without_a_local_replacement_head(tmp_path):
    store = ReviewStore(tmp_path / "reviews.sqlite", study_seed="fixture")
    article = Article("one", "Paper", "Text", "2026-10-05", ("cs.AI",))
    store.import_articles((article,))
    core = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(reviewer_task()), FakeModel(), agent([]))
    reviewer = ReviewerFlywheel(store, core)
    result = reviewer.predict(article)
    assert result.kind == "decision:flywheel-warmup"
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
        events = core.history(10000)
        assert events[0]['kind'] == 'cycle-started'
        assert events[-1]['kind'] == 'cycle-completed'
        assert events[0]['cycle_item_id'] is None
        assert all(e.get('cycle_id') == events[0]['cycle_id'] for e in events)
        assert result["stage"] == "questions"
        assert result["error_type"] == "ValueError"
        assert "private-provider-secret" not in str(core.history())
        assert core.history()[-2]["kind"] == "optimization-stage-failed"
        core.close()
