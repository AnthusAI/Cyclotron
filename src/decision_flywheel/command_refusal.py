"""A queued command refused with a stable non-secret code: before any provider dispatch, or by the provider itself."""


class CommandRefused(Exception):
    """Fail closed with a public code. The message must never contain secrets or private text."""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code
        self.public_message = message or code


class ProviderRejectedRequest(CommandRefused):
    """The model provider answered that the request itself is invalid, so no decision came back.

    Sending the same request again cannot succeed, so the command ends with a failed receipt instead of
    holding its claim. Only the status and the provider's request id are kept, never the provider's message.
    """

    def __init__(self, status, provider_request_id=None):
        message = f"the decision model rejected the request (HTTP {status}); no decision was returned"
        if provider_request_id:
            message += f" (provider request id {provider_request_id})"
        super().__init__("provider_rejected", message)
        self.status = status
