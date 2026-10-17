"""Host configuration resolves credentials privately and never accepts client endpoints."""

import json

import pytest

from .priming_config import load_priming_service
from .web_store import WebStore


def test_host_config_loads_configurable_http_fields_and_environment_authority_without_exposing_headers(tmp_path):
    store = WebStore(tmp_path / "db")
    store.save_item_list("l", "Examples")
    store.save_classifier("c", "Output", {"question": "Choose", "classes": [{"label": "yes"}, {"label": "no"}]})
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "transformers": [
                    {
                        "id": "domain",
                        "name": "Domain",
                        "endpoint": "https://transformer.invalid",
                        "authorization_env": "SYNTHETIC_AUTH",
                        "fields": [{"name": "reference", "label": "Reference", "required": True}],
                    }
                ]
            }
        )
    )
    service = load_priming_service(store, path, environment={"SYNTHETIC_AUTH": "Bearer synthetic-private"})
    try:
        assert service.transformers["domain"].headers == {"Authorization": "Bearer synthetic-private"}
        assert "synthetic-private" not in json.dumps(service.contract("l", [{"id": "c", "revision": 1}]))
        with pytest.raises(ValueError):
            service.safe({"values": "Bearer synthetic-private"})
    finally:
        service.close()


def test_bearer_credentials_are_refused_even_without_the_header_prefix(tmp_path):
    store = WebStore(tmp_path / "db")
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "transformers": [
                    {
                        "id": "domain",
                        "name": "Domain",
                        "endpoint": "https://transformer.invalid",
                        "authorization_env": "AUTH",
                        "fields": [],
                    }
                ]
            }
        )
    )
    service = load_priming_service(store, path, environment={"AUTH": "Bearer synthetic-secret"})
    try:
        with pytest.raises(ValueError, match="credential"):
            service.safe({"text": "synthetic-secret"})
    finally:
        service.close()
