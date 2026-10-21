"""Explicit bounded Jev-only baseline factory; construction makes no model call."""

from dataclasses import dataclass

from ..optimizer_agent import DisabledOptimizer
from .jev import JevAdapter, JevConfiguration


@dataclass(frozen=True)
class HostedJevFactory:
    model: str
    maximum_requests: int

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("an explicit reviewed Jev model is required")
        if type(self.maximum_requests) is not int or not 1 <= self.maximum_requests <= 100:
            raise ValueError("an explicit baseline request ceiling between1 and100 is required")

    @classmethod
    def from_environment(cls, environment):
        try:
            raw = environment["CYCLOTRON_PREDICTION_MAX_REQUESTS"]
            if not isinstance(raw, str) or not raw.isdecimal():
                raise ValueError()
            return cls(environment["CYCLOTRON_PREDICTION_MODEL"], int(raw))
        except (ValueError, KeyError, TypeError):
            raise ValueError("reviewed prediction model and request ceiling are required") from None

    def __call__(self, config):
        if (
            config.get("decisions_provider") != "jev"
            or config.get("decisions_model") != self.model
            or type(config.get("max_requests")) is not int
            or not 1 <= config["max_requests"] <= self.maximum_requests
            or config.get("priming_examples")
            or config.get("priming_list_id")
        ):
            raise ValueError("run exceeds or differs from the reviewed baseline provider configuration")
        # Optional provider dependencies and credentials are resolved only in
        # the authorized worker. Never infer a provider or run an optimizer.
        return JevAdapter.from_environment(configuration=JevConfiguration(model=self.model)), DisabledOptimizer()
