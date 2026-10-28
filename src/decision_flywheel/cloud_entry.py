"""Standard API Gateway v2 and SQS Lambda entry points; no work on import."""

import asyncio
import json
import os
import time
import traceback

_cached_runtime = None
_cached_adapter = None


def runtime():
    global _cached_runtime
    if _cached_runtime is None:
        from botocore.config import Config
        from botocore.session import get_session

        from .hosted_runtime import HostedConfig, HostedRuntime

        config = HostedConfig.from_environment(os.environ)
        from .hosted_publications import targets_from_environment

        targets = targets_from_environment(os.environ)
        from .machine_access import principals_from_environment

        principals = principals_from_environment(os.environ)
        if config.allow_live and targets is None:
            raise ValueError("live cloud execution requires reviewed publication targets")
        session = get_session()
        model_factory = None
        if config.allow_live:
            from .adapters.hosted_jev import HostedJevFactory
            from .provider_credentials import SecretsManagerKeySource

            # The client is built, and the secret read, only inside the first authorized provider use.
            key_source = SecretsManagerKeySource(
                lambda: session.create_client(
                    "secretsmanager",
                    region_name=config.region,
                    config=Config(retries={"total_max_attempts": 2}, connect_timeout=5, read_timeout=10),
                )
            )
            model_factory = HostedJevFactory.from_environment(os.environ, key_source)
        settings = Config(signature_version="s3v4", retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=10)
        s3 = session.create_client("s3", region_name=config.region, config=settings)
        kms = session.create_client(
            "kms", region_name=config.region, config=Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=10)
        )
        sqs = session.create_client("sqs", region_name=config.region)
        from .decision_events import DecisionEventNotifier

        decision_events = DecisionEventNotifier.from_environment(
            os.environ,
            lambda: session.create_client(
                "events",
                region_name=config.region,
                config=Config(retries={"total_max_attempts": 2}, connect_timeout=2, read_timeout=3),
            ),
        )
        _cached_runtime = HostedRuntime(
            config,
            s3=s3,
            kms=kms,
            sqs=sqs,
            model_factory=model_factory,
            publication_targets=targets,
            machine_principals=principals,
            decision_events=decision_events,
        )
    return _cached_runtime


def bff_handler(event, context):
    from .prospectus_accounts import account_request_id

    global _cached_adapter
    if _cached_adapter is None:
        from mangum import Mangum

        # Clients belong to the warm function, not each request's ASGI lifespan.
        _cached_adapter = Mangum(runtime().app(), lifespan="off")
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or loop.is_closed():
        asyncio.set_event_loop(asyncio.new_event_loop())
    correlation = account_request_id.set(getattr(context, "aws_request_id", None))
    try:
        return _cached_adapter(event, context)
    finally:
        account_request_id.reset(correlation)


def describe_failure(error, started, context=None):
    """One log line about a failed delivery: exception classes and code locations only.

    Never the message, arguments, payload, locals or source text: exception messages can carry provider or
    credential detail. Classes cover the cause chain; frames are `file:line function` for this package
    (relative path) and `package/file:line` for libraries, innermost last (at most eight).
    """
    classes, seen, current = [], set(), error
    while current is not None and id(current) not in seen and len(classes) < 6:
        seen.add(id(current))
        classes.append(f"{type(current).__module__}.{type(current).__qualname__}")
        current = current.__cause__ or current.__context__
    frames = []
    for frame in traceback.extract_tb(error.__traceback__)[-8:]:
        path = frame.filename.replace("\\", "/")
        short = path.split("decision_flywheel/", 1)[1] if "decision_flywheel/" in path else "/".join(path.split("/")[-2:])
        frames.append(f"{short}:{frame.lineno} {frame.name}")
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    return json.dumps(
        {
            "event": "delivery_failed",
            "errors": classes,
            "frames": frames,
            "elapsedMs": round((time.monotonic() - started) * 1000),
            "remainingMs": remaining() if callable(remaining) else None,
        },
        separators=(",", ":"),
    )


def is_schedule(event):
    return isinstance(event, dict) and event.get("source") == "aws.events" and event.get("detail-type") == "Scheduled Event"


def worker_handler(event, context):
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    seconds = (lambda: remaining() / 1000) if callable(remaining) else None
    if is_schedule(event):
        # The once-a-minute sweep: a fresh invocation, outside any SQS chain, starts each run's waiting head.
        return {"sweep": asyncio.run(runtime().worker().sweep(seconds))}
    if not isinstance(event, dict) or not isinstance(event.get("Records"), list):
        raise ValueError("SQS batch required")

    started = time.monotonic()

    async def process():
        failures = []
        for record in event["Records"]:
            identifier = record.get("messageId")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("SQS delivery identity required")
            try:
                body = record.get("body")
                if not isinstance(body, str):
                    raise ValueError("queue reference body required")
                await runtime().worker().process(json.loads(body), seconds)
            except Exception as error:
                # No token, raw payload, private model error, message or source is logged: only exception
                # classes and code locations (describe_failure). Uncertain/running claims retry only into
                # fail-closed diagnosis and eventually the DLQ; they never grant another paid dispatch.
                print(describe_failure(error, started, context))
                failures.append({"itemIdentifier": identifier})
        return {"batchItemFailures": failures}

    return asyncio.run(process())
