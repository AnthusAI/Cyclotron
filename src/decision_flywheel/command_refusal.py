"""A queued command refused before any provider dispatch, with a stable non-secret code."""


class CommandRefused(Exception):
    """Fail closed with a public code. The message must never contain secrets or private text."""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code
        self.public_message = message or code
