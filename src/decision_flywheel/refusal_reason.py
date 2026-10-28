"""A short, safe reason for a refused machine or publication request, for the request log line."""

import json

LOGGED_PATHS = ("/machine/", "publications")
# The conflict message is str(exc) from storage; only its code and class name are logged.
CODE_ONLY = {"request_conflict": "ValueError"}


def logs_reason(path, status):
    return status >= 400 and any(part in path for part in LOGGED_PATHS)


def refusal_reason(status, body):
    """The response's ``code`` else its HTTPException ``detail``; never request data, headers or free-form messages."""
    if status < 400:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if isinstance(code, str) and code:
        reason = f"{code} ({CODE_ONLY[code]})" if code in CODE_ONLY else code
    elif isinstance(payload.get("detail"), str):
        reason = payload["detail"]
    else:
        return None
    reason = "".join(ch if ch.isprintable() else " " for ch in reason)
    return reason or None
