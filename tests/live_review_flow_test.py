"""Complete article UI -> optimizer -> Jev -> fitted head, using only fake SDKs."""
from io import StringIO
import json
from types import SimpleNamespace

from rich.console import Console

from decision_flywheel.adapters.jev import JevAdapter
from decision_flywheel.adapters.openai_optimizer import OpenAIOptimizer
from decision_flywheel.classifier_config import ClassifierConfig
from decision_flywheel.flywheel import DecisionFlywheel
from decision_flywheel.optimizer_agent import OptimizerAgent
from decision_flywheel.reviewer import run_review_session
from decision_flywheel.reviewer_core import reviewer_task
from decision_flywheel.reviewer_flywheel import ReviewerFlywheel
from decision_flywheel.reviewer_store import Article, ReviewStore


def test_the_article_ui_displays_real_optimizer_and_jev_messages_and_serves_the_retrained_head(tmp_path, monkeypatch):
    optimizer_calls, jev_calls = [], []
    def complete(**request):
        optimizer_calls.append(request)
        briefing = json.loads(request["messages"][1]["content"])
        proposal = {"rationale": "The human prefers practical papers", "rubric": "Practical papers",
                    "example_ids": [row["id"] for row in briefing["feedback"][:2]],
                    "tasks": [{"name": "practical", "instructions": "Is this practical?", "labels": ["yes", "no"]}]}
        control = briefing["current"]["control_under_test"]
        proposal = {"rationale": proposal["rationale"], control: proposal[control]}
        message = SimpleNamespace(content=json.dumps(proposal), tool_calls=None)
        return SimpleNamespace(model="fake-optimizer", usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 15}),
                               choices=[SimpleNamespace(message=message)])
    optimizer_sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    class Jev:
        def system_one(self, *, state, questions):
            jev_calls.append({"state": state, "questions": questions})
            positive = "Title: yes" in state["target"]["text"]
            answers = {"decision": {"choice": "exclude", "probabilities": {"include": .2, "exclude": .8}}}
            if state["rubric"]:
                # Leave room for a learned head to improve probability calibration.
                p = .65 if positive else .35
                answers["decision"] = {"choice": "include" if positive else "exclude", "probabilities": {"include": p, "exclude": 1-p}}
            if "practical" in questions:
                p = .99 if positive else .01
                answers["practical"] = {"choice": "yes" if positive else "no", "probabilities": {"yes": p, "no": 1-p}}
            return SimpleNamespace(answers=answers, model="fake-jev", usage={"input_tokens": 12})
    choices = iter(["i", "Practical and useful", "o", "", "j", "", "f", "", "q"])
    monkeypatch.setattr("decision_flywheel.reviewer.Prompt.ask", lambda *a, **k: next(choices))
    path = tmp_path / "runtime.sqlite"
    with ReviewStore(tmp_path / "review.sqlite", study_seed="integration") as store:
        articles = tuple(Article(f"a{i:03d}", f"{'yes' if i % 2 else 'no'} paper {i}", f"Abstract {i}",
                                 "2026-10-05", ("cs.AI",), "Author") for i in range(60))
        store.import_articles(articles)
        for i, article in enumerate(articles[:-2]):
            store.record_vote(article.id, "include" if i % 2 else "exclude", comment="Practical" if i % 2 else "Not useful")
        wheel = DecisionFlywheel(path, ClassifierConfig(reviewer_task()), JevAdapter(Jev()),
            OptimizerAgent(OpenAIOptimizer(optimizer_sdk, model="fake", max_calls=3)), max_requests=150)
        output = StringIO()
        # This flow spec deliberately exercises immediate vote-cadence optimization.
        client = ReviewerFlywheel(store, wheel, min_stage_evaluation_per_class=2, rubric_changes_every=None)
        run_review_session(store, Console(file=output, width=120), flywheel=client, optimize_every=1)
        assert wheel.active.head is not None
        assert store.current_label(articles[-2].id) == "include"
        shown = store.presentations_for(articles[-2].id)[-1]
        assert shown.predictor_kind == "jev:flywheel-warmup"
        assert store.events_for(articles[-2].id)[0].comment == "Practical and useful"
        transcript = output.getvalue()
        assert "The human prefers practical papers" in transcript
        assert '"criteria"' in transcript
        assert "decision/include" in transcript
        events = wheel.history(10000)
        first_prediction = next(e for e in events if e['kind'] == 'prediction')
        first_feedback = next(e for e in events if e['kind'] == 'human-feedback')
        first_optimizer = next(e for e in events if e['kind'] == 'optimizer-request')
        assert first_prediction['event_id'] < first_feedback['event_id'] < first_optimizer['event_id']
        assert first_prediction['cycle_id'] == first_optimizer['cycle_id']
        assert {e["kind"] for e in events} >= {"fit-started", "fit-completed", "promoted", "decision-request", "decision-response"}
        assert len(optimizer_calls) == 1
        protected_ids = {item.id for item in client.partitions()[2]}
        briefing_ids = {row["id"] for row in json.loads(optimizer_calls[0]["messages"][1]["content"])["feedback"]}
        assert not briefing_ids & protected_ids
        version = wheel.active.fingerprint
        wheel.close()
        wheel = DecisionFlywheel(path, ClassifierConfig(reviewer_task()), JevAdapter(Jev()),
            OptimizerAgent(OpenAIOptimizer(optimizer_sdk, model="fake", max_calls=3)), max_requests=150)
        assert wheel.active.fingerprint == version
        assert wheel.active.head is not None
        assert any(event["kind"] == "optimizer-response" for event in wheel.history(10000))
        wheel.close()
