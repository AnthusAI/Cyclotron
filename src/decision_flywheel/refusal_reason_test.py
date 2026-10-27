import json

from .refusal_reason import REASON_LIMIT, logs_reason, refusal_reason


def body(**value):
    return json.dumps(value).encode()


def test_an_http_exception_detail_is_the_reason():
    assert refusal_reason(409, body(detail="publication binding changed")) == "publication binding changed"


def test_a_code_wins_over_detail_and_a_request_conflict_logs_code_and_class_only():
    reason = refusal_reason(409, body(code="request_conflict", message="secret@example.com item text", detail="x"))
    assert reason == "request_conflict (ValueError)"
    assert "secret" not in reason


def test_a_success_has_no_reason():
    assert refusal_reason(202, body(code="x", jobId="j")) is None


def test_unparseable_or_unexpected_bodies_have_no_reason():
    assert refusal_reason(500, b"<html>") is None
    assert refusal_reason(400, body(detail=[{"input": "private"}])) is None


def test_a_long_reason_is_capped_and_control_characters_removed():
    reason = refusal_reason(400, body(detail="a\nb" + "c" * 500))
    assert len(reason) == REASON_LIMIT and "\n" not in reason


def test_only_machine_and_publication_routes_log_a_reason():
    assert logs_reason("/machine/decisions", 409) and logs_reason("/api/publications/p/operations", 400)
    assert not logs_reason("/machine/decisions", 202) and not logs_reason("/graphql", 401)
