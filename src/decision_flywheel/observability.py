"""A single-writer step driver and replayable trace for application frontends."""
from uuid import uuid4


class RequestBudgetExhausted(RuntimeError):
    """Collection paused before reserving or sending another paid request."""


class StepFailed(RuntimeError):
    def __init__(self, error_type):
        self.error_type = error_type
        super().__init__("flywheel step failed; inspect the recorded trace")


async def step(wheel, stage, training, development, *, trigger="manual", request_budget=None,
               parent_step_id=None, **kwargs):
    if stage not in {"rubric", "examples", "questions", "classifier"}:
        raise ValueError("unknown step stage")
    if not isinstance(trigger, str) or not trigger.strip():
        raise ValueError("step trigger must be a non-empty string")
    if request_budget is not None and (type(request_budget) is not int or request_budget < 0):
        raise ValueError("step request budget must be nonnegative")
    if parent_step_id is not None and (not isinstance(parent_step_id, str) or not parent_step_id.strip()):
        raise ValueError("parent step ID must be a non-empty string")
    if wheel._step_running:
        raise RuntimeError("one driver may advance this flywheel at a time")
    wheel._step_running = True
    step_id = str(uuid4())
    token = wheel._step_context.set({"step_id": step_id, "step_stage": stage, "parent_step_id": parent_step_id})
    ceiling = wheel.max_requests
    if request_budget is not None:
        wheel.max_requests = min(ceiling, wheel.requests + request_budget)
    try:
        wheel._emit({"kind": "step-started", "trigger": trigger,
            "context": wheel.optimizer_context, "configuration": wheel.active.config.briefing_state(),
            "classifier_version": wheel.active.fingerprint,
            "training_count": len(training), "development_count": len(development),
            "request_budget": request_budget, "session_request_ceiling": ceiling})
        result = await wheel.optimize_stage(stage, training, development, **kwargs,
            **({"train_after_questions": False} if stage == "questions" else {}))
        reason = result.get("reason", "")
        status = "waiting" if "interrupted" in reason or "waiting" in reason else "completed"
        trials = result.get("trials", [])
        if result.get("error_type") or (trials and all(t.get("error_type") for t in trials)):
            status = "failed"
        elif any(t.get("error_type") for t in trials):
            status = "partial"
        wheel._emit({"kind": "step-completed", "status": status, "result": result,
                     "classifier_version": wheel.active.fingerprint})
        return {"step_id": step_id, "status": status, "result": result}
    except Exception as error:
        if isinstance(error, RequestBudgetExhausted):
            wheel._emit({"kind": "step-paused", "reason": "request budget exhausted",
                         "resume_requires_explicit_retry": True})
            return {"step_id": step_id, "status": "paused", "reason": "request budget exhausted"}
        wheel._emit({"kind": "step-failed", "error_type": type(error).__name__})
        raise StepFailed(type(error).__name__) from None
    finally:
        wheel.max_requests = ceiling
        wheel._step_context.reset(token)
        wheel._step_running = False
