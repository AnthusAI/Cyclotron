"""Credential values are collected for local trace redaction, never persistence."""
from .credential_redaction import credential_values


def test_non_openai_provider_credentials_are_redacted_without_treating_every_environment_value_as_secret():
    environment={'ANTHROPIC_API_KEY':'fake-anthropic-secret','OPENAI_API_KEY':'fake-openai-secret',
        'AWS_SECRET_ACCESS_KEY':'fake-aws-secret','AWS_SESSION_TOKEN':'fake-session-secret',
        'FLYWHEEL_WEB_TOKEN':'fake-web-secret','HOME':'/fake/home','PATH':'/fake/bin','EMPTY_API_KEY':''}
    assert set(credential_values(environment))=={'fake-anthropic-secret','fake-openai-secret',
        'fake-aws-secret','fake-session-secret','fake-web-secret'}
