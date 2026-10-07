"""Select an optional optimizer transport only at explicit application setup."""


def validate_optimizer_transport(value):
    if value not in ("openai", "litellm"):
        raise ValueError("optimizer transport must be openai or litellm")
    return value


def optimizer_transport(config):
    transport = validate_optimizer_transport(config.get("optimizer_transport", "openai"))
    if transport == "litellm":
        from .litellm_optimizer import LiteLLMOptimizer
        cls = LiteLLMOptimizer
    else:
        from .openai_optimizer import OpenAIOptimizer
        cls = OpenAIOptimizer
    return cls.from_environment(model=config["optimizer_model"], max_calls=config["max_optimizer_calls"])
