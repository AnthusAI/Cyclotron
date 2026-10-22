import pytest

from .storage_contract_backends import (
    BACKEND_NAMES,
    LAKE_BACKENDS,
    BatchBackend,
    EmbeddedBackend,
    EvaluationBackend,
    ReviewBackend,
    RuntimeBackend,
)


def _backend_fixture(backend_class, names=BACKEND_NAMES):
    @pytest.fixture(params=names)
    def backend(request, tmp_path):
        opened_backend = backend_class(request.param, tmp_path)
        yield opened_backend
        opened_backend.release_everything()
    return backend


embedded_backend = _backend_fixture(EmbeddedBackend)
runtime_backend = _backend_fixture(RuntimeBackend, BACKEND_NAMES + tuple(LAKE_BACKENDS))
review_backend = _backend_fixture(ReviewBackend)
evaluation_backend = _backend_fixture(EvaluationBackend)
batch_backend = _backend_fixture(BatchBackend)
