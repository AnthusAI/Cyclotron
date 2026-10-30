"""Remote CLI sign-in for the lab: the discovery data and the approval pages, from the shared ``cli-login`` library.

A CLI on a machine with no browser runs ``cyclotron login --remote``. It reads ``/cli/discovery``, starts a sign-in
at the lab's broker, and prints an approval URL on this origin. The person opens ``/cli/approve`` on any device,
checks the matching code and signs in with Google; ``/cli/callback`` hands the authorization code to the broker,
and the waiting CLI exchanges it for its own tokens. The pages reach the broker only by invoking its Lambda with
the BFF's IAM role; the broker never holds a token. Design: AnthusAI/cli-login ``docs/design.md``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from .account_sessions import SessionRejected
from .journal import decode, encode
from .prospectus_accounts import AccountAccessDenied, ProspectusUnavailable

PROTOCOL_VERSION = 1
PRIVILEGES = "Everything you can do in the lab yourself, within each account's grants."


@dataclass(frozen=True)
class CliLoginConfig:
    broker_arn: str
    broker_endpoint: str

    def __post_init__(self):
        if not re.fullmatch(r"arn:aws:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]+", self.broker_arn):
            raise ValueError("an exact broker function ARN is required")
        if not re.fullmatch(r"https://[a-z0-9.-]+/?", self.broker_endpoint):
            raise ValueError("the broker endpoint must be an https origin")

    @classmethod
    def from_environment(cls, environment=os.environ) -> CliLoginConfig | None:
        """None when remote CLI sign-in is not configured; both settings or neither."""
        arn, endpoint = environment.get("CYCLOTRON_CLI_BROKER_ARN", ""), environment.get("CYCLOTRON_CLI_BROKER_ENDPOINT", "")
        if not arn and not endpoint:
            return None
        return cls(arn, endpoint.rstrip("/"))


def lambda_invoker(client, function_arn):
    """Invoke the broker's private operations; a function error reads as an unavailable transaction."""

    def invoke(payload):
        result = client.invoke(FunctionName=function_arn, InvocationType="RequestResponse", Payload=json.dumps(payload).encode())
        if result.get("FunctionError"):
            return {"ok": False, "error": "unavailable"}
        try:
            answer = json.loads(result["Payload"].read())
        except (KeyError, ValueError):
            return {"ok": False, "error": "unavailable"}
        return answer if isinstance(answer, dict) else {"ok": False, "error": "unavailable"}

    return invoke


def _response(page):
    response = HTMLResponse(page.body, status_code=page.status)
    for name, value in page.headers:
        if name.lower() == "content-type":
            continue
        if name.lower() == "set-cookie":
            response.headers.append("set-cookie", value)
        else:
            response.headers[name] = value
    return response


def _enrollment_reference(transaction):
    # The sealed proof is bound to its one transaction: opening it under any other transaction fails.
    return hashlib.sha256(("cli-enroll:" + transaction).encode()).hexdigest()


class CliEnrollment:
    """Each CLI login is enrolled at Prospectus exactly like a browser login: a signed identity challenge whose
    nonce the Google sign-in carries, then central resolution with the login's own access and ID tokens.

    The approval page asks for the challenge and seals its proof with the lab's session cipher, bound to the
    transaction; the broker carries the sealed string to the CLI, which cannot read it. Right after its code
    exchange the CLI sends its tokens and the sealed string to ``/cli/enroll``, which opens it here and resolves
    the person. Proofs never leave the lab unsealed.
    """

    def __init__(self, gateway, cipher, *, clock=time.time):
        self.gateway, self.cipher, self.clock = gateway, cipher, clock

    def prepare(self, transaction, described):
        challenge = self.gateway.google_identity.challenge()
        proof = {
            "challengeId": challenge.challenge_id,
            "challengeToken": challenge.challenge_token,
            "nonce": challenge.nonce,
            "expiresAt": challenge.expires_at,
        }
        envelope = self.cipher.encrypt("pending", _enrollment_reference(transaction), encode(proof))
        return challenge.nonce, base64.urlsafe_b64encode(encode(envelope)).decode()

    def open(self, transaction, sealed):
        try:
            envelope = decode(base64.urlsafe_b64decode(sealed.encode()))
            proof = decode(self.cipher.decrypt("pending", _enrollment_reference(transaction), envelope))
        except Exception:
            raise SessionRejected("enrollment proof is not valid for this sign-in") from None
        if not isinstance(proof, dict) or proof.get("expiresAt", 0) <= self.clock():
            raise SessionRejected("enrollment proof expired; start the sign-in again")
        return proof

    def enroll(self, transaction, sealed, access_token, id_token):
        """Resolve the person for this CLI login and keep its session; raises SessionRejected or AccountAccessDenied."""
        from .account_routes import bearer_identifier

        gateway = self.gateway
        proof = self.open(transaction, sealed)
        claims = gateway.verifier.token_pair(access_token, id_token, proof["nonce"], require_session_binding=True)
        person = gateway.google_identity.resolve(
            access_token, id_token, proof["challengeId"], challenge_token=proof.get("challengeToken")
        )
        session = gateway.sessions.adopt(bearer_identifier(access_token), access_token, claims)
        if session.access_token != access_token or session.subject != claims["sub"]:
            raise SessionRejected("session identity changed")
        if session.person_id not in (None, person["id"]):
            raise SessionRejected("session identity changed")
        if session.person_id is None:
            session.person_id = person["id"]
            gateway.sessions.save(session)
        return person


def install_cli_login_routes(app, oauth, config: CliLoginConfig, invoke, enrollment: CliEnrollment | None = None):
    """Mount ``/cli/discovery``, ``/cli/approve`` and ``/cli/callback`` for the lab's broker, and ``/cli/enroll``
    when logins are enrolled centrally."""
    from cli_login.broker.pages import ApprovalPages

    origin = oauth.public_origin.rstrip("/")
    authorization = oauth.authorization_origin.rstrip("/")
    discovery = {
        "protocol": PROTOCOL_VERSION,
        "issuer": oauth.issuer,
        "client_id": oauth.client_id,
        "oauth_domain": authorization,
        "token_endpoint": authorization + "/oauth2/token",
        "api_endpoint": origin,
        "remote_login_endpoint": config.broker_endpoint,
        "remote_callback_uri": origin + "/cli/callback",
    }
    pages = ApprovalPages(
        invoke,
        authorize_endpoint=authorization + "/oauth2/authorize",
        product="Cyclotron lab",
        privileges=PRIVILEGES,
        identity_provider="Google",
        prepare=enrollment.prepare if enrollment is not None else None,
    )
    if enrollment is not None:
        discovery["enrollment_endpoint"] = origin + "/cli/enroll"

    @app.get("/cli/discovery")
    async def cli_discovery():
        return JSONResponse(discovery, headers={"cache-control": "no-store"})

    @app.get("/cli/approve")
    async def cli_approve(request: Request):
        return _response(await asyncio.to_thread(pages.approve, "GET", dict(request.query_params), {}, dict(request.cookies)))

    @app.post("/cli/approve")
    async def cli_approve_post(request: Request):
        form = await request.form()
        fields = {key: value for key, value in form.items() if isinstance(value, str)}
        return _response(await asyncio.to_thread(pages.approve, "POST", {}, fields, dict(request.cookies)))

    @app.get("/cli/callback")
    async def cli_callback(request: Request):
        return _response(await asyncio.to_thread(pages.callback, dict(request.query_params), dict(request.cookies)))

    if enrollment is None:
        return

    @app.post("/cli/enroll")
    async def cli_enroll(request: Request):
        from .account_routes import bearer_token

        headers = {"cache-control": "no-store"}
        token, id_token = bearer_token(request), request.headers.get("x-identity-token", "")
        try:
            body = await request.json()
        except ValueError:
            body = None
        if (
            not token
            or not id_token
            or request.cookies.get(oauth.cookie_name)
            or not isinstance(body, dict)
            or not isinstance(body.get("transaction"), str)
            or not isinstance(body.get("enrollment"), str)
        ):
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=headers)
        try:
            person = await asyncio.to_thread(enrollment.enroll, body["transaction"], body["enrollment"], token, id_token)
        except (SessionRejected, AccountAccessDenied):
            return JSONResponse({"error": "enrollment_refused"}, status_code=401, headers=headers)
        except ProspectusUnavailable:
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=503, headers=headers)
        return JSONResponse({"ok": True, "personId": person["id"]}, headers=headers)
