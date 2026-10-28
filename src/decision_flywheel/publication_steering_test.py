"""Output-scoped commands use native guarded trials and existing catalog records."""

import asyncio

import pytest

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import DEV, TASK, TRAIN, FakeModel
from .publication_lab import PublicationActor, PublicationBinding, PublicationError
from .publication_records_test import setup_records
from .publication_steering import NativeOutputEvaluator, PublicationSteering

PROPOSAL = {
    "rubric": "Prioritize practical work",
    "example_ids": ["t0", "t1"],
    "tasks": [{"name": "practical", "instructions": "Is this practical?", "labels": ["yes", "no"]}],
}


def test_native_trial_and_reviewed_promotion_are_output_scoped_and_retry_safe(tmp_path):
    async def scenario():
        _, records, _, binding_ref = setup_records(tmp_path)
        # Reuse a real native wheel and explicit trusted offline train/dev fixtures.
        wheel = DecisionFlywheel(tmp_path / "wheel.sqlite3", ClassifierConfig(TASK), FakeModel(), None, max_requests=100)
        try:
            binding = PublicationBinding("publication-a", ("inclusion",), "binding-1")
            records.catalog.upsert_list_items(
                "bindings",
                [
                    {
                        "id": "publication-a",
                        "occurred_at": "2026-01-02T00:00:00Z",
                        "values": {
                            "accountId": "account-a",
                            "publicationId": "publication-a",
                            "revision": "binding-1",
                            "outputs": ["inclusion"],
                        },
                    }
                ],
            )
            binding_ref = records.reference("bindings", "publication-a", 2)
            actor = PublicationActor("account-a", "editor-a", frozenset({("publication-a", "read"), ("publication-a", "steer")}))
            evaluator = NativeOutputEvaluator(
                "inclusion",
                wheel,
                partitions=lambda: {
                    "training": TRAIN,
                    "development": DEV,
                    "protected": (),
                    "propensities": {row.item.id: 1.0 for row in TRAIN},
                },
            )
            gateway = PublicationSteering(records, {"inclusion": evaluator}, owner_lock=asyncio.Lock(), allow_evaluation=True)
            original = wheel.active.fingerprint
            payload = {
                "bindingRevision": "binding-1",
                "bindingRef": binding_ref.to_json(),
                "classifier": "inclusion",
                "requestId": "evaluate-1",
                "expectedFingerprint": original,
                "action": "evaluate",
                "proposal": PROPOSAL,
                "explanation": "Test a human-supplied practical-work rubric.",
            }
            trial = await gateway.command(actor, binding, payload)
            assert trial["result"]["improved"] is True and trial["result"]["promoted"] is False
            assert wheel.active.fingerprint == original
            assert await gateway.command(actor, binding, payload) == trial
            promote = {key: value for key, value in payload.items() if key != "proposal"}
            promote.update(
                action="promote",
                requestId="promote-1",
                trialFingerprint=trial["result"]["trial_fingerprint"],
                explanation="Reviewed the guarded development evidence.",
            )
            promoted = await gateway.command(actor, binding, promote)
            assert promoted["result"]["promoted"] is True
            assert wheel.active.config.rubric == "Prioritize practical work"
            assert await gateway.command(actor, binding, promote) == promoted
            with pytest.raises(PublicationError):
                await gateway.command(actor, binding, {**promote, "explanation": "Changed command body"})
            assert not any(row["kind"] == "review" for row in records.records())
        finally:
            wheel.close()

    asyncio.run(scenario())


def test_uncertain_steering_is_not_automatically_reexecuted_after_reconstruction(tmp_path):
    async def scenario():
        _, records, _, binding_ref = setup_records(tmp_path)

        class Uncertain:
            output_id = "intel"
            active_fingerprint = "active-1"
            calls = 0

            async def execute(self, action, payload):
                self.calls += 1
                raise RuntimeError("lost acknowledgement after dispatch")

        evaluator = Uncertain()
        actor = PublicationActor("account-a", "editor-a", frozenset({("publication-a", "read"), ("publication-a", "steer")}))
        binding = PublicationBinding("publication-a", ("intel",), "binding-1")
        payload = {
            "bindingRevision": "binding-1",
            "bindingRef": binding_ref.to_json(),
            "classifier": "intel",
            "requestId": "uncertain-1",
            "expectedFingerprint": "active-1",
            "action": "evaluate",
            "proposal": {"rubric": "Human draft"},
            "explanation": "Evaluate human draft.",
        }
        gateway = PublicationSteering(records, {"intel": evaluator}, owner_lock=asyncio.Lock(), allow_evaluation=True)
        with pytest.raises(PublicationError):
            await gateway.command(actor, binding, payload)
        recreated = PublicationSteering(records, {"intel": evaluator}, owner_lock=asyncio.Lock(), allow_evaluation=True)
        with pytest.raises(PublicationError, match="reconciliation"):
            await recreated.command(actor, binding, payload)
        assert evaluator.calls == 1

    asyncio.run(scenario())


def test_steering_scope_capability_and_active_revision_are_checked_before_execution(tmp_path):
    async def scenario():
        _, records, _, binding_ref = setup_records(tmp_path)

        class Evaluator:
            output_id = "intel"
            active_fingerprint = "active-1"
            calls = 0

            async def execute(self, *args):
                self.calls += 1
                return {}

        evaluator = Evaluator()
        binding = PublicationBinding("publication-a", ("intel",), "binding-1")
        gateway = PublicationSteering(records, {"intel": evaluator}, owner_lock=asyncio.Lock(), allow_evaluation=True)
        actor = PublicationActor("account-a", "editor-a", frozenset({("publication-a", "read"), ("publication-a", "steer")}))
        payload = {
            "bindingRevision": "binding-1",
            "bindingRef": binding_ref.to_json(),
            "classifier": "intel",
            "requestId": "steer-1",
            "expectedFingerprint": "active-1",
            "action": "evaluate",
            "proposal": {"rubric": "Draft"},
            "explanation": "Human steering reason.",
        }
        for bad_actor, bad in [
            (PublicationActor("account-a", "other", frozenset({("publication-b", "steer")})), payload),
            (actor, {**payload, "classifier": "other"}),
            (actor, {**payload, "expectedFingerprint": "old"}),
            (actor, {**payload, "action": "set_review_rate"}),
            (actor, {**payload, "explanation": " "}),
        ]:
            with pytest.raises(PublicationError):
                await gateway.command(bad_actor, binding, bad)
        assert evaluator.calls == 0 and records.records() == []

    asyncio.run(scenario())


def test_lost_settlement_acknowledgement_recovers_receipt_without_another_execution(tmp_path, monkeypatch):
    async def scenario():
        _, records, _, binding_ref = setup_records(tmp_path)

        class Evaluator:
            output_id = "intel"
            active_fingerprint = "active-1"
            calls = 0

            async def execute(self, *args):
                self.calls += 1
                return {"promoted": False, "trial_fingerprint": "trial-1", "improved": False}

        evaluator = Evaluator()
        actor = PublicationActor("account-a", "editor-a", frozenset({("publication-a", "read"), ("publication-a", "steer")}))
        binding = PublicationBinding("publication-a", ("intel",), "binding-1")
        payload = {
            "bindingRevision": "binding-1",
            "bindingRef": binding_ref.to_json(),
            "classifier": "intel",
            "requestId": "lost-settlement",
            "expectedFingerprint": "active-1",
            "action": "evaluate",
            "proposal": {"rubric": "Human draft"},
            "explanation": "Evaluate this output only.",
        }
        original = records.put
        lost = []

        def publish(kind, data, at):
            identifier = original(kind, data, at)
            if kind == "steering-complete" and not lost:
                lost.append(True)
                raise OSError("Synthetic acknowledgement loss")
            return identifier

        monkeypatch.setattr(records, "put", publish)
        gateway = PublicationSteering(records, {"intel": evaluator}, owner_lock=asyncio.Lock(), allow_evaluation=True)
        with pytest.raises(PublicationError, match="uncertain"):
            await gateway.command(actor, binding, payload)
        monkeypatch.setattr(records, "put", original)
        resumed = PublicationSteering(records, {"intel": evaluator}, owner_lock=asyncio.Lock(), allow_evaluation=False)
        receipt = await resumed.command(actor, binding, payload)
        assert receipt["result"]["trial_fingerprint"] == "trial-1" and evaluator.calls == 1
        with pytest.raises(ValueError, match="historical output ownership"):
            records.check_binding(PublicationBinding("other-publication", ("intel",), "binding-2"))

    asyncio.run(scenario())


@pytest.mark.parametrize("budget", ["records", "bytes"])
def test_insufficient_receipt_capacity_refuses_steering_before_dispatch(tmp_path, budget):
    async def scenario():
        _, records, _, binding_ref = setup_records(tmp_path)
        if budget == "records":
            records.max_records = 1
        else:
            records.max_bytes = 1_000  # smaller than the steering receipts themselves

        class Evaluator:
            output_id = "intel"
            active_fingerprint = "active-1"
            calls = 0

            async def execute(self, *args):
                self.calls += 1
                return {}

        evaluator = Evaluator()
        binding = PublicationBinding("publication-a", ("intel",), "binding-1")
        actor = PublicationActor("account-a", "editor-a", frozenset({("publication-a", "steer"), ("publication-a", "read")}))
        payload = {
            "bindingRevision": "binding-1",
            "bindingRef": binding_ref.to_json(),
            "classifier": "intel",
            "requestId": "no-capacity",
            "expectedFingerprint": "active-1",
            "action": "evaluate",
            "proposal": {"rubric": "Draft"},
            "explanation": "Human request.",
        }
        gateway = PublicationSteering(records, {"intel": evaluator}, owner_lock=asyncio.Lock(), allow_evaluation=True)
        with pytest.raises(PublicationError, match="nothing dispatched"):
            await gateway.command(actor, binding, payload)
        assert evaluator.calls == 0 and records.records() == []

    asyncio.run(scenario())


def test_a_steering_result_of_two_hundred_kilobytes_is_recorded_whole(tmp_path):
    async def scenario():
        _, records, _, binding_ref = setup_records(tmp_path)
        records.max_bytes = 10_000_000  # the catalog budget is configuration, not a steering limit

        class Evaluator:
            output_id = "intel"
            active_fingerprint = "active-1"

            async def execute(self, *args):
                return {"report": "r" * 200_000}

        binding = PublicationBinding("publication-a", ("intel",), "binding-1")
        actor = PublicationActor("account-a", "editor-a", frozenset({("publication-a", "steer"), ("publication-a", "read")}))
        payload = {
            "bindingRevision": "binding-1",
            "bindingRef": binding_ref.to_json(),
            "classifier": "intel",
            "requestId": "large-result",
            "expectedFingerprint": "active-1",
            "action": "evaluate",
            "proposal": {"rubric": "Draft " + "d" * 100_000},
            "explanation": "Human request.",
        }
        gateway = PublicationSteering(records, {"intel": Evaluator()}, owner_lock=asyncio.Lock(), allow_evaluation=True)
        receipt = await gateway.command(actor, binding, payload)
        assert receipt["result"] == {"report": "r" * 200_000}

    asyncio.run(scenario())
