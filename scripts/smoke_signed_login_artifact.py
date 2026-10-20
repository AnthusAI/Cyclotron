"""Official-runtime smoke of public source build, using only synthetic proofs."""

import asyncio
from dataclasses import replace
from http.cookies import SimpleCookie
import json
import os
import platform
import runpy
import sys
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt

assert sys.version_info[:2] == (3, 13), "official Lambda Python3.13 required"
assert sys.platform == "linux" and platform.machine() == "x86_64", (
    "existing Lambda architecture required"
)
assert not any(
    os.environ.get(name)
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    )
), "smoke must have no cloud credentials"

# Keep the existing packaged Gateway/S3/worker/event regressions, with its fake
# provider and storage. Its emitted receipts contain synthetic evidence only.
base = runpy.run_path(sys.argv[1])
HostedRuntime, cloud_entry = base["HostedRuntime"], base["cloud_entry"]
config = replace(base["config"], allow_live=False)
verifier, private = base["verifier"], base["private"]
opaque, nonce, challenge_id = "v1.synthetic.c3ludGhldGlj.bWFj", "n" * 43, str(uuid4())
person = {"id": str(uuid4()), "createdAt": "synthetic"}
exchanges, resolutions = [], []


def central(method, path, access=None, body=None, **proofs):
    if path == "/v1/identities/google/challenge":
        assert method == "POST" and access is None and body == {}
        return {
            "challengeId": challenge_id,
            "nonce": nonce,
            "expiresAt": int(time.time()) + 300,
            "challengeToken": opaque,
        }
    if path == "/v1/identities/google/resolve":
        assert body == {"challengeToken": opaque}
        verifier.token_pair(
            access, proofs["identity_token"], nonce, require_session_binding=True
        )
        resolutions.append(True)
        return {"person": person}
    verifier.access(access)
    if path == "/v1/identities/me":
        return {"person": person}
    if path == "/v1/accounts":
        return {"person": person, "accounts": []}
    raise AssertionError("unexpected synthetic identity route")


def tokens(request):
    assert request.url.path == "/oauth2/token"
    exchanges.append(True)
    now = int(time.time())
    claims = {
        "iss": config.issuer,
        "sub": "synthetic-google-person",
        "iat": now,
        "exp": now + 1800,
        "auth_time": now,
        "origin_jti": "synthetic-lineage",
    }

    def encode(kind, extra):
        return jwt.encode(
            {**claims, "token_use": kind, **extra},
            private,
            algorithm="RS256",
            headers={"kid": "synthetic"},
        )

    return httpx.Response(
        200,
        json={
            "access_token": encode("access", {"client_id": config.client_id}),
            "id_token": encode("id", {"aud": config.client_id, "nonce": nonce}),
        },
    )


s3 = base["SyntheticS3"]()
host = HostedRuntime(
    config,
    s3=s3,
    cipher=base["SyntheticCipher"](),
    central=central,
    verifier=verifier,
    send=base["forbidden"],
)
app = host.app()
host.gateway.token_client = httpx.AsyncClient(transport=httpx.MockTransport(tokens))
cloud_entry._cached_runtime = SimpleNamespace(app=lambda: app)
cloud_entry._cached_adapter = None


def gateway(path, query="", cookies=()):
    event = {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": query,
        "cookies": list(cookies),
        "headers": {"host": "synthetic.execute-api.us-east-1.amazonaws.com"},
        "requestContext": {
            "domainName": "synthetic.execute-api.us-east-1.amazonaws.com",
            "stage": "$default",
            "http": {
                "method": "GET",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "synthetic",
            },
        },
        "isBase64Encoded": False,
    }
    return cloud_entry.bff_handler(event, SimpleNamespace())


def cookie_pairs(response):
    values = SimpleCookie()
    for value in response.get("cookies", []):
        values.load(value)
    return [key + "=" + value.value for key, value in values.items() if value.value]


login = gateway("/auth/login")
assert login["statusCode"] == 303 and opaque not in json.dumps(login)
params = parse_qs(urlsplit(login["headers"]["location"]).query)
assert params["nonce"] == [nonce]
query = urlencode({"state": params["state"][0], "code": "synthetic-code"})
transaction = cookie_pairs(login)
callback = gateway("/auth/callback", query, transaction)
assert callback["statusCode"] == 303 and opaque not in json.dumps(callback)
assert len(exchanges) == len(resolutions) == 1
assert gateway("/auth/callback", query, transaction)["statusCode"] == 400
assert len(exchanges) == len(resolutions) == 1
state = gateway("/api/session", cookies=cookie_pairs(callback))
assert (
    state["statusCode"] == 200 and json.loads(state["body"])["personId"] == person["id"]
)
assert opaque.encode() not in b"".join(body for body, _etag in s3.objects.values())
assert host.config.allow_live is False
asyncio.run(host.gateway.token_client.aclose())
print(
    json.dumps(
        {
            "signedCallbackArtifact": "PASS",
            "singleUse": True,
            "pendingEncrypted": True,
            "liveExecution": False,
            "realCloudCalls": False,
            "python": sys.version.split()[0],
            "architecture": platform.machine(),
        }
    )
)
