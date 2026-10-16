"""Bounded deployed HTTP checks; never prints OAuth state, cookies or tokens."""

import argparse
import json
import re
from urllib.parse import parse_qs, urlparse

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin")
    parser.add_argument("--signed-out-only", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(
        r"https://(?:[a-z0-9]+\.execute-api\.us-east-1\.amazonaws\.com|lab\.cyclotron\.anth\.us)",
        args.origin,
    ):
        raise ValueError("fixed reviewed origin required")

    with httpx.Client(base_url=args.origin, timeout=35, follow_redirects=False) as client:
        response = client.get("/api/session")
        assert response.status_code == 200, (response.status_code, "session")
        assert response.json() == {"authenticated": False, "loginUrl": "/auth/login"}
        assert "no-store" in response.headers.get("cache-control", "")
        response = client.post("/graphql", json={"query": "{runs{id}}"})
        assert response.status_code == 401, (response.status_code, "logged-out GraphQL")
        response = client.get(
            "/api/runs/11111111-1111-4111-8111-111111111111/events",
            params={"account": "22222222-2222-4222-8222-222222222222"},
        )
        assert response.status_code == 401, (response.status_code, "logged-out tenant events")

        if not args.signed_out_only:
            response = client.get("/auth/login")
            assert response.status_code in (302, 303, 307), (response.status_code, "login initiation")
            location = urlparse(response.headers["location"])
            query = parse_qs(location.query)
            assert location.scheme == "https"
            assert location.hostname == "prospectus-712236451410.auth.us-east-1.amazoncognito.com"
            assert query["client_id"] == ["2uhgmi3c69nobpiauichdsdmo3"]
            assert query["redirect_uri"] == ["https://lab.cyclotron.anth.us/auth/callback"]
            assert query["response_type"] == ["code"]
            assert query["code_challenge_method"] == ["S256"]
            assert all(query.get(key) for key in ("nonce", "state", "code_challenge"))
            assert client.cookies, "no browser binding cookie"
            assert "no-store" in response.headers.get("cache-control", "")

    print(json.dumps({
        "origin": args.origin,
        "signedOutDenied": True,
        "loginInitiation": "not checked" if args.signed_out_only else "PASS",
        "humanGoogleVerified": False,
        "providerCalls": 0,
    }))


if __name__ == "__main__":
    main()
