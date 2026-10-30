"""Remote CLI sign-in for the lab: the discovery data and the approval pages, from the shared ``cli-login`` library.

A CLI on a machine with no browser runs ``cyclotron login --remote``. It reads ``/cli/discovery``, starts a sign-in
at the lab's broker, and prints an approval URL on this origin. The person opens ``/cli/approve`` on any device,
checks the matching code and signs in with Google; ``/cli/callback`` hands the authorization code to the broker,
and the waiting CLI exchanges it for its own tokens. The pages reach the broker only by invoking its Lambda with
the BFF's IAM role; the broker never holds a token. Design: AnthusAI/cli-login ``docs/design.md``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

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


def install_cli_login_routes(app, oauth, config: CliLoginConfig, invoke):
    """Mount ``/cli/discovery``, ``/cli/approve`` and ``/cli/callback`` for the lab's broker."""
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
    )

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
