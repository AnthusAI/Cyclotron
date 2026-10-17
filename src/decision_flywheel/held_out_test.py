"""A frozen held-out set measures what a cyclotron learned: first classifier versus active classifier."""
import asyncio

import jsonschema
import pytest

from .batched_classification import BatchedAnswers
from .embedded_cyclotron import Cyclotron, CyclotronDefinition
from .embedded_cyclotron_test import RELEVANT, item
from .learning_loop import review_role
from .held_out import (Endpoint, HeldOutItem, HeldOutSet, build_report, load_report_schema, sign_test_p_value,
                       weighted_metrics)
from .models import DecisionResult
from .optimizer_agent import OptimizerAgent, OptimizerReply

LABELS = ("include", "exclude")
LEARNED_RUBRIC = "Include items that say yes."


class RubricReadingModel:
    """Says exclude to everything until its rubric tells it what a yes item looks like."""
    model_identity = "rubric-reading"

    def __init__(self):
        self.calls = 0

    async def classify_many(self, configs, target, training, **kwargs):
        self.calls += 1
        answers = {}
        for classifier_id, config in configs.items():
            says_yes = "yes" in target.values["text"]
            include_probability = (.9 if says_yes else .1) if config.rubric == LEARNED_RUBRIC else .3
            answers[classifier_id] = {"decision": DecisionResult(
                "include" if include_probability > .5 else "exclude",
                {"include": include_probability, "exclude": 1 - include_probability})}
        return BatchedAnswers(answers, "rubric-reading", {"prompt_tokens": 10, "completion_tokens": 2}, 1)


def rubric_optimizer():
    return OptimizerAgent(lambda _: OptimizerReply(
        '{"rationale":"Editors include items that say yes","rubric":"' + LEARNED_RUBRIC + '"}', "fake-optimizer"))


def learning_definition():
    return CyclotronDefinition("papyrus-relevance", (RELEVANT,), seed="held-out-seed", optimize_every=1000,
                               rubric_changes_every=1)


def run(coroutine):
    return asyncio.run(coroutine)


def decide_and_review(cyclotron, count, *, prefix="learn", selected_by=None, audit_only=False):
    reviewed, index = 0, 0
    while reviewed < count:
        identifier, says_yes, index = f"{prefix}-{index}", index % 2 == 1, index + 1
        if audit_only and review_role(cyclotron.definition.seed, identifier) != "scoreboard":
            continue
        decision = run(cyclotron.decide(item(identifier, ("yes " if says_yes else "no ") + identifier)))
        run(cyclotron.review(decision.decision_id, "include" if says_yes else "exclude", selected_by=selected_by))
        reviewed += 1


def test_a_learned_rubric_shows_as_a_clear_improvement_on_the_frozen_held_out_set(tmp_path):
    model = RubricReadingModel()
    with Cyclotron.open(tmp_path / "c", learning_definition(), model, rubric_optimizer(), max_requests=2000) as cyclotron:
        decide_and_review(cyclotron, 16)
        assert cyclotron.status().version >= 2
        decide_and_review(cyclotron, 24, prefix="audit", audit_only=True)
        summary = cyclotron.freeze_held_out_set()
        training_ids = {row.item.id for row in cyclotron._partitions("relevant")[0] + cyclotron._partitions("relevant")[1]}
        frozen_ids = {row.item_id for row in cyclotron._held_out_set("relevant", None).items}
        report = run(cyclotron.evaluate_held_out(minimum_per_label=5))
        calls_after_first_evaluation = model.calls
        repeated = run(cyclotron.evaluate_held_out(minimum_per_label=5))
        assert cyclotron.held_out_reports()[-1] == repeated
    jsonschema.validate(report, load_report_schema())
    assert not frozen_ids & training_ids
    assert summary["items"] == len(frozen_ids) and summary["selection"].startswith("Reviews the cyclotron selected")
    assert report["before"]["version"] == 1 and report["after"]["version"] >= 2
    assert report["headline"]["before"] == pytest.approx(.5)
    assert report["headline"]["after"] == pytest.approx(1.)
    assert report["change"]["lost"] == 0 and report["change"]["gained"] == summary["labelCounts"]["include"]
    assert report["change"]["verdict"] == "improved"
    assert report["cost"]["newRequests"] + report["cost"]["cachedRequests"] == 2 * summary["items"]
    assert repeated["cost"]["newRequests"] == 0 and model.calls == calls_after_first_evaluation
    assert repeated["change"] == report["change"]


def test_before_learning_there_is_nothing_to_compare(tmp_path):
    with Cyclotron.open(tmp_path / "c", learning_definition(), RubricReadingModel(), max_requests=500) as cyclotron:
        decide_and_review(cyclotron, 30)
        cyclotron.freeze_held_out_set()
        report = run(cyclotron.evaluate_held_out())
    assert report["before"]["fingerprint"] == report["after"]["fingerprint"]
    assert report["change"]["verdict"] == "nothing-learned-yet"
    assert report["headline"]["changePoints"] == 0


def test_a_frozen_set_never_changes_and_freezing_again_adds_a_newer_set(tmp_path):
    with Cyclotron.open(tmp_path / "c", learning_definition(), RubricReadingModel(), max_requests=500) as cyclotron:
        with pytest.raises(ValueError, match="freeze a held-out set first"):
            run(cyclotron.evaluate_held_out())
        decide_and_review(cyclotron, 30)
        first = cyclotron.freeze_held_out_set()
        assert cyclotron.freeze_held_out_set() == first
        decide_and_review(cyclotron, 30, prefix="later")
        second = cyclotron.freeze_held_out_set()
        assert [row["id"] for row in cyclotron.held_out_sets()] == [first["id"], second["id"]]
        assert second["items"] > first["items"]
        assert cyclotron.held_out_request_bound(set_id=first["id"]) == 2 * first["items"]


def test_reviews_a_reviewer_chose_to_make_never_enter_a_held_out_set(tmp_path):
    with Cyclotron.open(tmp_path / "c", learning_definition(), RubricReadingModel(), max_requests=500) as cyclotron:
        decide_and_review(cyclotron, 30, selected_by="reviewer")
        with pytest.raises(ValueError, match="at least one labeled audit item"):
            cyclotron.freeze_held_out_set()


def test_an_evaluation_that_could_exceed_the_request_ceiling_is_refused_before_any_call(tmp_path):
    with Cyclotron.open(tmp_path / "c", learning_definition(), RubricReadingModel(), max_requests=500) as cyclotron:
        decide_and_review(cyclotron, 30)
        cyclotron.freeze_held_out_set()
    model = RubricReadingModel()
    with Cyclotron.open(tmp_path / "c", learning_definition(), model, max_requests=1) as cyclotron:
        with pytest.raises(ValueError, match="decision-model requests"):
            run(cyclotron.evaluate_held_out())
    assert model.calls == 0


def test_the_cloud_store_keeps_held_out_sets_and_reports_across_reopen():
    from .s3_embedded_storage import S3CyclotronStores
    from .s3_journal_test import ConditionalS3, journal
    client = ConditionalS3()

    def cloud_stores():
        return S3CyclotronStores(journal(client, name="held-out-root"), max_records=10000,
                                 journal_for=lambda domain, name: journal(client, name=domain + "-" + name))
    with Cyclotron.open(None, learning_definition(), RubricReadingModel(), storage=cloud_stores(),
                        max_requests=500) as cyclotron:
        decide_and_review(cyclotron, 3, audit_only=True)
        frozen = cyclotron.freeze_held_out_set()
        report = run(cyclotron.evaluate_held_out())
    with Cyclotron.open(None, learning_definition(), RubricReadingModel(), storage=cloud_stores()) as reopened:
        assert reopened.held_out_sets() == [frozen]
        assert reopened.held_out_reports() == [report]


def held_out_item(identifier, label, propensity=1.):
    return HeldOutItem(identifier, {"text": identifier}, label, propensity, "program")


def test_each_item_counts_with_the_inverse_of_its_selection_probability():
    items = [held_out_item("a", "include"), held_out_item("b", "exclude", .25), held_out_item("c", "exclude")]
    metrics = weighted_metrics(LABELS, "include", items, {"a": "include", "b": "include", "c": "exclude"})
    assert metrics["accuracy"] == pytest.approx(2 / 6)
    assert metrics["recallByLabel"] == {"include": 1., "exclude": pytest.approx(1 / 5)}
    assert metrics["balancedAccuracy"] == pytest.approx(.6)
    assert metrics["precision"] == pytest.approx(1 / 5)


def test_the_sign_test_is_exact_and_two_sided():
    assert sign_test_p_value(0, 0) == 1.
    assert sign_test_p_value(5, 0) == pytest.approx(2 / 32)
    assert sign_test_p_value(6, 0) == pytest.approx(2 / 64)
    assert sign_test_p_value(3, 3) == 1.


@pytest.mark.parametrize("include_count,expected_verdict", [(3, "too-few-labels"), (12, "improved")])
def test_a_claim_needs_enough_labels_of_each_kind(include_count, expected_verdict):
    items = ([held_out_item(f"in-{index}", "include") for index in range(include_count)]
             + [held_out_item(f"out-{index}", "exclude") for index in range(12)])
    held_out_set = HeldOutSet.freeze("papyrus-relevance", "relevant", items)
    before = Endpoint(1, 0, "first", "first", {row.item_id: "exclude" for row in items})
    after = Endpoint(2, 0, "active", "active", {row.item_id: row.label for row in items})
    report = build_report(held_out_set, labels=LABELS, positive_label="include", before=before, after=after)
    jsonschema.validate(report, load_report_schema())
    assert report["change"]["verdict"] == expected_verdict
    assert report["headline"]["changePoints"] == 50.


def test_items_used_for_training_since_freezing_are_left_out_and_counted():
    items = [held_out_item("a", "include"), held_out_item("b", "exclude")]
    held_out_set = HeldOutSet.freeze("papyrus-relevance", "relevant", items)
    predictions = {"a": "include", "b": "exclude"}
    report = build_report(held_out_set, labels=LABELS, positive_label="include",
                          before=Endpoint(1, 0, "x", "x", predictions), after=Endpoint(2, 0, "y", "y", predictions),
                          excluded_item_ids=["b"])
    assert report["heldOutSet"]["excludedSinceFrozen"] == 1 and [row["itemId"] for row in report["items"]] == ["a"]


def test_the_shared_graphql_operations_freeze_then_evaluate_only_with_live_authority_and_a_confirmed_bound(tmp_path):
    from fastapi.testclient import TestClient
    from .held_out_service import HeldOutService
    from .web_api import create_app
    from .web_store import WebStore
    model = RubricReadingModel()
    with Cyclotron.open(tmp_path / "c", learning_definition(), model, max_requests=500) as cyclotron:
        decide_and_review(cyclotron, 4, audit_only=True)

    def open_authorized_cyclotron(cyclotron_id, max_requests=None):
        if cyclotron_id != "papyrus-relevance":
            raise PermissionError("not this caller's cyclotron")
        return Cyclotron.open(tmp_path / "c", learning_definition(), model, max_requests=max_requests or 500)

    def graphql_client(allow_live):
        client = TestClient(create_app(WebStore(tmp_path / "db"),
                                       held_out=HeldOutService(open_authorized_cyclotron, allow_live=allow_live)))

        def call(query, **variables):
            return client.post("/graphql", json={"query": query, "variables": variables}).json()
        return call
    overview_query = 'query($id:ID!){heldOutOverview(cyclotronId:$id)}'
    command_mutation = 'mutation($action:String!,$payload:JSON!){heldOutCommand(action:$action,payload:$payload)}'
    read_only = graphql_client(allow_live=False)
    assert read_only(overview_query, id="papyrus-relevance")["data"]["heldOutOverview"]["sets"] == []
    frozen = read_only(command_mutation, action="freeze",
                       payload={"cyclotronId": "papyrus-relevance"})["data"]["heldOutCommand"]
    overview = read_only(overview_query, id="papyrus-relevance")["data"]["heldOutOverview"]
    assert overview["sets"] == [frozen] and overview["requestBound"] == 2 * frozen["items"]
    evaluate = {"cyclotronId": "papyrus-relevance", "confirmedRequestBound": overview["requestBound"]}
    calls_before = model.calls
    refused = read_only(command_mutation, action="evaluate", payload=evaluate)
    assert "live authority" in refused["errors"][0]["message"] and model.calls == calls_before
    live = graphql_client(allow_live=True)
    unconfirmed = live(command_mutation, action="evaluate", payload={**evaluate, "confirmedRequestBound": 1})
    assert "confirm up to" in unconfirmed["errors"][0]["message"] and model.calls == calls_before
    report = live(command_mutation, action="evaluate", payload=evaluate)["data"]["heldOutCommand"]
    assert report["change"]["verdict"] == "nothing-learned-yet"
    assert live(overview_query, id="papyrus-relevance")["data"]["heldOutOverview"]["latestReport"] == report
    assert "not this caller's cyclotron" in read_only(overview_query, id="another-publication")["errors"][0]["message"]


def test_the_example_report_matches_the_schema_and_the_typescript_type_names_every_field():
    import json
    from pathlib import Path
    schemas = Path(__file__).with_name("schemas")
    jsonschema.validate(json.loads((schemas / "cyclotron-held-out-report.v1.example.json").read_text()),
                        load_report_schema())
    typescript_source = (Path(__file__).resolve().parents[2] / "trace-ui" / "src" / "heldOutReport.ts").read_text()

    def field_names(schema):
        for key, child in schema.get("properties", {}).items():
            yield key
            yield from field_names(child)
            yield from field_names(child.get("items", {}))
    missing = sorted({name for name in field_names(load_report_schema())
                      if f"{name}:" not in typescript_source and f"{name}?:" not in typescript_source})
    assert not missing, f"trace-ui/src/heldOutReport.ts lacks {missing}"
