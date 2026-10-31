"""The ``cyclotron`` command: sign in to a Cyclotron lab from a machine with no browser, and call it.

    cyclotron login --remote [--lab URL] [--as EMAIL] [--note TEXT]
    cyclotron status [--lab URL]
    cyclotron whoami [--lab URL]
    cyclotron accounts [--lab URL]
    cyclotron use ACCOUNT | --clear [--lab URL]
    cyclotron logout [--lab URL]

``login --remote`` prints an approval URL and a matching code; the person opens the URL on any device, checks the
code and signs in. The CLI then holds its own renewable session in the most secure store this machine has (never
plain text). Sign-in, storage and renewal come from the shared ``cli-login`` library; this module only finds the
lab's settings and names the commands.

A person may belong to several lab accounts. ``use`` chooses one by name or id, and the choice is kept on this
machine per lab and per person. The choice grants nothing: the lab checks the person's current access each time the
CLI selects the account, which it does again for every renewed access token, since each one starts a new lab session.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
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


def _post_json(url: str, body: dict, headers: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method="POST", headers={"content-type": "application/json", **headers})
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


def enroller(lab: str, endpoint: str, *, post_json=_post_json):
    """The enroll step: the lab enrolls this new login at Prospectus, as it does every browser sign-in."""

    def enroll(tokens, transaction, enrollment):
        from cli_login.remote import RemoteLoginError

        if not enrollment:
            raise RemoteLoginError("enrollment_missing", "The lab did not prepare this sign-in for enrollment; start again.")
        status, answer = post_json(
            endpoint,
            {"transaction": transaction, "enrollment": enrollment},
            {"Authorization": "Bearer " + tokens["access_token"], "X-Identity-Token": tokens["id_token"]},
        )
        if status != 200 or not answer.get("ok"):
            raise RemoteLoginError(
                str(answer.get("error") or "enrollment_refused"),
                "The lab could not enroll this sign-in; start again.",
                transient=status >= 500,
            )

    return enroll


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
    enrollment = data.get("enrollment_endpoint")
    if enrollment is not None and enrollment != lab + "/cli/enroll":
        raise CliError("The lab's enrollment endpoint is on another site; refusing to sign in.")
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
    ), enrollment


class FileSelections:
    """The account chosen for each lab, in one private file on this machine. An account id is not a secret, but it
    is kept beside the session's other metadata, readable only by this user."""

    FILE = "selected-accounts.json"

    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else None

    def _path(self):
        from cli_login.locks import default_state_dir, ensure_state_dir

        return ensure_state_dir(self.directory or default_state_dir(NAMESPACE)) / self.FILE

    def _read(self):
        try:
            data = json.loads(self._path().read_text())
        except (FileNotFoundError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data):
        path = self._path()
        handle, name = tempfile.mkstemp(dir=path.parent, prefix=".selected-")
        try:
            with os.fdopen(handle, "w") as file:
                json.dump(data, file, indent=2, sort_keys=True)
            os.replace(name, path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    def get(self, lab, subject):
        record = self._read().get(lab)
        if isinstance(record, dict) and record.get("subject") == subject and isinstance(record.get("accountId"), str):
            return record["accountId"]
        return None

    def set(self, lab, subject, account_id):
        data = self._read()
        data[lab] = {"subject": subject, "accountId": account_id}
        self._write(data)

    def clear(self, lab):
        data = self._read()
        if data.pop(lab, None) is not None:
            self._write(data)


def _choose(accounts, wanted):
    """The one account named by id or by name (case-insensitive), or a message listing the choices."""
    matches = [row for row in accounts if row["id"] == wanted] or [
        row for row in accounts if row["name"].casefold() == wanted.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(f"{row['name']} ({row['id']})" for row in accounts) or "none"
    reason = "names more than one account" if matches else "is not one of your accounts"
    raise CliError(f"{wanted!r} {reason}. Your accounts: {names}.")


def _whoami(data):
    return json.dumps(
        {key: data.get(key) for key in ("personId", "accounts", "selectedAccountId", "canWrite") if key in data}, indent=2
    )


def open_session(target, **overrides):
    from cli_login.session import Session

    return Session(target, login_command=LOGIN_COMMAND, api_token="access", authorization_scheme="Bearer", **overrides)


def main(
    argv=None,
    *,
    out=print,
    get_json=_get_json,
    post_json=_post_json,
    session_factory=open_session,
    remote=None,
    selections=None,
) -> int:
    parser = argparse.ArgumentParser(prog="cyclotron", description="Sign in to a Cyclotron lab and check the session.")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="sign in; --remote approves the sign-in from another device")
    login.add_argument("--remote", action="store_true", required=True, help="approve from another device (required for now)")
    login.add_argument("--as", dest="expected_email", help="refuse the session unless this person approves it")
    login.add_argument("--note", help="shown on the approval page, for example which agent is asking")
    for name, text in (
        ("status", "show the local session, without tokens"),
        ("whoami", "ask the lab who this session is and which account it works in"),
        ("accounts", "list the lab accounts you belong to"),
        ("logout", "remove the local session and revoke it"),
    ):
        commands.add_parser(name, help=text)
    use = commands.add_parser("use", help="choose the account later commands work in")
    choice = use.add_mutually_exclusive_group(required=True)
    choice.add_argument("account", nargs="?", help="the account's name or id")
    choice.add_argument("--clear", action="store_true", help="forget the chosen account")
    for command in commands.choices.values():
        command.add_argument("--lab", default=DEFAULT_LAB, help=f"the lab's https origin (default {DEFAULT_LAB})")
    args = parser.parse_args(argv)
    selections = selections if selections is not None else FileSelections()
    try:
        from cli_login.errors import AuthError

        lab = _origin(args.lab)
        target, enrollment = discover(lab, get_json=get_json)
        session = session_factory(target)
        if args.command == "login":
            if remote is None:
                from cli_login.remote import remote_login as remote
            enroll = enroller(lab, enrollment, post_json=post_json) if enrollment else None
            remote(target, session.store_token_set, out=out, expected_email=args.expected_email, note=args.note, enroll=enroll)
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
        if args.command == "use" and args.clear:
            selections.clear(lab)
            out("No account is chosen for this lab now.")
            return 0
        headers = session.headers(lab)
        subject = session.status().get("subject")
        status, data = get_json(lab + "/api/session", headers)
        if status != 200 or not data.get("authenticated"):
            raise CliError(f"The lab did not accept this session; run `{LOGIN_COMMAND}`.")
        accounts = data.get("accounts") or []

        def select(account_id):
            status, answer = post_json(lab + "/api/accounts/select", {"accountId": account_id}, headers)
            if status == 200 and answer.get("selectedAccountId") == account_id:
                return answer
            if status in (401, 403):
                return None
            raise CliError("The lab could not choose the account right now; try again.")

        if args.command == "use":
            account = _choose(accounts, args.account)
            if select(account["id"]) is None:
                raise CliError(f"You don't have access to {account['name']} in this lab.")
            selections.set(lab, subject, account["id"])
            out(f"Now working in {account['name']} ({account['id']}).")
            return 0
        chosen = selections.get(lab, subject)
        if args.command == "accounts":
            rows = [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "role": row["role"],
                    "status": row["entitlement"]["status"],
                    "capabilities": row["entitlement"]["capabilities"],
                    "selected": row["id"] == chosen,
                }
                for row in accounts
            ]
            out(json.dumps(rows, indent=2))
            return 0
        if chosen and data.get("selectedAccountId") != chosen:
            # Each renewed access token is a new lab session with no account; selecting again makes the lab recheck
            # the person's current access to it.
            answer = select(chosen)
            if answer is None:
                selections.clear(lab)
                out(_whoami(data))
                out("You no longer have access to the account chosen for this lab; choose another with `cyclotron use`.")
                return 1
            data = answer
        out(_whoami(data))
        return 0
    except (CliError, AuthError) as error:  # RemoteLoginError is an AuthError
        out(str(error))
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
