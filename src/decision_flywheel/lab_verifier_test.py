"""Curation authority through the assumed lab verifier role; fake STS and fake HTTP, no network or credentials."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from .lab_verifier import AssumedRoleSession, lab_verifier_authority, unavailable_authority, valid_verifier_role_arn
from .prospectus_accounts import ProspectusUnavailable
from .publication_lab import PublicationError
from .site_credentials import VerifiedSiteCredential

ROLE = "arn:aws:iam::682849160744:role/cyclotron-lab-verifier"
URL = "https://accounts.example.test/api/"
ACCOUNT = "account-a"
PERSON = "person-1"
START = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


class FakeSTS:
    """assume_role issues credentials 'ASIA<n>' that expire after the requested duration."""

    def __init__(self, clock):
        self.clock, self.calls, self.fail = clock, [], False

    def assume_role(self, **request):
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("sts down: do-not-leak-this")
        n = len(self.calls)
        return {
            "Credentials": {
                "AccessKeyId": f"ASIAVERIFIER{n}",
                "SecretAccessKey": f"fake-secret-{n}",
                "SessionToken": f"fake-session-token-{n}",
                "Expiration": self.clock.now + timedelta(seconds=request["DurationSeconds"]),
            }
        }


class FakeAccountsService:
    """Stands in for the central API behind httpx: records what arrived, answers by editor token."""

    def __init__(self):
        self.requests = []
        self.roles = {"viewer-token": "viewer", "member-token": "member", "owner-token": "owner"}
        self.status = None

    def __call__(self, request):
        self.requests.append(request)
        if self.status:
            return httpx.Response(self.status, json={"error": "x"})
        token = request.headers.get("x-prospectus-human-token")
        if token == "papyrus-unenrolled":
            return httpx.Response(200, json={"person": None}) if request.method == "GET" else httpx.Response(403, json={})
        if token not in self.roles and token != "other-account-token":
            return httpx.Response(401, json={"error": "invalid_human"})
        if request.method == "GET":
            assert request.url.path == "/api/v1/identities/me"
            return httpx.Response(200, json={"person": {"id": PERSON}})
        body = json.loads(request.content)
        account = request.url.path.split("/")[4]
        if token == "other-account-token" or body["operation"] == "console:write" and self.roles[token] == "viewer":
            return httpx.Response(403, json={"error": "forbidden", "message": "Account operation is not permitted."})
        return httpx.Response(
            200,
            json={
                "allowed": True,
                "accountId": account,
                "personId": PERSON,
                "product": "cyclotron",
                "role": self.roles[token],
                "operation": body["operation"],
                "revision": 3,
            },
        )


def build(**overrides):
    clock, service = Clock(), FakeAccountsService()
    sts = FakeSTS(clock)
    settings = {"sts": sts, "client": httpx.Client(transport=httpx.MockTransport(service)), "now": clock}
    authority = lab_verifier_authority(
        overrides.pop("role", ROLE), overrides.pop("url", URL), "us-east-1", **settings, **overrides
    )
    return authority, sts, service, clock


def run(coroutine):
    return asyncio.run(coroutine)


def refusal(authority, token, operation="console:read", account=ACCOUNT):
    async def attempt():
        with pytest.raises(PublicationError) as caught:
            await authority(token, account, operation)
        return caught.value.status, caught.value.code

    return run(attempt())


def test_the_two_verifier_calls_are_signed_with_the_assumed_credentials_and_carry_the_editor_token():
    authority, sts, service, _ = build()
    credential = run(authority("member-token", ACCOUNT, "console:write"))
    assert credential == VerifiedSiteCredential(ACCOUNT, PERSON, "member")
    assert sts.calls == [{"RoleArn": ROLE, "RoleSessionName": "cyclotron-lab-curation", "DurationSeconds": 3600}]
    me, authorize = service.requests
    assert (me.method, me.url.path) == ("GET", "/api/v1/identities/me")
    assert (authorize.method, authorize.url.path) == ("POST", f"/api/v1/accounts/{ACCOUNT}/authorize")
    assert json.loads(authorize.content) == {"operation": "console:write"}
    for request in (me, authorize):
        assert str(request.url).startswith("https://accounts.example.test/api/v1/")
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=ASIAVERIFIER1/")
        assert "execute-api/aws4_request" in request.headers["authorization"] and "us-east-1" in request.headers["authorization"]
        assert request.headers["x-amz-security-token"] == "fake-session-token-1"
        assert request.headers["x-prospectus-human-token"] == "member-token"
        assert "x-prospectus-human-token" in request.headers["authorization"]
    assert "x-prospectus-identity-token" not in me.headers


def test_a_viewer_reads_but_cannot_write_and_each_denial_keeps_its_status_and_code():
    authority, _, service, _ = build()
    assert run(authority("viewer-token", ACCOUNT, "console:read")).role == "viewer"
    assert refusal(authority, "viewer-token", "console:write") == (403, "access_refused")
    assert refusal(authority, "other-account-token") == (403, "access_refused")
    # A person the Papyrus site never enrolled: me answers {"person": null}, so no authorize call is made.
    before = len(service.requests)
    assert refusal(authority, "papyrus-unenrolled") == (401, "bearer_invalid")
    assert len(service.requests) == before + 1
    # A token that is not from a registered Papyrus site is a 401 from me.
    assert refusal(authority, "not-a-papyrus-token") == (401, "bearer_invalid")
    assert [r.url.path for r in service.requests[-1:]] == ["/api/v1/identities/me"]


def test_the_authorized_account_must_be_the_pinned_one():
    service = FakeAccountsService()

    def wrong_account(request):
        response = service(request)
        if request.method == "POST":
            return httpx.Response(200, json=response.json() | {"accountId": "account-b"})
        return response

    clock = Clock()
    authority = lab_verifier_authority(
        ROLE,
        URL,
        "us-east-1",
        sts=FakeSTS(clock),
        client=httpx.Client(transport=httpx.MockTransport(wrong_account)),
        now=clock,
    )
    assert refusal(authority, "member-token") == (403, "access_refused")


def test_an_unavailable_central_service_is_not_reported_as_a_refusal():
    authority, _, service, _ = build()
    service.status = 502

    async def attempt():
        with pytest.raises(ProspectusUnavailable):
            await authority("member-token", ACCOUNT, "console:read")

    run(attempt())


def test_credentials_are_cached_then_renewed_before_they_expire():
    authority, sts, service, clock = build()
    run(authority("member-token", ACCOUNT, "console:read"))
    run(authority("member-token", ACCOUNT, "console:write"))
    assert len(sts.calls) == 1 and len(service.requests) == 4
    clock.now = START + timedelta(minutes=54)  # more than five minutes left: still cached
    run(authority("member-token", ACCOUNT, "console:read"))
    assert len(sts.calls) == 1
    clock.now = START + timedelta(minutes=56)  # inside the five-minute margin: renewed before expiry
    run(authority("member-token", ACCOUNT, "console:read"))
    assert len(sts.calls) == 2
    assert service.requests[-1].headers["x-amz-security-token"] == "fake-session-token-2"
    assert service.requests[-2].headers["x-amz-security-token"] == "fake-session-token-2"
    assert service.requests[1].headers["x-amz-security-token"] == "fake-session-token-1"


def test_a_failed_renewal_keeps_serving_valid_credentials_but_fails_closed_once_they_expire():
    authority, sts, service, clock = build()
    run(authority("member-token", ACCOUNT, "console:read"))
    sts.fail = True
    clock.now = START + timedelta(minutes=57)
    run(authority("member-token", ACCOUNT, "console:read"))
    assert service.requests[-1].headers["x-amz-security-token"] == "fake-session-token-1"
    clock.now = START + timedelta(minutes=61)
    sent = len(service.requests)

    async def attempt():
        with pytest.raises(ProspectusUnavailable) as caught:
            await authority("member-token", ACCOUNT, "console:read")
        assert "do-not-leak-this" not in str(caught.value) and caught.value.__cause__ is None

    run(attempt())
    assert len(service.requests) == sent
    sts.fail = False
    assert run(authority("member-token", ACCOUNT, "console:read")).role == "member"


def test_incomplete_or_naive_sts_answers_are_never_used():
    clock = Clock()
    for bad in (
        {"AccessKeyId": "ASIA", "SecretAccessKey": "s", "SessionToken": "", "Expiration": START + timedelta(hours=1)},
        {"AccessKeyId": "ASIA", "SecretAccessKey": "s", "SessionToken": "t", "Expiration": datetime(2030, 1, 1)},
    ):
        sts = type("Broken", (), {"assume_role": lambda self, **kw: {"Credentials": bad}})()
        with pytest.raises(ProspectusUnavailable):
            AssumedRoleSession(ROLE, sts=sts, now=clock).get_credentials().get_frozen_credentials()


@pytest.mark.parametrize(
    "role,url",
    [
        ("", URL),
        (ROLE, ""),
        ("", ""),
        ("arn:aws:iam::682849160744:role/cyclotron-console-bff-runtime", URL),
        ("arn:aws:iam::682849160744:role/path/cyclotron-lab-verifier", URL),
        (ROLE, "http://accounts.example.test/api"),
        (ROLE, "https://accounts.example.test/api?x=1"),
    ],
)
def test_unset_or_invalid_configuration_fails_closed_without_calling_sts_or_the_network(role, url):
    clock, service = Clock(), FakeAccountsService()
    sts = FakeSTS(clock)
    authority = lab_verifier_authority(
        role, url, "us-east-1", sts=sts, client=httpx.Client(transport=httpx.MockTransport(service)), now=clock
    )

    async def attempt():
        with pytest.raises(ProspectusUnavailable):
            await authority("member-token", ACCOUNT, "console:read")

    run(attempt())
    assert sts.calls == [] and service.requests == []


def test_the_verifier_role_arn_must_be_the_exact_unpathed_role_name():
    assert valid_verifier_role_arn(ROLE)
    assert not any(
        valid_verifier_role_arn(value)
        for value in (None, "", ROLE + "x", ROLE.replace("682849160744", "1"), ROLE.replace("role/", "role/p/"), "x" + ROLE)
    )


def test_the_unavailable_authority_always_raises():
    async def attempt():
        with pytest.raises(ProspectusUnavailable):
            await unavailable_authority()("token", ACCOUNT, "console:read")

    run(attempt())
