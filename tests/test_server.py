import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from qev.schema import NoulAnswer, SystemOneResponse, Usage
from qev.server import create_app


BODY = {"state": "hello", "questions": {"ok": {"type": "noul"}}}


class StubEngine:
    model_type = "stub"
    info = SimpleNamespace(fork_mode="sequential", prompt_version="test")

    def __init__(self):
        self.calls = []
        self.error = None
        self.block = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.thread_ids = []

    def decide(self, request, debias=1, debug=False):
        self.calls.append((request, debias, debug))
        self.thread_ids.append(threading.get_ident())
        self.started.set()
        if self.block:
            self.release.wait(timeout=1)
        if self.error:
            raise self.error
        return SystemOneResponse(
            model="stub", answers={"ok": NoulAnswer(noul=0.75)},
            usage=Usage(input_tokens=10, output_tokens=1),
        ), {"debug": debug}


@pytest.fixture
def engine():
    return StubEngine()


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine, "test-model")) as client:
        yield client


def test_decision_response_and_request_id(client, engine):
    response = client.post("/v1/systemone", json=BODY, headers={"x-typesafe-request-id": "test-123"})
    assert response.status_code == 200
    assert response.json()["model"] == "test-model"
    assert response.json()["answers"]["ok"]["noul"] == 0.75
    assert response.headers["x-typesafe-request-id"] == "test-123"
    assert float(response.headers["x-qev-server-time-ms"]) >= 0
    assert len(engine.calls) == 1


@pytest.mark.parametrize("query", ["debias=no", "debias=0", "debias=-1", "debug=maybe"])
def test_invalid_query_is_validation_error(client, engine, query):
    response = client.post(f"/v1/systemone?{query}", json=BODY)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][0] == "query"
    assert not engine.calls


@pytest.mark.parametrize("debug,expected", [("0", False), ("false", False), ("1", True), ("true", True)])
def test_debug_boolean(client, engine, debug, expected):
    assert client.post(f"/v1/systemone?debug={debug}", json=BODY).status_code == 200
    assert engine.calls[-1][2] is expected


@pytest.mark.parametrize("body", [None, [], {}, {"questions": {}}, {"questions": {"x": {"type": "unknown"}}}])
def test_invalid_body(client, engine, body):
    response = client.post("/v1/systemone", json=body)
    assert response.status_code == 422
    assert not engine.calls


def test_invalid_json(client):
    assert client.post("/v1/systemone", content="{").status_code == 422


def test_unknown_model(client, engine):
    assert client.post("/v1/systemone", json={**BODY, "model": "other"}).status_code == 404
    assert not engine.calls


def test_engine_value_error(client, engine):
    engine.error = ValueError("too many tokens")
    response = client.post("/v1/systemone", json=BODY)
    assert response.status_code == 422
    assert "too many tokens" in response.text


def test_nonfinite_debug_diagnostics_preserve_valid_answers(client, engine):
    original = engine.decide
    diagnostics = {"slot_logits": [0.0, -float("inf")], "allowed_token_mass": float("nan"),
                   "range": (float("inf"), -float("inf"))}

    def decide(*args, **kwargs):
        response, _ = original(*args, **kwargs)
        return response, {"diagnostics": diagnostics}

    engine.decide = decide
    response = client.post("/v1/systemone?debug=true", json=BODY)
    assert response.status_code == 200
    assert response.json()["answers"]["ok"]["noul"] == .75
    assert response.json()["qev"]["diagnostics"] == {
        "slot_logits": [0.0, "-Infinity"], "allowed_token_mass": "NaN", "range": ["Infinity", "-Infinity"]}
    assert diagnostics["slot_logits"][1] == -float("inf")
    assert diagnostics["allowed_token_mass"] != diagnostics["allowed_token_mass"]


def test_inference_runs_off_event_loop_and_leaves_health_available(engine):
    async def run():
        engine.block = True
        app = create_app(engine, "test-model")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            prediction = asyncio.create_task(client.post("/v1/systemone", json=BODY))
            try:
                assert await asyncio.to_thread(engine.started.wait, 2)
                assert not prediction.done()
                assert engine.thread_ids[0] != threading.get_ident()
                assert (await client.get("/health")).status_code == 200
                queued = asyncio.create_task(client.post("/v1/systemone", json=BODY))
                assert (await client.get("/v1/models")).status_code == 200
                assert len(engine.calls) == 1
            finally:
                engine.release.set()
                await prediction
            assert (await queued).status_code == 200
            assert len(engine.calls) == 2

    asyncio.run(run())


def prediction_task(app):
    endpoint = next(route.endpoint for route in app.routes if route.path == "/v1/systemone")

    async def receive():
        return {"type": "http.request", "body": json.dumps(BODY).encode()}

    return asyncio.create_task(endpoint(Request({"type": "http"}, receive), debug=False, debias=1))


@pytest.mark.parametrize("failure", [None, ValueError("invalid input"), RuntimeError("failed forward")])
def test_cancelling_active_request_keeps_engine_reserved_until_worker_finishes(engine, failure):
    async def run():
        engine.block = True
        engine.error = failure
        app = create_app(engine, "test-model")
        active = prediction_task(app)
        queued = None
        try:
            assert await asyncio.to_thread(engine.started.wait, 2)
            active.cancel()
            try:
                await active
            except asyncio.CancelledError:
                pass
            queued = prediction_task(app)
            # Give the next request a chance to enter the model while its first
            # worker is still blocked. Cancelling an await cannot stop a thread.
            await asyncio.sleep(0.05)
            assert len(engine.calls) == 1
            assert not queued.done()
        finally:
            engine.release.set()
            await asyncio.gather(active, *([queued] if queued else []), return_exceptions=True)
        assert len(engine.calls) == 2

    asyncio.run(run())


def test_cancelling_queued_request_does_not_run_it_or_block_the_next_request(engine):
    async def run():
        engine.block = True
        app = create_app(engine, "test-model")
        active = prediction_task(app)
        queued = following = None
        try:
            assert await asyncio.to_thread(engine.started.wait, 2)
            queued = prediction_task(app)
            await asyncio.sleep(0)
            queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)
            following = prediction_task(app)
            await asyncio.sleep(0)
            assert len(engine.calls) == 1
        finally:
            engine.release.set()
            await asyncio.gather(active, *[t for t in (queued, following) if t], return_exceptions=True)
        assert len(engine.calls) == 2
        assert following.result().status_code == 200

    asyncio.run(run())


def test_shutdown_waits_for_inference_abandoned_by_a_cancelled_caller(engine):
    async def run():
        engine.block = True
        app = create_app(engine, "test-model")
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        active = prediction_task(app)
        shutdown = None
        try:
            assert await asyncio.to_thread(engine.started.wait, 2)
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
            await asyncio.sleep(0.05)
            assert not shutdown.done()
        finally:
            engine.release.set()
            await asyncio.gather(active, *([shutdown] if shutdown else []), return_exceptions=True)

    asyncio.run(run())
