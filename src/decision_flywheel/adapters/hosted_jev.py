"""Explicit Jev-only baseline factory; construction makes no model call."""

from collections.abc import Callable
from dataclasses import dataclass, field

from ..optimizer_agent import DisabledOptimizer
from .jev import JevAdapter, JevConfiguration


@dataclass(frozen=True)
class HostedJevFactory:
    model: str
    # Resolves the provider credential lazily, inside the authorized worker, per decision.
    key_source: Callable[[], str] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("an explicit reviewed Jev model is required")

    @classmethod
    def from_environment(cls, environment, key_source=None):
        try:
            return cls(environment["CYCLOTRON_PREDICTION_MODEL"], key_source)
        except (ValueError, KeyError, TypeError):
            raise ValueError("a reviewed prediction model is required") from None

    def __call__(self, config):
        if (
            config.get("decisions_provider") != "jev"
            or config.get("decisions_model") != self.model
            or config.get("priming_examples")
            or config.get("priming_list_id")
        ):
            raise ValueError("run differs from the reviewed baseline provider configuration")
        # Optional provider dependencies and credentials are resolved only in
        # the authorized worker. Never infer a provider or run an optimizer.
        # A missing credential refuses here, before any state registration or request reservation.
        api_key = self.key_source() if self.key_source is not None else None
        return (
            JevAdapter.from_environment(configuration=JevConfiguration(model=self.model), api_key=api_key),
            DisabledOptimizer(),
        )
