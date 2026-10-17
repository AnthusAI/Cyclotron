import pytest


@pytest.fixture(autouse=True)
def starlette_test_client_counts_as_loopback(monkeypatch):
    monkeypatch.setattr('decision_flywheel.web_api.LOOPBACK_HOSTS', ('127.0.0.1', '::1', 'testclient'))
