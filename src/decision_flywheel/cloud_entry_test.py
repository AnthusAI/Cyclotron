"""Lambda entry points accept native Gateway/SQS events without deployment."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI


def test_api_gateway_v2_adapter_preserves_http_status_cookies_and_same_origin_paths(monkeypatch):
    from . import cloud_entry

    app = FastAPI()

    @app.get("/api/session")
    async def session():
        return {"authenticated": False, "loginUrl": "/auth/login"}

    monkeypatch.setattr(cloud_entry, "_cached_runtime", SimpleNamespace(app=lambda: app))
    monkeypatch.setattr(cloud_entry, "_cached_adapter", None)
    event = {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": "/api/session",
        "rawQueryString": "",
        "headers": {"host": "synthetic.execute-api.us-east-1.amazonaws.com"},
        "requestContext": {
            "accountId": "111111111111",
            "apiId": "synthetic",
            "domainName": "synthetic.execute-api.us-east-1.amazonaws.com",
            "stage": "$default",
            "http": {
                "method": "GET",
                "path": "/api/session",
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "synthetic",
            },
        },
        "isBase64Encoded": False,
    }
    response = cloud_entry.bff_handler(event, SimpleNamespace())
    assert response["statusCode"] == 200 and json.loads(response["body"])["authenticated"] is False


def test_sqs_partial_failure_never_treats_delivery_body_as_authority(monkeypatch):
    from . import cloud_entry

    calls = []

    class Worker:
        async def process(self, message):
            calls.append(message)
            if set(message) != {"accountId", "runId", "jobId"}:
                raise ValueError("references only")

    monkeypatch.setattr(cloud_entry, "_cached_runtime", SimpleNamespace(worker=lambda: Worker()))
    good = {"accountId": str(uuid4()), "runId": str(uuid4()), "jobId": str(uuid4())}
    event = {
        "Records": [
            {"messageId": "one", "body": json.dumps(good)},
            {"messageId": "two", "body": json.dumps(good | {"personId": "untrusted"})},
            {"messageId": "three", "body": "not-json"},
        ]
    }
    response = cloud_entry.worker_handler(event, SimpleNamespace())
    assert response == {"batchItemFailures": [{"itemIdentifier": "two"}, {"itemIdentifier": "three"}]}
    assert calls[0] == good


@pytest.mark.parametrize(
    "extra",
    [
        {"CYCLOTRON_ALLOW_LIVE": "yes"},
        {"CYCLOTRON_ALLOW_LIVE": "true"},
        {"CYCLOTRON_PUBLICATION_TARGETS": "[{}]"},
    ],
)
def test_invalid_live_activation_fails_before_creating_cloud_clients(monkeypatch, extra):
    import botocore.session

    from . import cloud_entry
    from .hosted_runtime_test import complete_environment

    for key, value in (complete_environment() | extra).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cloud_entry, "_cached_runtime", None)

    def forbidden():
        raise AssertionError("invalid activation created a cloud client")

    monkeypatch.setattr(botocore.session, "get_session", forbidden)
    with pytest.raises(ValueError):
        cloud_entry.runtime()


def test_live_runtime_wires_a_lazy_secret_reader_and_exact_machine_principals(monkeypatch):
    import botocore.session

    from . import cloud_entry
    from .hosted_runtime_test import complete_environment

    created = []

    class Session:
        def create_client(self, name, **kwargs):
            created.append(name)
            return SimpleNamespace(
                get_secret_value=lambda **kw: created.append(kw) or {"SecretString": "synthetic-key-value-0001"}
            )

    role = "arn:aws:iam::222222222222:role/synthetic-invoker"
    targets = [
        {
            "accountId": str(uuid4()),
            "runId": str(uuid4()),
            "standing": True,
            "bindings": [{"publicationId": "alpha", "outputs": ["alpha"], "revision": "1"}],
        }
    ]
    environment = complete_environment() | {
        "CYCLOTRON_ALLOW_LIVE": "true",
        "CYCLOTRON_PUBLICATION_TARGETS": json.dumps(targets),
        "CYCLOTRON_PREDICTION_MODEL": "jev-1.13.0",
        "CYCLOTRON_PREDICTION_MAX_REQUESTS": "100",
        "CYCLOTRON_MACHINE_PRINCIPALS": json.dumps([role]),
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cloud_entry, "_cached_runtime", None)
    monkeypatch.setattr(botocore.session, "get_session", lambda: Session())
    built = cloud_entry.runtime()
    assert built.machine_principals == (role,) and built.publication_targets[0].standing
    assert "secretsmanager" not in created, "the secret client must be built only at first provider use"
    assert built.model_factory.key_source() == "synthetic-key-value-0001"
    assert created[-2:] == ["secretsmanager", {"SecretId": "cyclotron/providers/openai"}]
    assert "synthetic-key-value-0001" not in repr(built.model_factory)


def test_wildcard_machine_principals_fail_before_creating_cloud_clients(monkeypatch):
    import botocore.session

    from . import cloud_entry
    from .hosted_runtime_test import complete_environment

    for key, value in (complete_environment() | {"CYCLOTRON_MACHINE_PRINCIPALS": '["arn:aws:iam::*:role/x"]'}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cloud_entry, "_cached_runtime", None)

    def forbidden():
        raise AssertionError("invalid principals created a cloud client")

    monkeypatch.setattr(botocore.session, "get_session", forbidden)
    with pytest.raises(ValueError):
        cloud_entry.runtime()


def test_failed_delivery_logs_classes_and_locations_but_never_messages_or_payloads(monkeypatch, capsys):
    from . import cloud_entry

    class Worker:
        async def process(self, message):
            try:
                raise OSError("sk-secret-token and a private prompt")
            except OSError as inner:
                raise ValueError("provider said: sk-secret-token") from inner

    monkeypatch.setattr(cloud_entry, "_cached_runtime", SimpleNamespace(worker=lambda: Worker()))
    body = json.dumps({"accountId": str(uuid4()), "runId": str(uuid4()), "jobId": str(uuid4())})
    event = {"Records": [{"messageId": "m1", "body": body}]}
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: 170000)
    assert cloud_entry.worker_handler(event, context) == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    out = capsys.readouterr().out
    line = json.loads(out.strip().splitlines()[-1])
    assert line["event"] == "delivery_failed" and line["errors"][:2] == ["builtins.ValueError", "builtins.OSError"]
    assert line["remainingMs"] == 170000 and any("cloud_entry_test.py" in frame for frame in line["frames"])
    assert "secret" not in out and "private prompt" not in out and "provider said" not in out and body not in out
