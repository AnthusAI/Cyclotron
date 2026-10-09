"""A cyclotron store moves between workers, has one writer, and survives a crash between its two stores."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import textwrap

import pytest

from .embedded_cyclotron import Cyclotron, CyclotronDefinition, StoreLocked
from .embedded_cyclotron_test import DEFINITION, RELEVANT, Model, item, run

SECRET = "sk-live-not-a-real-key-123"


class KeyedModel(Model):
    """A decision model that holds a provider key, as real adapters do."""
    api_key = SECRET


def populate(cyclotron):
    first = run(cyclotron.decide(item("ref-1", "yes one")))
    run(cyclotron.review(first.decision_id, "exclude", explanation="Vendor marketing.", reason_code="out_of_scope"))
    second = run(cyclotron.decide(item("ref-2", "no two")))
    run(cyclotron.review(second.decision_id, "exclude", selected_by="reviewer"))
    run(cyclotron.decide(item("ref-3", "yes three")))
    return first, second


def comparable(status):
    snapshot = status.to_json()
    snapshot.pop("asOf")
    return snapshot


def test_a_second_writer_is_refused_before_anything_is_opened(tmp_path):
    model = Model()
    with Cyclotron.open(tmp_path / "c", DEFINITION, model):
        with pytest.raises(StoreLocked):
            Cyclotron.open(tmp_path / "c", DEFINITION, model)
    assert model.calls == 0
    Cyclotron.open(tmp_path / "c", DEFINITION, model).close()  # released on close


def test_an_application_lease_guards_the_store(tmp_path):
    class Claim:
        held = False
        def __init__(self):
            self.calls = []
        def acquire(self):
            self.calls.append("acquire")
            if Claim.held:
                raise StoreLocked("assignment claimed by another worker")
            Claim.held = True
        def release(self):
            self.calls.append("release")
            Claim.held = False
    claim = Claim()
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), lease=claim):
        with pytest.raises(StoreLocked, match="another worker"):
            Cyclotron.open(tmp_path / "other", DEFINITION, Model(), lease=Claim())
    assert claim.calls == ["acquire", "release"]
    assert not (tmp_path / "other" / "cyclotron.sqlite3").exists()


def test_a_snapshot_restores_to_the_same_status_and_decision_cache(tmp_path):
    model = Model()
    with Cyclotron.open(tmp_path / "a", DEFINITION, model) as cyclotron:
        first, _ = populate(cyclotron)
        before = comparable(cyclotron.status())
        events = cyclotron.subscribe(limit=1000)["events"]
        manifest = cyclotron.snapshot(tmp_path / "out" / "store.tar.gz")
    assert manifest["schema"] == "cyclotron-store/v1" and len(manifest["files"]) == 3
    calls = model.calls
    Cyclotron.restore(tmp_path / "out" / "store.tar.gz", tmp_path / "b")
    with Cyclotron.open(tmp_path / "b", DEFINITION, model) as restored:
        assert comparable(restored.status()) == before
        assert restored.subscribe(limit=1000)["events"] == events
        assert run(restored.decide(item("ref-1", "yes one"))) == first
        assert run(restored.decide(item("ref-3", "yes three"))).item_id == "ref-3"
    assert model.calls == calls


def test_restore_refuses_an_existing_store_and_a_damaged_archive(tmp_path):
    with Cyclotron.open(tmp_path / "a", DEFINITION, Model()) as cyclotron:
        populate(cyclotron)
        cyclotron.snapshot(tmp_path / "store.tar.gz")
    with pytest.raises(ValueError, match="without a cyclotron store"):
        Cyclotron.restore(tmp_path / "store.tar.gz", tmp_path / "a")
    damaged = tmp_path / "damaged.tar.gz"
    with tarfile.open(tmp_path / "store.tar.gz") as source, tarfile.open(damaged, "w:gz") as target:
        for member in source.getmembers():
            data = source.extractfile(member).read()
            if member.name == "shared.sqlite3":
                data = data[:-1] + bytes([data[-1] ^ 1])
            member.size = len(data)
            import io
            target.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match="damaged"):
        Cyclotron.restore(damaged, tmp_path / "b")


def test_a_snapshot_holds_no_provider_key_and_redacted_text_stays_redacted(tmp_path):
    with Cyclotron.open(tmp_path / "a", DEFINITION, KeyedModel(), redact=(SECRET,)) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        run(cyclotron.review(decision.decision_id, "include", explanation=f"pasted {SECRET} by mistake"))
        cyclotron.snapshot(tmp_path / "store.tar.gz")
    with tarfile.open(tmp_path / "store.tar.gz") as archive:
        contents = b"".join(archive.extractfile(member).read() for member in archive.getmembers())
    assert SECRET.encode() not in contents
    assert b"[REDACTED]" in contents


def test_replaying_recorded_labels_rebuilds_the_same_labels_and_partitions(tmp_path):
    with Cyclotron.open(tmp_path / "a", DEFINITION, Model()) as original:
        populate(original)
        corrected = run(original.decide(item("ref-4", "yes four")))
        run(original.review(corrected.decision_id, "exclude"))
        run(original.review(corrected.decision_id, "include", explanation="Misread it."))
        labels = original.labels()
        partitions = [[row.item.id for row in group] for group in original._partitions("relevant")[:2]]
        alignment = original.status().alignment
    with Cyclotron.open(tmp_path / "b", DEFINITION, Model()) as rebuilt:
        assert run(rebuilt.replay(labels)) == len(labels) == 3
        assert [(r["item"].id, r["label"], r["selected_by"]) for r in rebuilt.labels()] == [
            (r["item"].id, r["label"], r["selected_by"]) for r in labels]
        assert [[row.item.id for row in group] for group in rebuilt._partitions("relevant")[:2]] == partitions
        assert rebuilt.status().alignment.labels == alignment.labels


CHILD = textwrap.dedent('''
    import asyncio, os, sys
    from decision_flywheel import embedded_cyclotron, flywheel
    from decision_flywheel.embedded_cyclotron import Cyclotron, CyclotronDefinition
    from decision_flywheel.embedded_cyclotron_test import DEFINITION, RELEVANT, Model, item
    from decision_flywheel.optimizer_agent import OptimizerAgent, OptimizerReply

    store, point = sys.argv[1], sys.argv[2]

    def die_after(owner, name):
        original = getattr(owner, name)
        def wrapped(*args, **kwargs):
            result = original(*args, **kwargs)
            os._exit(17)
        setattr(owner, name, wrapped)

    async def main():
        if point == "decision":
            with Cyclotron.open(store, DEFINITION, Model()) as cyclotron:
                embedded_cyclotron.Cyclotron._insert_decision = lambda *a, **k: os._exit(17)
                await cyclotron.decide(item("ref-1", "yes one"))
        elif point == "review":
            with Cyclotron.open(store, DEFINITION, Model()) as cyclotron:
                decision = await cyclotron.decide(item("ref-1", "yes one"))
                die_after(flywheel.DecisionFlywheel, "record_feedback_event")
                await cyclotron.review(decision.decision_id, "exclude", review_id="message-1",
                                       explanation="Vendor marketing.", reason_code="out_of_scope")
        elif point == "activation":
            optimizer = OptimizerAgent(lambda _: OptimizerReply(
                '{"rationale":"Editors include yes items","rubric":"Include items that say yes."}', "fake"))
            definition = CyclotronDefinition("papyrus-relevance", (RELEVANT,), seed="spec-seed",
                                             optimize_every=1000, rubric_changes_every=1)
            with Cyclotron.open(store, definition, Model(), optimizer, max_requests=500) as cyclotron:
                die_after(flywheel.DecisionFlywheel, "_activate")
                for index in range(40):
                    decision = await cyclotron.decide(item(f"learn-{index}", ("yes " if index % 2 else "no ") + str(index)))
                    await cyclotron.review(decision.decision_id, "include" if index % 2 else "exclude")
        os._exit(0)

    asyncio.run(main())
''')


def crash(tmp_path, point):
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    source = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (source, os.environ.get("PYTHONPATH"))))}
    result = subprocess.run([sys.executable, str(script), str(tmp_path / "c"), point], capture_output=True, text=True, env=env)
    assert result.returncode == 17, result.stderr


def kinds(cyclotron):
    return [event["kind"] for event in cyclotron.subscribe(limit=1000)["events"]]


def test_a_crash_after_the_classifier_records_a_review_loses_no_event(tmp_path):
    crash(tmp_path, "review")
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        assert kinds(cyclotron) == ["decision", "review"]
        review = cyclotron.subscribe(limit=1000)["events"][1]["review"]
        assert (review["reviewId"], review["label"], review["reasonCode"]) == ("message-1", "exclude", "out_of_scope")
        decision_id = review["decisionId"]
        again = run(cyclotron.review(decision_id, "exclude", review_id="message-1",
                                     explanation="Vendor marketing.", reason_code="out_of_scope"))
        assert again.review_id == "message-1"
        feedback = [e for e in cyclotron.wheels["relevant"].history(100000) if e["kind"] == "human-feedback"]
        assert len(feedback) == 1
        assert kinds(cyclotron) == ["decision", "review"]
        assert cyclotron.status().alignment.labels == 1


def test_a_crash_before_a_decision_is_saved_is_resumed_without_a_model_call(tmp_path):
    crash(tmp_path, "decision")
    model = Model()
    with Cyclotron.open(tmp_path / "c", DEFINITION, model) as cyclotron:
        assert kinds(cyclotron) == []
        decision = run(cyclotron.decide(item("ref-1", "yes one")))
        assert model.calls == 0
        assert kinds(cyclotron) == ["decision"]
        run(cyclotron.review(decision.decision_id, "include"))
        assert cyclotron.status().pending.decisions_awaiting_review == 0


def test_a_crash_right_after_an_activation_still_reports_the_change(tmp_path):
    crash(tmp_path, "activation")
    definition = CyclotronDefinition("papyrus-relevance", (RELEVANT,), seed="spec-seed",
                                     optimize_every=1000, rubric_changes_every=1)
    with Cyclotron.open(tmp_path / "c", definition, Model()) as cyclotron:
        changes = [e for e in cyclotron.subscribe(limit=1000)["events"] if e["kind"] in ("promoted", "refit")]
        assert len(changes) == 1
        status = cyclotron.status()
        assert status.last_change.kind == changes[0]["kind"]
    with Cyclotron.open(tmp_path / "c", definition, Model()) as cyclotron:
        assert len([e for e in cyclotron.subscribe(limit=1000)["events"] if e["kind"] in ("promoted", "refit")]) == 1
