"""The ``cyclotron`` command: sign in to a Cyclotron lab from a machine with no browser, and call it.

    cyclotron login --remote [--lab URL] [--as EMAIL] [--note TEXT]
    cyclotron status [--lab URL]
    cyclotron whoami [--lab URL]
    cyclotron logout [--lab URL]

``login --remote`` prints an approval URL and a matching code; the person opens the URL on any device, checks the
code and signs in. The CLI then holds its own renewable session in the most secure store this machine has (never
plain text). Sign-in, storage and renewal come from the shared ``cli-login`` library; this module only finds the
lab's settings and names the commands.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

DEFAULT_LAB = "https://lab.cyclotron.anth.us"
NAMESPACE = "cyclotron-lab"
LOGIN_COMMAND = "cyclotron login --remote"


class CliError(Exception):
    """A message for the person at the terminal; never carries a token."""


def _origin(url: str) -> str:
    parts = urlsplit(url if "://" in url else "https://" + url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.path not in ("", "/"):
        raise CliError("--lab must be an https origin such as https://lab.cyclotron.anth.us")
    return f"https://{parts.netloc.lower()}"


def _get_json(url: str, headers: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers={"accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https origins only
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {}
    except (OSError, ValueError) as error:
        raise CliError(f"The lab could not be reached ({type(error).__name__}); try again.") from None


def discover(lab: str, *, get_json=_get_json):
    """The lab's sign-in settings, checked so that every endpoint belongs to this lab or its provider."""
    from cli_login.target import Target

    status, data = get_json(lab + "/cli/discovery")
    if status != 200 or data.get("protocol") != 1:
        raise CliError("This lab does not offer remote CLI sign-in yet.")
    if data.get("api_endpoint") != lab:
        raise CliError("The lab's discovery names a different API; refusing to sign in.")
    callback = data.get("remote_callback_uri", "")
    if _origin(callback.rsplit("/cli/", 1)[0]) != lab:
        raise CliError("The lab's approval pages are on another site; refusing to sign in.")
    return Target(
        issuer=data["issuer"],
        client_id=data["client_id"],
        oauth_domain=data["oauth_domain"],
        token_endpoint=data["token_endpoint"],
        api_endpoint=lab,
        key=urlsplit(lab).hostname,
        namespace=NAMESPACE,
        callback_uri=callback,
        remote_callback_uri=callback,
        broker_endpoint=data["remote_login_endpoint"],
    )


def open_session(target, **overrides):
    from cli_login.session import Session

    return Session(target, login_command=LOGIN_COMMAND, api_token="access", authorization_scheme="Bearer", **overrides)


def main(argv=None, *, out=print, get_json=_get_json, session_factory=open_session, remote=None) -> int:
    parser = argparse.ArgumentParser(prog="cyclotron", description="Sign in to a Cyclotron lab and check the session.")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="sign in; --remote approves the sign-in from another device")
    login.add_argument("--remote", action="store_true", required=True, help="approve from another device (required for now)")
    login.add_argument("--as", dest="expected_email", help="refuse the session unless this person approves it")
    login.add_argument("--note", help="shown on the approval page, for example which agent is asking")
    for name, text in (
        ("status", "show the local session, without tokens"),
        ("whoami", "ask the lab who this session is"),
        ("logout", "remove the local session and revoke it"),
    ):
        commands.add_parser(name, help=text)
    for command in commands.choices.values():
        command.add_argument("--lab", default=DEFAULT_LAB, help=f"the lab's https origin (default {DEFAULT_LAB})")
    args = parser.parse_args(argv)
    try:
        from cli_login.errors import AuthError

        lab = _origin(args.lab)
        target = discover(lab, get_json=get_json)
        session = session_factory(target)
        if args.command == "login":
            if remote is None:
                from cli_login.remote import remote_login as remote
            remote(target, session.store_token_set, out=out, expected_email=args.expected_email, note=args.note)
            return 0
        if args.command == "status":
            out(json.dumps(session.status(), indent=2))
            return 0
        if args.command == "logout":
            result = session.logout()
            out(json.dumps(result, indent=2))
            if result.get("remote_revocation") == "unconfirmed":
                out("The local session is removed, but revoking it at the sign-in provider could not be confirmed.")
            return 0
        status, data = get_json(lab + "/api/session", session.headers(lab))
        if status != 200 or not data.get("authenticated"):
            raise CliError(f"The lab did not accept this session; run `{LOGIN_COMMAND}`.")
        out(
            json.dumps(
                {key: data.get(key) for key in ("personId", "accounts", "selectedAccountId", "canWrite") if key in data}, indent=2
            )
        )
        return 0
    except (CliError, AuthError) as error:
        out(str(error))
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
