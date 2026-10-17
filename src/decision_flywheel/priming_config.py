"""Operator-owned JSON configuration for standalone HTTP transformers."""

import json
import os

from .priming import HTTPTransformer, PrimingService


def load_priming_service(store, path, *, environment=None, redact=()):
    environment = os.environ if environment is None else environment
    if path is None:
        return PrimingService(store, redact=redact)
    with open(path, encoding="utf-8") as source:
        config = json.load(source)
    if not isinstance(config, dict) or set(config) - {"schemas", "transformers"}:
        raise ValueError("unknown priming host configuration")
    transformers = []
    for row in config.get("transformers", []):
        if set(row) - {"id", "name", "endpoint", "fields", "allow_local", "authorization_env"}:
            raise ValueError("unknown transformer configuration")
        headers = {}
        if row.get("authorization_env"):
            value = environment.get(row["authorization_env"])
            if not value:
                raise ValueError("configured transformer authority is unavailable")
            headers["Authorization"] = value
        transformers.append(
            HTTPTransformer(
                row["id"], row["name"], row["endpoint"], tuple(row["fields"]), headers or None, row.get("allow_local", False)
            )
        )
    return PrimingService(store, transformers=transformers, schemas=config.get("schemas"), redact=redact)
