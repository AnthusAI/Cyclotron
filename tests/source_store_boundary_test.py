"""Source-store consumers reuse injected repositories without native fallbacks."""

import asyncio
from dataclasses import asdict

from decision_flywheel.batch_storage import SQLiteBatchStorage
from decision_flywheel.classifier_config import ClassifierConfig
from decision_flywheel.flywheel import FittedClassifier
from decision_flywheel.matched_run_evaluation import evaluate_matched_runs
from decision_flywheel.matched_run_evaluation_test import Model
from decision_flywheel.matched_run_plan import plan_matched_runs
from decision_flywheel.matched_run_plan_test import source
from decision_flywheel.models import DecisionTask


def test_frozen_evaluator_uses_injected_clock_trace_and_existing_batch_contract(tmp_path):
    class Evidence:
        def __init__(self):
            self.clocks = {}
            self.trace = {}

        def bind_clock(self, plan, clock):
            return self.clocks.setdefault(plan, clock)

        def append_trace(self, key, payload):
            return self.trace.setdefault(key, (len(self.trace) + 1, payload))

    evidence = Evidence()
    handles = []

    class Batches(SQLiteBatchStorage):
        def close(self):
            self.closed = True
            super().close()

    def batches(side):
        store = Batches(tmp_path / "batch-records" / f"{side}.sqlite")
        handles.append(store)
        return store

    endpoints = {side: source(side) for side in ("before", "after")}
    for endpoint in endpoints.values():
        endpoint["checkpoint"]["payload"]["classifiers"] = {
            "topic": {"state": asdict(FittedClassifier(ClassifierConfig(DecisionTask("topic", ("yes", "no"), "Choose"))))}
        }
    plan = plan_matched_runs(**endpoints)
    models = {side: Model() for side in endpoints}

    def execute():
        return asyncio.run(
            evaluate_matched_runs(
                plan,
                endpoints,
                models,
                tmp_path / "unused-native-layout",
                max_requests=8,
                observer=lambda _: None,
                execution_storage=evidence,
                batch_storage_factory=batches,
            )
        )

    first = execute()
    second = execute()
    assert first["requests"] == 8 and second["new_requests"] == 0
    assert first["classifiers"] == second["classifiers"]
    assert sum(len(model.requests) for model in models.values()) == 8
    assert not list((tmp_path / "unused-native-layout").glob("*.sqlite3"))
    assert handles and all(store.closed for store in handles)
