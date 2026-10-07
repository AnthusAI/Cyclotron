"""Executable Gherkin contract for the application-host boundary."""
from pytest_bdd import given, scenarios, then, when

from decision_flywheel.web_store import WebStore
from decision_flywheel.web_worker import WebWorker


scenarios("features/application_runtime.feature")


class ScriptedWorkspaceRuntime:
    def __init__(self):
        self.calls = []

    def normalize_run_config(self, config, items):
        self.calls.append((dict(config), tuple(items)))
        return {"runtime": "scripted", "max_requests": 1}

    def open_session(self, run_id, config, items, current):  # pragma: no cover - no job in this scenario
        raise AssertionError("the scenario only creates a run")


@given("a web worker with a scripted workspace runtime", target_fixture="worker_context")
def scripted_worker(tmp_path):
    runtime = ScriptedWorkspaceRuntime()
    worker = WebWorker(WebStore(tmp_path / "workspace.sqlite3"), tmp_path / "runs", runtime=runtime, allow_live=True)
    return worker, runtime


@when("the application creates a run", target_fixture="run")
def create_run(worker_context):
    worker, _ = worker_context
    return worker.create_run("Runtime contract", {"requested": "by caller"})


@then("the runtime receives the caller configuration and items")
def runtime_receives_inputs(worker_context):
    _, runtime = worker_context
    assert runtime.calls == [({"requested": "by caller"}, ())]


@then("the saved run contains the runtime-normalized configuration")
def saved_run_is_normalized(run):
    assert run["config"] == {"runtime": "scripted", "max_requests": 1}
