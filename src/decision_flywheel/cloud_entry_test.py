"""Lambda entry points accept native Gateway/SQS events without deployment."""

import json
from types import SimpleNamespace
from uuid import uuid4

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
