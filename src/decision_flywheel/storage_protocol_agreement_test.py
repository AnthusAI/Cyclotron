"""Each storage protocol and both of its adapters declare the same methods with the same parameters."""

from inspect import Parameter, signature

import pytest

from .batch_storage import BatchStorage, SQLiteBatchStorage
from .embedded_storage import EmbeddedStorage, SQLiteEmbeddedStorage
from .evaluation_storage import EvaluationStorage, SQLiteEvaluationStorage
from .review_storage import ReviewStorage, SQLiteReviewStorage
from .runtime_storage import RuntimeStateStorage, SQLiteRuntimeStateStorage
from .s3_batch_storage import S3BatchStorage
from .s3_embedded_storage import S3EmbeddedStorage
from .s3_evaluation_storage import S3EvaluationStorage
from .s3_review_storage import S3ReviewStorage
from .s3_runtime_storage import _State as S3RuntimeState

PROTOCOLS_WITH_ADAPTERS = [
    (EmbeddedStorage, SQLiteEmbeddedStorage, S3EmbeddedStorage),
    (RuntimeStateStorage, SQLiteRuntimeStateStorage, S3RuntimeState),
    (ReviewStorage, SQLiteReviewStorage, S3ReviewStorage),
    (EvaluationStorage, SQLiteEvaluationStorage, S3EvaluationStorage),
    (BatchStorage, SQLiteBatchStorage, S3BatchStorage),
]


def protocol_method_names(protocol):
    return sorted(name for name, member in vars(protocol).items() if callable(member) and not name.startswith("_"))


def parameter_shape(method):
    return [
        (parameter.name, parameter.kind, parameter.default if parameter.default is not Parameter.empty else None)
        for name, parameter in signature(method).parameters.items()
        if name != "self"
    ]


@pytest.mark.parametrize(
    "protocol,sqlite_adapter,s3_adapter",
    PROTOCOLS_WITH_ADAPTERS,
    ids=[protocol.__name__ for protocol, _, _ in PROTOCOLS_WITH_ADAPTERS],
)
def test_both_adapters_implement_every_protocol_method_with_its_parameters(protocol, sqlite_adapter, s3_adapter):
    method_names = protocol_method_names(protocol)
    assert method_names
    for adapter in (sqlite_adapter, s3_adapter):
        missing = [name for name in method_names if not callable(getattr(adapter, name, None))]
        assert missing == [], f"{adapter.__name__} lacks {missing}"
    mismatched = [
        name
        for name in method_names
        if not parameter_shape(getattr(protocol, name))
        == parameter_shape(getattr(sqlite_adapter, name))
        == parameter_shape(getattr(s3_adapter, name))
    ]
    assert mismatched == []
