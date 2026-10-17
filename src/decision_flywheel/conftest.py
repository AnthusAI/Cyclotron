import pytest

from .storage_contract_backends import (
    BACKEND_NAMES,
    BatchBackend,
    EmbeddedBackend,
    EvaluationBackend,
    ReviewBackend,
    RuntimeBackend,
)


def _backend_fixture(backend_class):
    @pytest.fixture(params=BACKEND_NAMES)
    def backend(request, tmp_path):
        opened_backend = backend_class(request.param, tmp_path)
        yield opened_backend
        opened_backend.release_everything()
    return backend


embedded_backend = _backend_fixture(EmbeddedBackend)
runtime_backend = _backend_fixture(RuntimeBackend)
review_backend = _backend_fixture(ReviewBackend)
evaluation_backend = _backend_fixture(EvaluationBackend)
batch_backend = _backend_fixture(BatchBackend)
