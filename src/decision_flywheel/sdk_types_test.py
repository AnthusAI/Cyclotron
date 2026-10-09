"""The TypeScript types an application imports name every key the SDK writes."""
from pathlib import Path

from .embedded_cyclotron import Cyclotron
from .embedded_cyclotron_test import DEFINITION, Model, item, run

TYPES = Path(__file__).resolve().parents[2] / "trace-ui" / "src" / "cyclotronSdk.ts"


def keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            if key not in ("classifiers", "probabilities"):
                yield from keys(child)
            else:
                for nested in (child or {}).values():
                    yield from keys(nested)
    elif isinstance(value, list):
        for child in value:
            yield from keys(child)


def test_decision_review_and_event_keys_are_typed(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        review = run(cyclotron.review(decision.decision_id, "exclude", explanation="Vendor marketing."))
        cyclotron.set_review_rate(.5, set_by="editor")
        events = cyclotron.subscribe(limit=1000)["events"]
    written = set(keys(decision.to_json())) | set(keys(review.to_json())) | set(keys(events))
    source = TYPES.read_text()
    missing = sorted(key for key in written if f"{key}:" not in source and f"{key}?:" not in source)
    assert not missing, f"trace-ui/src/cyclotronSdk.ts lacks {missing}"
    assert {e["kind"] for e in events} == {"decision", "review", "review-rate-changed"}
