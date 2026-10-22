"""Which S3 read failures are worth exactly one more attempt, and how to name a failure safely.

Only idempotent GETs use this. Writes and conditional puts never retry here: the
SDK is held to one attempt precisely so a lost successful PUT cannot become a
later 412 (see s3_journal.py). Failures are named by exception class only, so a
log line or an error message can never carry a URL, key, header or credential.
"""

import logging
import time

log = logging.getLogger("decision_flywheel.storage")

# One retry, after a short pause, is enough to ride out a throttle or a dropped connection.
RETRY_PAUSE_SECONDS = 0.1

TRANSIENT_CODES = frozenset(
    {
        "SlowDown",
        "ServiceUnavailable",
        "InternalError",
        "RequestTimeout",
        "RequestTimeoutException",
        "RequestLimitExceeded",
        "Throttling",
        "ThrottlingException",
        "ThrottledException",
        "TooManyRequestsException",
        "500",
        "502",
        "503",
        "504",
    }
)
TRANSIENT_CLASSES = frozenset(
    {
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "ConnectionClosedError",
        "EndpointConnectionError",
        "IncompleteReadError",
        "ResponseStreamingError",
        "ProxyConnectionError",
    }
)


def cause_name(error):
    """The exception class name alone; never its message, which may hold URLs or request ids."""
    return type(error).__name__


def is_transient(error):
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if any(klass.__name__ in TRANSIENT_CLASSES for klass in type(error).__mro__):
        return True
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    code = (response.get("Error") or {}).get("Code")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in TRANSIENT_CODES or (type(status) is int and (status == 429 or status >= 500))


def read_with_one_retry(read, what):
    """Run an idempotent read; retry once if it fails transiently, then let the second outcome stand."""
    try:
        return read()
    except Exception as error:
        if not is_transient(error):
            raise
        log.warning("transient S3 read failure on %s: %s; retrying once", what, cause_name(error))
    time.sleep(RETRY_PAUSE_SECONDS)
    return read()
