import asyncio
from dataclasses import replace

import pytest

from .candidate_fitting import fit_candidate
from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from .embedded_cyclotron_test import Model, item
from .iteration_transfer import IterationStale, adopt_iteration, iteration_record
from .models import LabeledItem


def spec(name):
    return ClassifierSpec(name, ("include", "exclude"), question=f"Is this for {name}?")


def definition():
    return CyclotronDefinition("shared", (spec("one"), spec("two")), seed="seed")


def votes():
    return tuple(
        LabeledItem(item(f"vote-{n}", f"{'yes' if n % 2 else 'no'} story {n}"), "include" if n % 2 else "exclude", "trusted", {})
        for n in range(8)
    )


def opened(path, model=None):
    return Cyclotron.open(path, definition(), model or Model())


def learned(path):
    """An iteration of output ``one``, learned in its own store away from the live one."""

    async def learn():
        engine = opened(path)
        base = {cid: wheel.active.fingerprint for cid, wheel in engine.wheels.items()}
        rows = votes()
        pools = {"one": rows, "two": ()}
        wheel = engine.wheels["one"]
        async with engine.execution():
            config = replace(wheel.active.config, rubric="Learned rubric", example_ids=tuple(row.item.id for row in rows))
            engine.shared.bind_context({"one": config, "two": engine.wheels["two"].active.config}, pools)
            candidate, _ = await fit_candidate(
                wheel,
                config,
                rows,
                (),
                protected=(),
                propensities={r.item.id: 1.0 for r in rows},
                validation_status="provisional",
            )
            wheel._activate(candidate)
            record = iteration_record(
                engine,
                identity="first",
                parent=None,
                base=base,
                examples={"one": rows},
                voted=[row.item.id for row in rows],
                acceptance={"one": {"accepted": True}},
            )
        engine.close()
        return record, candidate

    return asyncio.run(learn())


def test_a_live_store_adopts_an_iteration_learned_elsewhere_without_calling_the_model(tmp_path):
    record, candidate = learned(tmp_path / "learning")
    model = Model()
    live = opened(tmp_path / "live", model)
    result = asyncio.run(adopt_iteration(live, record))
    assert result["adopted"]
    assert live.wheels["one"].active.fingerprint == candidate.fingerprint
    assert model.calls == 0
    live.close()


def test_an_adopted_iteration_survives_a_restart_and_the_next_decision_does_not_refit_it(tmp_path):
    record, candidate = learned(tmp_path / "learning")
    live = opened(tmp_path / "live")
    asyncio.run(adopt_iteration(live, record))
    live.close()
    model = Model()
    reopened = opened(tmp_path / "live", model)
    assert reopened.wheels["one"].active.fingerprint == candidate.fingerprint
    asyncio.run(reopened.decide(item("new-story", "yes a new story")))
    assert model.calls == 1
    assert reopened.wheels["one"].active.fingerprint == candidate.fingerprint
    reopened.close()


def test_a_voted_item_decided_again_is_not_among_its_own_examples(tmp_path):
    record, _ = learned(tmp_path / "learning")
    seen = []

    class Watching(Model):
        async def classify_many(self, configs, target, training, **kwargs):
            seen.append({row.item.id for row in training["one"]} - {target.id})
            return await super().classify_many(configs, target, training, **kwargs)

    live = opened(tmp_path / "live", Watching())
    asyncio.run(adopt_iteration(live, record))
    wheel = live.wheels["one"]
    request = wheel.active.config.request(item("vote-1", "yes story 1"), live.priming_examples["one"])
    assert all("yes story 1" != example.get("text") for example in request["state"]["examples"])
    live.close()


def test_adopting_the_same_iteration_again_changes_nothing(tmp_path):
    record, _ = learned(tmp_path / "learning")
    live = opened(tmp_path / "live")
    asyncio.run(adopt_iteration(live, record))
    assert not asyncio.run(adopt_iteration(live, record))["adopted"]
    live.close()


def test_an_iteration_grown_from_another_parent_is_refused_whole(tmp_path):
    record, _ = learned(tmp_path / "learning")
    live = opened(tmp_path / "live")
    record = {**record, "parent": "an-iteration-this-store-never-adopted"}
    with pytest.raises(IterationStale):
        asyncio.run(adopt_iteration(live, record))
    assert live.wheels["one"].active.config.rubric != "Learned rubric"
    live.close()


def test_a_later_vote_on_an_example_replaces_its_earlier_label(tmp_path):
    record, _ = learned(tmp_path / "learning")
    live = opened(tmp_path / "live")
    asyncio.run(adopt_iteration(live, record))
    assert sum(row.item.id == "vote-1" for row in live.priming_examples["one"]) == 1
    live.close()
