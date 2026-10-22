"""Transient S3 read failures get exactly one retry; naming a failure never leaks its message."""

import logging

import pytest

from . import s3_transient
from .s3_transient import cause_name, is_transient, read_with_one_retry


class ClientError(Exception):
    def __init__(self, code, status=400):
        super().__init__("https://bucket.s3.amazonaws.com/secret-key?X-Amz-Signature=abc")
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class ReadTimeoutError(Exception):
    pass


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(s3_transient, "RETRY_PAUSE_SECONDS", 0)


@pytest.mark.parametrize(
    "error",
    [
        ClientError("SlowDown", 503),
        ClientError("InternalError", 500),
        ClientError("RequestTimeout", 400),
        ClientError("Whatever", 502),
        ClientError("Whatever", 429),
        ReadTimeoutError(),
        TimeoutError(),
        ConnectionResetError(),
    ],
)
def test_throttling_server_errors_and_timeouts_are_transient(error):
    assert is_transient(error)


@pytest.mark.parametrize(
    "error", [ClientError("NoSuchKey", 404), ClientError("AccessDenied", 403), ValueError("bad"), KeyError("x")]
)
def test_missing_forbidden_and_logic_errors_are_permanent(error):
    assert not is_transient(error)


def test_a_transient_failure_is_retried_once_and_the_second_success_is_returned():
    outcomes = [ClientError("SlowDown", 503), "bytes"]

    def read():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert read_with_one_retry(read, "payload") == "bytes" and outcomes == []


def test_a_second_transient_failure_stands_and_is_not_retried_again():
    calls = []

    def read():
        calls.append(1)
        raise ClientError("SlowDown", 503)

    with pytest.raises(ClientError):
        read_with_one_retry(read, "payload")
    assert len(calls) == 2


def test_a_permanent_failure_is_not_retried():
    calls = []

    def read():
        calls.append(1)
        raise ClientError("AccessDenied", 403)

    with pytest.raises(ClientError):
        read_with_one_retry(read, "payload")
    assert len(calls) == 1


def test_a_failure_is_named_by_class_only_and_the_log_carries_no_message(caplog):
    error = ClientError("SlowDown", 503)
    assert cause_name(error) == "ClientError"
    with caplog.at_level(logging.WARNING, logger="decision_flywheel.storage"):
        read_with_one_retry(_once(error), "payload")
    assert "ClientError" in caplog.text and "amazonaws" not in caplog.text and "Signature" not in caplog.text


def _once(error):
    state = {"n": 0}

    def read():
        state["n"] += 1
        if state["n"] == 1:
            raise error
        return "ok"

    return read
