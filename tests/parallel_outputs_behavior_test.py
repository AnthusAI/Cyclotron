"""An engine over S3 does each output's storage work at once, and keeps results in output order."""

import asyncio
import threading
import time

import pytest

from decision_flywheel.embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from decision_flywheel.embedded_cyclotron_test import Model, item
from decision_flywheel.s3_embedded_storage import S3CyclotronStores
from decision_flywheel.s3_journal_test import ConditionalS3, journal
from decision_flywheel.s3_runtime_storage import RuntimeOwnershipError

OUTPUTS = ("gamma", "alpha", "beta")
DEFINITION = CyclotronDefinition(
    "parallel-outputs",
    tuple(
        ClassifierSpec(name, ("include", "exclude"), f"Does this belong in {name}?", positive_label="include") for name in OUTPUTS
    ),
    seed="parallel-seed",
)


class SlowS3(ConditionalS3):
    """Every runtime-journal write takes a while, and the most writes in flight at once are counted per phase."""

    def __init__(self):
        super().__init__()
        self.phase, self.in_flight, self.most = None, 0, {}
        self.counter = threading.Lock()

    def put_object(self, **request):
        if "/runtime-" not in request["Key"]:
            return super().put_object(**request)
        with self.counter:
            self.in_flight += 1
            self.most[self.phase] = max(self.most.get(self.phase, 0), self.in_flight)
        try:
            time.sleep(0.02)
            return super().put_object(**request)
        finally:
            with self.counter:
                self.in_flight -= 1


def stores(client):
    return S3CyclotronStores(
        journal(client, name="parallel-root"), journal_for=lambda domain, name: journal(client, name=domain + "-" + name)
    )


def test_every_outputs_runtime_is_acquired_written_and_released_at_once_and_results_keep_output_order():
    client, model = SlowS3(), Model()
    client.phase = "open"
    engine = Cyclotron.open(None, DEFINITION, model, storage=stores(client))
    try:
        assert list(engine.wheels) == list(OUTPUTS)
        client.phase = "decide"
        decision = asyncio.run(engine.decide(item("parallel-item")))
        client.phase = "flush"
        engine.flush()
    finally:
        client.phase = "close"
        engine.close()
    assert client.most["open"] == len(OUTPUTS)
    assert client.most["decide"] == len(OUTPUTS)  # each output's answer reservation, together
    assert client.most["close"] == len(OUTPUTS)
    assert list(decision.results) == list(OUTPUTS)
    assert model.calls == 1  # one shared decision-model request for every output

    client.phase = "reopen"
    with Cyclotron.open(None, DEFINITION, model, storage=stores(client)) as reopened:
        assert asyncio.run(reopened.decide(item("parallel-item"))) == decision
    assert model.calls == 1


def test_an_engine_that_cannot_acquire_one_output_releases_the_others_it_acquired():
    client, model = ConditionalS3(), Model()
    blocker = stores(client)
    blocker.runtime_for_classifier("alpha")  # Another owner holds one output's runtime only.
    with pytest.raises(RuntimeOwnershipError):
        Cyclotron.open(None, DEFINITION, model, storage=stores(client))
    blocker.release_classifier_runtime("alpha")
    with Cyclotron.open(None, DEFINITION, model, storage=stores(client)) as engine:
        assert list(asyncio.run(engine.decide(item("after-release"))).results) == list(OUTPUTS)


def test_a_flush_keeps_ownership_and_makes_held_writes_durable_for_another_reader():
    client, model = ConditionalS3(), Model()
    engine = Cyclotron.open(None, DEFINITION, model, storage=stores(client))
    try:
        decision = asyncio.run(engine.decide(item("flushed-item")))
        engine.flush()
        writes = len(client.calls)
        engine.flush()  # Nothing is held any more: no write.
        assert len(client.calls) == writes
        with pytest.raises(RuntimeOwnershipError):
            Cyclotron.open(None, DEFINITION, model, storage=stores(client))  # Still owned.
        again = asyncio.run(engine.decide(item("flushed-item")))
        assert again == decision and model.calls == 1
    finally:
        engine.close()
