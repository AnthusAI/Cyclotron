"""The provider key is read lazily from one fixed secret and never leaks into errors, logs or the environment."""

import json
import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

from .adapters.hosted_jev import HostedJevFactory
from .provider_credentials import SECRET_NAME, ProviderKeyUnavailable, SecretsManagerKeySource, parse_secret

KEY = "synthetic-key-value-0001"


class Client:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def get_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def test_the_fixed_secret_name_is_the_only_one_ever_read():
    assert SECRET_NAME == "cyclotron/providers/openai"
    client = Client({"SecretString": KEY})
    assert SecretsManagerKeySource(lambda: client)() == KEY
    assert client.calls == [{"SecretId": "cyclotron/providers/openai"}]


@pytest.mark.parametrize(
    "raw,expected",
    [
        (KEY, KEY),
        (f"  {KEY}\n", KEY),
        (json.dumps({"api_key": KEY}), KEY),
        (json.dumps({"api_key": KEY, "other": "x"}), None),
        (json.dumps({"key": KEY}), None),
        (json.dumps({"api_key": ""}), None),
        ("", None),
        ("   ", None),
        ("two words here", None),
        ("{not json", None),
        (None, None),
        (b"bytes", None),
    ],
)
def test_plaintext_and_json_secrets_are_both_supported_and_anything_else_is_refused(raw, expected):
    assert parse_secret(raw) == expected


def test_the_key_is_read_lazily_once_and_cached_for_the_warm_container():
    built = []
    client = Client({"SecretString": KEY})
    source = SecretsManagerKeySource(lambda: built.append(1) or client)
    assert not built and not client.calls
    assert source() == source() == KEY
    assert len(built) == 1 and len(client.calls) == 1


@pytest.mark.parametrize(
    "client",
    [
        Client({"SecretString": ""}),
        Client({}),
        Client({"SecretBinary": b"x"}),
        Client(error=RuntimeError("ResourceNotFoundException " + KEY)),
        Client(error=PermissionError("AccessDeniedException")),
    ],
)
def test_missing_empty_or_unreadable_secrets_fail_closed_with_no_secret_text(client):
    source = SecretsManagerKeySource(lambda: client)
    with pytest.raises(ProviderKeyUnavailable) as error:
        source()
    assert error.value.code == "provider_key_unavailable"
    assert KEY not in str(error.value) and error.value.__cause__ is None and error.value.__suppress_context__
    # A failed read is never cached: creating the secret later recovers without a redeploy.
    client.response, client.error = {"SecretString": KEY}, None
    assert source() == KEY


def test_a_client_that_cannot_be_built_is_also_unavailable():
    def broken():
        raise RuntimeError("no region")

    with pytest.raises(ProviderKeyUnavailable):
        SecretsManagerKeySource(broken)()


def fake_sdk(monkeypatch):
    seen = {}

    class TypeSafeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    sdk, retry = ModuleType("typesafe_sdk"), ModuleType("typesafe_sdk._core.retry")
    sdk.TypeSafeClient, retry.RetryPolicy = TypeSafeClient, lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "typesafe_sdk", sdk)
    monkeypatch.setitem(sys.modules, "typesafe_sdk._core", ModuleType("typesafe_sdk._core"))
    monkeypatch.setitem(sys.modules, "typesafe_sdk._core.retry", retry)
    return seen


def config():
    return {"decisions_provider": "jev", "decisions_model": "jev-1.13.0", "max_requests": 1}


def test_the_hosted_factory_hands_the_key_to_the_sdk_without_touching_dotenv_or_the_environment(monkeypatch):
    seen = fake_sdk(monkeypatch)
    monkeypatch.setitem(sys.modules, "dotenv", None)  # would raise ImportError if the factory loaded dotenv
    before = dict(os.environ)
    factory = HostedJevFactory("jev-1.13.0", 100, SecretsManagerKeySource(lambda: Client({"SecretString": KEY})))
    model, optimizer = factory(config())
    assert seen["api_key"] == KEY and seen["model"] == "jev-1.13.0"
    assert model.model_identity == "jev:jev-1.13.0" and optimizer.enabled is False
    assert dict(os.environ) == before and KEY not in repr(factory)


def test_a_missing_key_fails_before_any_sdk_client_is_constructed(monkeypatch):
    seen = fake_sdk(monkeypatch)
    factory = HostedJevFactory("jev-1.13.0", 100, SecretsManagerKeySource(lambda: Client({"SecretString": " "})))
    with pytest.raises(ProviderKeyUnavailable):
        factory(config())
    assert not seen


def test_without_a_key_source_the_factory_keeps_its_environment_based_behavior(monkeypatch):
    seen = fake_sdk(monkeypatch)
    dotenv = ModuleType("dotenv")
    dotenv.load_dotenv = lambda **kwargs: seen.setdefault("dotenv", True)
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)
    HostedJevFactory("jev-1.13.0", 100)(config())
    assert seen["dotenv"] and seen["api_key"] is None
