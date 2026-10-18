"""Curated examples prime context without pretending to be operational decisions."""

import asyncio

import pytest

from .cyclotron_status import load_schema
from .cyclotron_status_test import validate
from .embedded_cyclotron import Cyclotron
from .embedded_cyclotron_test import DEFINITION, Model, item
from .flywheel_test import agent
from .models import Item, LabeledItem


def seed():
    return {
        "relevant": [
            LabeledItem(
                Item("synthetic-prime", {"text": "Synthetic reviewed reference"}),
                "include",
                "trusted",
                {"human_feedback": "Synthetic explicit reason"},
            )
        ]
    }


def test_priming_context_is_persisted_training_only_and_used_before_first_decision(tmp_path):
    class Inspect(Model):
        async def classify_many(self, configs, target, training, **kwargs):
            assert [row.item.id for row in training["relevant"]] == ["synthetic-prime"]
            assert configs["relevant"].example_ids == ("synthetic-prime",)
            return await super().classify_many(configs, target, training, **kwargs)

    model = Inspect()
    c = Cyclotron.open(tmp_path / "engine", DEFINITION, model, agent([]), priming_examples=seed())
    try:
        assert c.status().alignment.labels == 0
        assert c.status().primed_examples == 1 and c.status().to_json()["cyclotron"]["primedExamples"] == 1
        validate(c.status().to_json(), load_schema())
        train, dev, protected = c._partitions("relevant")
        assert train[0].context["human_feedback"] == "Synthetic explicit reason"
        assert dev == protected == ()
        asyncio.run(c.decide(item("operational")))
        with pytest.raises(ValueError, match="priming"):
            asyncio.run(c.decide(item("synthetic-prime")))
        assert model.calls == 1 and c.labels() == []
    finally:
        c.close()
    reopened = Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([]))
    try:
        assert reopened._partitions("relevant")[0][0].item.id == "synthetic-prime"
    finally:
        reopened.close()


def test_reopening_cannot_silently_replace_accepted_priming(tmp_path):
    c = Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([]), priming_examples=seed())
    c.close()
    changed = {"relevant": [LabeledItem(Item("other", {"text": "Other"}), "include", "trusted", {"human_feedback": "Reason"})]}
    with pytest.raises(ValueError, match="priming"):
        Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([]), priming_examples=changed)


def test_priming_accepts_no_rationale_but_requires_known_label_and_keeps_global_context_validation(tmp_path):
    c = Cyclotron.open(
        tmp_path / "optional",
        DEFINITION,
        Model(),
        agent([]),
        priming_examples={"relevant": [LabeledItem(Item("p", {"text": "p"}), "include")]},
    )
    try:
        assert c._partitions("relevant")[0][0].context == {}
    finally:
        c.close()
    with pytest.raises(ValueError):
        Cyclotron.open(
            tmp_path / "bad-label",
            DEFINITION,
            Model(),
            agent([]),
            priming_examples={"relevant": [LabeledItem(Item("p", {"text": "p"}), "invented")]},
        )
    with pytest.raises(ValueError):
        LabeledItem(Item("p", {"text": "p"}), "include", context={"human_feedback": ""})


def test_workspace_run_pins_accepted_examples_separately_and_refuses_operational_overlap(tmp_path):
    import base64
    import json

    from .cyclotron_runtime import CyclotronRuntime
    from .priming import PrimingService
    from .web_store import WebStore
    from .workspace_session import WorkspaceSession

    store = WebStore(tmp_path / "web.db")
    store.save_classifier(
        "relevant", "Relevance", {"question": "Include?", "classes": [{"label": "include"}, {"label": "exclude"}]}
    )
    store.save_item_list("examples", "Examples")
    store.save_item_list("live", "Operational")
    row = {
        "id": "prime",
        "occurred_at": "2026-10-14",
        "values": {"text": "Synthetic accepted example"},
        "labels": {"relevant": {"label": "include", "rationale": "Synthetic explicit rationale"}},
    }
    service = PrimingService(store)
    draft = service.start_file(
        "draft", "examples", [{"id": "relevant", "revision": 1}], "json", base64.b64encode(json.dumps([row]).encode()).decode()
    )
    service.accept("draft", draft["version"], None)
    store.upsert_list_items(
        "live", [{"id": "operational", "occurred_at": "2026-10-14", "values": {"text": "Synthetic live item"}}]
    )
    runtime = CyclotronRuntime(store, tmp_path / "runs", model_factory=lambda *_: None, sink_factory=lambda *_: None)
    run = runtime.create_run("Primed", {"classifier_ids": ["relevant"], "item_list_id": "live", "priming_list_id": "examples"})
    session = WorkspaceSession(store, run, tmp_path / "session", Model(), agent([]), lambda _: None)
    try:
        assert [row.item.id for row in session.partitions("relevant")[0]] == ["prime"]
        assert session.wheels["relevant"].initial.example_ids == ("prime",)
        assert session.partitions("relevant")[1] == ()
    finally:
        session.close()
    store.upsert_list_items("live", [{"id": "prime", "occurred_at": "2026-10-14", "values": {"text": "Overlapping identity"}}])
    with pytest.raises(ValueError, match="disjoint"):
        runtime.create_run("Leak", {"classifier_ids": ["relevant"], "item_list_id": "live", "priming_list_id": "examples"})


def accepted(identifier, text, label, explanation):
    return {"relevant": [LabeledItem(Item(identifier, {"text": text}), label, "trusted", {"human_feedback": explanation})]}


class RecordsContext(Model):
    def __init__(self):
        super().__init__()
        self.example_lists = []
        self.training_ids = []

    async def classify_many(self, configs, target, training, **kwargs):
        self.example_lists.append(configs["relevant"].example_ids)
        self.training_ids.append([row.item.id for row in training["relevant"]])
        return await super().classify_many(configs, target, training, **kwargs)


def test_a_running_store_takes_a_new_primed_example_as_a_new_version_its_next_decision_uses(tmp_path):
    model = RecordsContext()
    with Cyclotron.open(tmp_path / "engine", DEFINITION, model, agent([])) as engine:
        first = asyncio.run(engine.decide(item("before")))
        assert first.version == 1 and engine.status().primed_examples == 0
        result = asyncio.run(
            engine.add_priming_examples(
                accepted("pasted-url", "Synthetic pasted source", "exclude", "Vendor marketing"), added_by="editor-1"
            )
        )
        assert result == {"added": {"relevant": ["pasted-url"]}, "primed_examples": {"relevant": 1}, "versions": {"relevant": 2}}
        status = engine.status().to_json()
        assert status["cyclotron"]["primedExamples"] == 1 and status["cyclotron"]["version"] == 2
        assert status["lastChange"]["summary"] == "Added 1 primed example to the example list."
        validate(status, load_schema())
        again = asyncio.run(engine.decide(item("before")))
        assert again.decision_id != first.decision_id and again.version == 2
        asyncio.run(engine.decide(item("after", "yes, a different source")))
        assert model.example_lists == [(), ("pasted-url",), ("pasted-url",)]
        assert model.training_ids[-1] == ["pasted-url"]
        assert engine._partitions("relevant")[0][0].context == {"human_feedback": "Vendor marketing"}
        assert engine.priming_additions()[0]["added_by"] == "editor-1"
        assert engine.priming_additions()[0]["examples"] == {"relevant": ["pasted-url"]}
        assert engine.labels() == [] and engine.status().alignment.labels == 0
    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([])) as reopened:
        assert reopened.wheels["relevant"].active.config.example_ids == ("pasted-url",)
        assert reopened.status().to_json()["cyclotron"]["version"] == 2


def test_adding_primed_examples_is_idempotent_and_refuses_changed_content_or_decided_items(tmp_path):
    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([]), priming_examples=seed()) as engine:
        asyncio.run(engine.decide(item("operational")))
        assert asyncio.run(engine.add_priming_examples(seed(), added_by="editor-1"))["added"] == {}
        assert engine.status().to_json()["cyclotron"]["version"] == 1 and engine.priming_additions() == []
        with pytest.raises(ValueError, match="different content"):
            asyncio.run(
                engine.add_priming_examples(accepted("synthetic-prime", "Changed text", "include", "Reason"), added_by="editor-1")
            )
        with pytest.raises(ValueError, match="disjoint"):
            asyncio.run(engine.add_priming_examples(accepted("operational", "yes", "include", "Reason"), added_by="editor-1"))
        with pytest.raises(ValueError, match="added_by"):
            asyncio.run(engine.add_priming_examples(accepted("new", "New", "include", "Reason"), added_by=" "))
        with pytest.raises(ValueError):
            asyncio.run(engine.add_priming_examples(accepted("new", "New", "invented", "Reason"), added_by="editor-1"))
        asyncio.run(engine.add_priming_examples(accepted("new", "New", "include", "Reason"), added_by="editor-1"))
        assert engine.wheels["relevant"].active.config.example_ids == ("synthetic-prime", "new")
        assert engine.status().primed_examples == 2


def test_reopening_with_the_original_priming_keeps_added_examples_and_does_not_reapply_a_swapped_one(tmp_path):
    from dataclasses import replace

    from .flywheel import FittedClassifier

    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([]), priming_examples=seed()) as engine:
        asyncio.run(engine.add_priming_examples(accepted("added", "Added", "exclude", "Reason"), added_by="editor-1"))
        wheel = engine.wheels["relevant"]
        wheel._activate(FittedClassifier(replace(wheel.active.config, example_ids=("synthetic-prime",))))
    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([]), priming_examples=seed()) as reopened:
        assert reopened.wheels["relevant"].active.config.example_ids == ("synthetic-prime",)
        assert [row.item.id for row in reopened._partitions("relevant")[0]] == ["synthetic-prime", "added"]
    changed = accepted("other", "Other", "include", "Reason")
    with pytest.raises(ValueError, match="add_priming_examples"):
        Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([]), priming_examples=changed)


def test_an_addition_interrupted_before_its_example_list_changed_completes_on_reopen(tmp_path):
    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([])) as engine:
        engine._apply_priming_additions = lambda: None
        asyncio.run(engine.add_priming_examples(accepted("pending", "Pending", "include", "Reason"), added_by="editor-1"))
        assert engine.wheels["relevant"].active.config.example_ids == ()
    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([])) as reopened:
        assert reopened.wheels["relevant"].active.config.example_ids == ("pending",)
        assert reopened.status().to_json()["cyclotron"]["version"] == 2


def test_a_retracted_primed_example_stops_reaching_the_prompt_as_training_and_the_retraction_is_recorded(tmp_path):
    model = RecordsContext()
    with Cyclotron.open(tmp_path / "engine", DEFINITION, model, agent([])) as engine:
        asyncio.run(
            engine.add_priming_examples(accepted("withdrawn-review", "Reviewed", "include", "Reason"), added_by="editor-1")
        )
        asyncio.run(engine.add_priming_examples(accepted("kept-review", "Kept", "exclude", "Other reason"), added_by="editor-1"))
        asyncio.run(engine.decide(item("before")))
        assert model.training_ids[-1] == ["withdrawn-review", "kept-review"]
        result = asyncio.run(engine.retract_priming_examples({"relevant": ["withdrawn-review"]}, retracted_by="editor-2"))
        assert result == {
            "retracted": {"relevant": ["withdrawn-review"]},
            "absent": {},
            "primed_examples": {"relevant": 1},
            "versions": {"relevant": 4},
        }
        assert engine.status().to_json()["lastChange"]["summary"] == "Retracted 1 primed example from the example list."
        validate(engine.status().to_json(), load_schema())
        asyncio.run(engine.decide(item("after", "a different source")))
        assert model.example_lists[-1] == ("kept-review",)
        assert model.training_ids[-1] == ["kept-review"]
        assert [row.item.id for row in engine._partitions("relevant")[0]] == ["kept-review"]
        assert [row.item.id for row in engine._labeled_partitions("relevant")[0]] == ["kept-review"]
        assert engine.wheels["relevant"].initial.example_ids == ("kept-review",)
        retraction = engine.priming_retractions()[0]
        assert (
            retraction["retracted_by"] == "editor-2"
            and retraction["at"]
            and retraction["examples"] == {"relevant": ["withdrawn-review"]}
        )
        first_addition = engine.priming_additions()[0]
        assert first_addition["examples"] == {"relevant": ["withdrawn-review"]}
        assert first_addition["retractions"][0]["retracted_by"] == "editor-2"
        assert engine.priming_additions()[1]["retractions"] == []


def test_retracting_an_absent_or_already_retracted_example_changes_nothing_and_says_so(tmp_path):
    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([]), priming_examples=seed()) as engine:
        first = asyncio.run(
            engine.retract_priming_examples({"relevant": ["synthetic-prime", "never-primed"]}, retracted_by="editor-1")
        )
        assert first["retracted"] == {"relevant": ["synthetic-prime"]} and first["absent"] == {"relevant": ["never-primed"]}
        version = engine.status().to_json()["cyclotron"]["version"]
        again = asyncio.run(engine.retract_priming_examples({"relevant": ["synthetic-prime"]}, retracted_by="editor-2"))
        assert again["retracted"] == {} and again["absent"] == {"relevant": ["synthetic-prime"]}
        assert engine.status().to_json()["cyclotron"]["version"] == version
        assert len(engine.priming_retractions()) == 1
        assert engine.wheels["relevant"].active.config.example_ids == ()
        with pytest.raises(ValueError, match="retracted_by"):
            asyncio.run(engine.retract_priming_examples({"relevant": ["x"]}, retracted_by=" "))
        with pytest.raises(ValueError, match="example identities"):
            asyncio.run(engine.retract_priming_examples({"relevant": [""]}, retracted_by="editor-1"))
        with pytest.raises(ValueError, match="list of example identities"):
            asyncio.run(engine.retract_priming_examples({"relevant": "synthetic-prime"}, retracted_by="editor-1"))


def test_a_retraction_survives_reopening_even_when_the_original_priming_is_passed_again(tmp_path):
    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([]), priming_examples=seed()) as engine:
        asyncio.run(engine.add_priming_examples(accepted("added", "Added", "exclude", "Reason"), added_by="editor-1"))
        asyncio.run(engine.retract_priming_examples({"relevant": ["synthetic-prime", "added"]}, retracted_by="editor-2"))
        version = engine.status().to_json()["cyclotron"]["version"]
    for requested in (None, seed()):
        with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([]), priming_examples=requested) as reopened:
            assert reopened.wheels["relevant"].active.config.example_ids == ()
            assert reopened._partitions("relevant")[0] == ()
            assert reopened.status().primed_examples == 0
            assert reopened.status().to_json()["cyclotron"]["version"] == version
            assert reopened.priming_retractions()[0]["retracted_by"] == "editor-2"
            assert reopened.priming_additions()[0]["retractions"][0]["examples"] == {"relevant": ["added"]}
            asyncio.run(reopened.decide(item("synthetic-prime")))


def test_a_retraction_interrupted_before_its_example_list_changed_completes_on_reopen(tmp_path):
    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([])) as engine:
        asyncio.run(engine.add_priming_examples(accepted("pending", "Pending", "include", "Reason"), added_by="editor-1"))
        engine._apply_priming_retractions = lambda: None
        asyncio.run(engine.retract_priming_examples({"relevant": ["pending"]}, retracted_by="editor-1"))
        assert engine.wheels["relevant"].active.config.example_ids == ("pending",)
    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([])) as reopened:
        assert reopened.wheels["relevant"].active.config.example_ids == ()
        assert reopened.status().to_json()["cyclotron"]["version"] == 3


def test_a_retracted_example_can_be_added_again_only_as_a_new_addition(tmp_path):
    model = RecordsContext()
    with Cyclotron.open(tmp_path / "engine", DEFINITION, model, agent([])) as engine:
        asyncio.run(engine.add_priming_examples(accepted("review", "First review", "include", "Reason"), added_by="editor-1"))
        asyncio.run(engine.retract_priming_examples({"relevant": ["review"]}, retracted_by="editor-2"))
        result = asyncio.run(
            engine.add_priming_examples(accepted("review", "Second review", "exclude", "New reason"), added_by="editor-3")
        )
        assert result["added"] == {"relevant": ["review"]} and result["primed_examples"] == {"relevant": 1}
        assert [addition["added_by"] for addition in engine.priming_additions()] == ["editor-1", "editor-3"]
        assert engine.priming_additions()[0]["retractions"][0]["retracted_by"] == "editor-2"
        assert engine.priming_additions()[1]["retractions"] == []
        asyncio.run(engine.decide(item("after")))
        assert model.example_lists[-1] == ("review",)
        assert engine._partitions("relevant")[0][0].label == "exclude"
        assert (
            asyncio.run(engine.add_priming_examples(accepted("review", "Second review", "exclude", "New reason"), added_by="x"))[
                "added"
            ]
            == {}
        )
        with pytest.raises(ValueError, match="different content"):
            asyncio.run(engine.add_priming_examples(accepted("review", "Third", "include", "R"), added_by="editor-1"))
    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([])) as reopened:
        assert reopened.wheels["relevant"].active.config.example_ids == ("review",)
        assert len(reopened.priming_retractions()) == 1


def test_a_retracted_identity_that_was_decided_meanwhile_cannot_be_added_again(tmp_path):
    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([]), priming_examples=seed()) as engine:
        asyncio.run(engine.retract_priming_examples({"relevant": ["synthetic-prime"]}, retracted_by="editor-1"))
        asyncio.run(engine.decide(item("synthetic-prime")))
        with pytest.raises(ValueError, match="disjoint"):
            asyncio.run(engine.add_priming_examples(seed(), added_by="editor-2"))


def test_retracting_for_an_unknown_classifier_is_refused_and_records_nothing(tmp_path):
    with Cyclotron.open(tmp_path / "engine", DEFINITION, Model(), agent([]), priming_examples=seed()) as engine:
        with pytest.raises(ValueError, match="known classifier"):
            asyncio.run(
                engine.retract_priming_examples({"relevant": ["synthetic-prime"], "unknown": ["x"]}, retracted_by="editor-1")
            )
        assert engine.priming_retractions() == [] and engine.status().primed_examples == 1
