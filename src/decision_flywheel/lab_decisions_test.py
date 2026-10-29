"""The lab's read-only decisions views over the real hosted API, queue, worker and S3 stores, with offline boundaries."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from .batched_classification import BatchedAnswers
from .hosted_machine_publications_test import body, client, decisions_body, drain, feedback_payload, fixture, revisions
from .lab_decisions import FinishedJobs, RunReader, health
from .models import DecisionResult

identity = __import__("decision_flywheel.account_sessions_test", fromlist=["identity"]).identity
OUTPUTS = ("alpha", "beta")


def answering(model):
    """The offline model, answering from the item text (``p=0.3``) and recording its exact exchange as a provider would."""

    async def classify_many(configs, target, training, **kwargs):
        model.calls.append(tuple(configs))
        text = target.values.get("text", "")
        p = float(text.split("p=")[1].split()[0]) if "p=" in text else 0.8
        label = "include" if p >= 0.5 else "exclude"
        sink = kwargs.get("event_sink") or (lambda event: None)
        sink(
            {"kind": "decision-request", "target_id": target.id, "model": "offline-v1", "state": {"target": dict(target.values)}}
        )
        sink({"kind": "decision-response", "target_id": target.id, "model": "offline-v1", "answers": {"p": p}})
        probabilities = {"include": p, "exclude": round(1 - p, 6)}
        return BatchedAnswers({key: {"decision": DecisionResult(label, probabilities)} for key in configs}, "offline-v1", {}, 1)

    model.classify_many = classify_many


def item(name, p=0.8, **values):
    return {"id": name, "values": {"text": f"Synthetic story {name} p={p} end.", **values}}


class Lab:
    """A signed-in lab member's browser, and Papyrus posting decisions, against one hosted fixture."""

    def __init__(self, identity, monkeypatch):
        parts, self.wrapped = fixture(identity, monkeypatch)
        self.bff, self.worker, self.model, self.store, self.config, self.account, self.cookie, *_ = parts
        self.state, self.sent, self.targets = parts[8], parts[9], parts[10]
        self.run_id = self.targets[0].run_id
        answering(self.model)

    async def post(self, http, story, request_id=None, outputs=OUTPUTS, drain_now=True):
        request_id = request_id or "req-story-" + story["id"]
        posted = await http.post("/machine/decisions", json=decisions_body(revisions(*outputs), story, request_id))
        assert posted.status_code == 202, posted.text
        if drain_now:
            await drain(self.worker, self.sent)
        return posted.json()["jobId"]

    def papyrus(self):
        return client(self.wrapped, self.config, self.account)

    def browser(self, cookie=True):
        http = client(self.wrapped, self.config, self.account, arn=None)
        if cookie:
            http.cookies.set("__Host-cyclotron-session", self.cookie)
        return http

    async def page(self, http, **query):
        response = await http.get(f"/api/lab/runs/{self.run_id}/decisions", params=query)
        assert response.status_code == 200, response.text
        return response.json()

    async def walk(self, http, before=None, **query):
        pages, cursor = [], before
        while True:
            page = await self.page(http, **query, **({"before": cursor} if cursor else {}))
            pages.append(page)
            cursor = page["next"]
            if cursor is None:
                return pages


def run(coroutine):
    return asyncio.run(coroutine)


def test_decisions_appear_newest_first_with_every_outputs_label_confidence_routing_and_label_status(identity, monkeypatch):
    lab = Lab(identity, monkeypatch)

    async def go():
        async with lab.papyrus() as papyrus:
            for name, p in (("first", 0.9), ("second", 0.2), ("third", 0.7)):
                await lab.post(papyrus, item(name, p, title=f"Title {name}", source="feed-a"))
        async with lab.browser() as http:
            return await lab.page(http)

    page = run(go())
    assert [row["item"]["id"] for row in page["decisions"]] == ["third", "second", "first"]
    assert page["next"] is None and page["total"] == 3
    newest = page["decisions"][0]
    assert newest["item"] == {
        "id": "third",
        "revision": 1,
        "fingerprint": newest["item"]["fingerprint"],
        "title": "Title third",
        "source": "feed-a",
    }
    assert set(newest["outputs"]) == set(OUTPUTS)
    alpha = newest["outputs"]["alpha"]
    assert alpha["label"] == "include" and alpha["confidence"] == pytest.approx(0.7) and alpha["binding"] == "alpha"
    assert alpha["review"]["selected"] is True and alpha["humanLabel"] is None
    assert page["decisions"][1]["outputs"]["beta"]["label"] == "exclude"
    times = [row["decidedAt"] for row in page["decisions"]]
    assert times == sorted(times, reverse=True)


def test_following_cursors_walks_every_decision_exactly_once_and_a_new_decision_never_shifts_a_page(identity, monkeypatch):
    lab = Lab(identity, monkeypatch)

    async def go():
        async with lab.papyrus() as papyrus:
            for index in range(7):
                await lab.post(papyrus, item(f"story-{index}"))
            async with lab.browser() as http:
                first = await lab.page(http, pageSize=3)
                # A decision arriving between pages lands above the cursor; the next page is unchanged.
                await lab.post(papyrus, item("late"))
                rest = await lab.walk(http, pageSize=3, before=first["next"])
                again = await lab.page(http, pageSize=3, before=first["next"])
                return first, rest, again

    first, rest, again = run(go())
    seen = [row["item"]["id"] for page in [first, *rest] for row in page["decisions"]]
    assert seen == [f"story-{n}" for n in range(6, -1, -1)]
    assert len(seen) == len(set(seen))
    assert again["decisions"] == rest[0]["decisions"]
    assert [len(page["decisions"]) for page in [first, *rest]] == [3, 3, 1]


def test_filters_by_output_label_confidence_source_and_time_are_applied_at_the_server(identity, monkeypatch):
    lab = Lab(identity, monkeypatch)

    async def go():
        async with lab.papyrus() as papyrus:
            await lab.post(papyrus, item("low", 0.1, source="feed-a"))
            await lab.post(papyrus, item("middle", 0.55, source="feed-b"))
            await lab.post(papyrus, item("high", 0.95, source="feed-a"))
        async with lab.browser() as http:

            async def ids(**query):
                return [row["item"]["id"] for page in await lab.walk(http, pageSize=2, **query) for row in page["decisions"]]

            middle = (await lab.page(http, pageSize=1, before=3))["decisions"][0]["decidedAt"]
            return {
                "exclude": await ids(output="alpha", label="exclude"),
                "include": await ids(label="include"),
                "confident": await ids(minConfidence="0.6"),
                "unsure": await ids(minConfidence="0.5", maxConfidence="0.6"),
                "feed-a": await ids(source="feed-a"),
                "until": await ids(until=middle),
                "since": await ids(since=middle),
                "bad": (await http.get(f"/api/lab/runs/{lab.run_id}/decisions", params={"minConfidence": "2"})).status_code,
                "unknown": (await http.get(f"/api/lab/runs/{lab.run_id}/decisions", params={"colour": "red"})).status_code,
                "output": (await http.get(f"/api/lab/runs/{lab.run_id}/decisions", params={"output": "zeta"})).status_code,
            }

    found = run(go())
    assert found["exclude"] == ["low"]
    assert found["include"] == ["high", "middle"]
    assert found["confident"] == ["high", "low"]  # low: the excluded label is 0.9 sure
    assert found["unsure"] == ["middle"]
    assert found["feed-a"] == ["high", "low"]
    assert found["until"] == ["middle", "low"]
    assert found["since"] == ["high", "middle"]
    assert found["bad"] == found["unknown"] == found["output"] == 400


def test_labeled_unlabeled_and_needs_review_filters_follow_human_labels_per_output(identity, monkeypatch):
    lab = Lab(identity, monkeypatch)

    async def go():
        async with lab.papyrus() as papyrus:
            await lab.post(papyrus, item("hn-1"))
            await lab.post(papyrus, item("hn-2"))
            labeled = await papyrus.post(
                "/machine/publications/alpha/operations", json=body("feedback", feedback_payload(), "fb-req-0001")
            )
            assert labeled.status_code == 200, labeled.text
        async with lab.browser() as http:

            async def ids(**query):
                return [row["item"]["id"] for row in (await lab.page(http, **query))["decisions"]]

            return (
                await ids(output="alpha", labeled="true"),
                await ids(output="alpha", labeled="false"),
                await ids(output="alpha", needsReview="true"),
                await ids(output="beta", needsReview="true"),
                (await lab.page(http))["decisions"][1]["outputs"]["alpha"]["humanLabel"],
            )

    labeled, unlabeled, alpha_review, beta_review, human = run(go())
    assert labeled == ["hn-1"] and unlabeled == ["hn-2"]
    assert alpha_review == ["hn-2"] and beta_review == ["hn-2", "hn-1"]
    assert human == "include"


def test_an_item_detail_carries_its_revisions_every_outputs_decision_the_exact_exchange_and_every_label(identity, monkeypatch):
    lab = Lab(identity, monkeypatch)

    async def go():
        async with lab.papyrus() as papyrus:
            await lab.post(papyrus, item("other"))
            await lab.post(
                papyrus, {"id": "hn-1", "values": {"text": "First version p=0.3 end.", "title": "Story"}}, "req-hn-one"
            )
            job = await lab.post(
                papyrus, {"id": "hn-1", "values": {"text": "Second version p=0.6 end.", "title": "Story"}}, "req-hn-two"
            )
            await lab.post(papyrus, item("after"))
            feedback = feedback_payload(itemRevision=2, comment="Kept after the edit.")
            assert (
                await papyrus.post("/machine/publications/alpha/operations", json=body("feedback", feedback, "fb-req-0009"))
            ).status_code == 200
        async with lab.browser() as http:
            detail = await http.get(f"/api/lab/runs/{lab.run_id}/items/hn-1", params={"job": job})
            missing = await http.get(f"/api/lab/runs/{lab.run_id}/items/hn-1")
            wrong = await http.get(f"/api/lab/runs/{lab.run_id}/items/other", params={"job": job})
            return detail, missing, wrong

    detail, missing, wrong = run(go())
    assert detail.status_code == 200, detail.text
    value = detail.json()
    assert value["item"] == {"id": "hn-1", "revision": 2, "values": {"text": "Second version p=0.6 end.", "title": "Story"}}
    assert [r["revision"] for r in value["revisions"]] == [1, 2]
    assert value["revisions"][0]["values"]["text"] == "First version p=0.3 end."
    assert value["revisions"][1]["provenance"]["kind"] == "machine"
    assert value["revisions"][1]["provenance"]["requestId"] == "machine:req-hn-two"
    assert value["revisions"][0]["provenance"] is None
    decision = value["decision"]
    assert decision["itemRevision"] == 2 and set(decision["outputs"]) == set(OUTPUTS)
    alpha = decision["outputs"]["alpha"]
    assert alpha["probabilities"] == {"include": 0.6, "exclude": 0.4} and alpha["model"] == "offline-v1"
    assert alpha["composition"]["target_field"] == "text"
    # One shared request answered every output: its exact request and response, recorded once.
    assert alpha["exchange"] == decision["outputs"]["beta"]["exchange"] and len(value["exchanges"]) == 1
    exchange = value["exchanges"][alpha["exchange"]]
    assert exchange["request"]["kind"] == "decision-request"
    assert exchange["request"]["state"] == {"target": {"text": "Second version p=0.6 end.", "title": "Story"}}
    assert exchange["response"]["answers"] == {"p": 0.6} and sorted(exchange["outputs"]) == list(OUTPUTS)
    [label] = value["labels"]
    assert label["output"] == "alpha" and label["itemRevision"] == 2 and label["label"] == "include"
    assert label["comment"] == "Kept after the edit." and label["retracted"] is False and label["createdAt"]
    assert label["source"] == {
        "kind": "caller",
        "binding": "alpha",
        "caller": "anth-us",
        "reviewer": "editor-17",
        "asserted": True,
        "principal": "arn:aws:iam::222222222222:role/synthetic-invoker",
    }
    assert missing.status_code == 400 and wrong.status_code == 404


def test_health_counts_recent_decisions_their_spacing_the_queue_and_failed_jobs(identity, monkeypatch):
    from .command_refusal import ProviderRejectedRequest

    lab = Lab(identity, monkeypatch)
    answer = lab.model.classify_many

    async def rejecting(configs, target, training, **kwargs):
        if target.id == "rejected":
            raise ProviderRejectedRequest(400, "req-provider-1")
        return await answer(configs, target, training, **kwargs)

    lab.model.classify_many = rejecting

    async def go():
        async with lab.papyrus() as papyrus:
            for name in ("one", "two", "three", "rejected"):
                await lab.post(papyrus, item(name))
            await lab.post(papyrus, item("waiting"), drain_now=False)
            await lab.post(papyrus, item("waiting-too"), drain_now=False)
        async with lab.browser() as http:
            response = await http.get(f"/api/lab/runs/{lab.run_id}/health")
            assert response.status_code == 200, response.text
            return response.json()

    value = run(go())
    assert value["decisions"] == {"lastHour": 3, "lastDay": 3}
    assert value["failed"] == {"lastDay": 1}
    assert value["queue"] == {"pending": 2, "running": 0}
    spacing = value["secondsBetweenDecisions"]
    assert spacing["samples"] == 2 and 0 <= spacing["median"] <= spacing["p90"]
    assert value["latestPosition"] == 6


def test_health_windows_are_found_by_decision_time_so_older_decisions_are_left_out(identity, monkeypatch):
    lab = Lab(identity, monkeypatch)

    async def go():
        async with lab.papyrus() as papyrus:
            for name in ("a", "b", "c"):
                await lab.post(papyrus, item(name))

    run(go())
    newest = lab.store.run_job_at(lab.run_id, 3)["result"]["response"]["decisionId"]
    assert newest
    store = lab.bff.console(lab.account)
    times = [RunReader(store, lab.run_id).time_at(n) for n in (1, 2, 3)]
    # Seen from a day and a half later, nothing is recent; from just after the second, only the third is.
    later = health(store, lab.run_id, FinishedJobs(), "k", times[2] + timedelta(hours=36))
    assert later["decisions"] == {"lastHour": 0, "lastDay": 0}
    between = health(store, lab.run_id, FinishedJobs(), "k", times[1] + timedelta(hours=1, microseconds=1))
    assert between["decisions"]["lastHour"] == 1 and between["decisions"]["lastDay"] == 3


def test_a_list_pages_storage_reads_do_not_grow_with_the_total_number_of_decisions(identity, monkeypatch):
    lab = Lab(identity, monkeypatch)
    client_s3 = lab.bff.s3

    async def reads_for_first_page(http):
        await lab.page(http, pageSize=3)  # the catalog's own parts are read once per process, whatever its size
        before = sum(1 for kind, _ in client_s3.calls if kind == "get")
        page = await lab.page(http, pageSize=3)
        assert len(page["decisions"]) == 3
        return sum(1 for kind, _ in client_s3.calls if kind == "get") - before

    async def go():
        counts = []
        async with lab.papyrus() as papyrus:
            for total in (4, 16):
                while lab.store.run_job_count(lab.run_id) < total:
                    await lab.post(papyrus, item(f"story-{lab.store.run_job_count(lab.run_id)}"))
                async with lab.browser() as http:
                    counts.append(await reads_for_first_page(http))
        return counts

    small, large = run(go())
    assert small == large


def test_the_routes_need_a_signed_in_reader_of_the_account_and_a_configured_standing_run(identity, monkeypatch):
    lab = Lab(identity, monkeypatch)

    async def go():
        async with lab.papyrus() as papyrus:
            await lab.post(papyrus, item("one"))
        paths = [f"/api/lab/runs/{lab.run_id}/decisions", f"/api/lab/runs/{lab.run_id}/health", "/api/lab/standing-runs"]
        async with lab.browser(cookie=False) as http:
            anonymous = [(await http.get(path)).status_code for path in paths]
        async with lab.browser() as http:
            listed = (await http.get("/api/lab/standing-runs")).json()
            unknown = (await http.get(f"/api/lab/runs/{lab.account}/decisions")).status_code
            lab.state["denied"] = True
            denied = [(await http.get(path)).status_code for path in paths]
        return anonymous, listed, unknown, denied

    anonymous, listed, unknown, denied = run(go())
    # A revoked membership fails the fresh identity check, which also ends the session.
    assert anonymous == [401, 401, 401] and denied == [401, 401, 401] and unknown == 404
    [standing] = listed
    assert standing["runId"] == lab.run_id and [o["id"] for o in standing["outputs"]] == ["alpha", "beta", "gamma", "delta"]
    assert standing["outputs"][0]["classes"] == ["include", "exclude"]


def test_a_standing_run_names_the_cyclotron_definition_it_is_pinned_to_and_its_bindings(identity, monkeypatch):
    lab = Lab(identity, monkeypatch)

    async def go():
        async with lab.browser() as http:
            return (await http.get("/api/lab/standing-runs")).json()

    [standing] = run(go())
    pin = lab.store.run_cyclotron_definition(lab.run_id)
    assert standing["cyclotron"] == {"id": "shared", "revision": 1} == {"id": pin["id"], "revision": pin["revision"]}
    # Each binding is a named consumer of some outputs: a site, an inbox, a sales team. No publication fields.
    assert standing["bindings"] == [{"id": b.publication_id, "outputs": list(b.outputs)} for b in lab.targets[0].bindings]
    assert "publications" not in standing


def test_reading_decisions_details_and_health_writes_nothing(identity, monkeypatch):
    lab = Lab(identity, monkeypatch)

    async def go():
        async with lab.papyrus() as papyrus:
            job = await lab.post(papyrus, item("one"))
            await lab.post(papyrus, item("two"))
        puts = sum(1 for kind, _ in lab.bff.s3.calls if kind == "put")
        async with lab.browser() as http:
            for path, query in (
                ("decisions", {}),
                ("decisions", {"labeled": "false", "since": datetime.now(timezone.utc).isoformat()}),
                ("items/one", {"job": job}),
                ("health", {}),
            ):
                assert (await http.get(f"/api/lab/runs/{lab.run_id}/{path}", params=query)).status_code == 200
            assert (await http.get("/api/lab/standing-runs")).status_code == 200
        return puts, sum(1 for kind, _ in lab.bff.s3.calls if kind == "put")

    before, after = run(go())
    assert before == after
