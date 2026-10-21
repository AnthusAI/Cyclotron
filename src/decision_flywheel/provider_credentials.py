"""Lazy, cached provider credential from AWS Secrets Manager; never logged, never in the environment."""

import json
import threading

from .command_refusal import CommandRefused

SECRET_NAME = "cyclotron/providers/openai"


class ProviderKeyUnavailable(CommandRefused):
    def __init__(self):
        super().__init__("provider_key_unavailable", "provider credential is unavailable; no provider call was made")


def parse_secret(raw):
    """Plaintext key, or JSON {"api_key": "..."}. Returns None unless exactly one usable key."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except ValueError:
            return None
        if not isinstance(value, dict) or set(value) - {"api_key"}:
            return None
        text = value.get("api_key")
        if not isinstance(text, str):
            return None
        text = text.strip()
    if not 8 <= len(text) <= 4096 or any(c.isspace() or not c.isprintable() for c in text):
        return None
    return text


class SecretsManagerKeySource:
    """Reads the fixed secret at first use and caches it for the warm container.

    ``client_factory`` is called lazily so the BFF, which has no read grant, never builds a client.
    Any failure collapses to ``ProviderKeyUnavailable`` carrying no provider or secret detail.
    """

    def __init__(self, client_factory, name=SECRET_NAME):
        self._client_factory, self.name = client_factory, name
        self._key, self._lock = None, threading.Lock()

    def __call__(self):
        with self._lock:
            if self._key is None:
                try:
                    key = parse_secret(self._client_factory().get_secret_value(SecretId=self.name).get("SecretString"))
                except Exception:
                    key = None
                if key is None:
                    raise ProviderKeyUnavailable() from None
                self._key = key
            return self._key
