import asyncio
import json
import sqlite3
import time
from contextlib import closing

import httpx
import pytest

from quilr_gateway.limiter import LimitExceeded, SlidingWindow
from quilr_gateway.router import CompletionInput, Router


def success(total=4):
    return {
        "text": "Completed.",
        "usage": {"prompt_tokens": 2, "completion_tokens": total - 2, "total_tokens": total},
    }


def used(path):
    with closing(sqlite3.connect(path)) as db, db:
        return db.execute("SELECT COALESCE(SUM(tokens),0) FROM ledger").fetchone()[0]


async def test_primary_success_reconciles_actual_usage(tmp_path):
    path = str(tmp_path / "quota.sqlite3")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=success())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = Router(client, SlidingWindow(path), "http://mock/primary", "http://mock/secondary")
        result = await router.complete("a", CompletionInput(prompt="hello", max_tokens=10))
    assert result["provider"] == "primary" and calls == ["/primary"]
    assert used(path) == 4


async def test_429_releases_primary_reservation_before_backup(tmp_path):
    path = str(tmp_path / "quota.sqlite3")

    def handler(request):
        if request.url.path == "/primary":
            return httpx.Response(429, text="secret-api-key-in-provider-stack")
        return httpx.Response(200, json=success())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = Router(
            client, SlidingWindow(path, limit=15), "http://mock/primary", "http://mock/secondary"
        )
        result = await router.complete("a", CompletionInput(prompt="hello", max_tokens=10))
    assert result["provider"] == "secondary" and used(path) == 4


async def test_timeout_cancels_primary_before_backup_and_keeps_ambiguous_spend(tmp_path):
    path = str(tmp_path / "quota.sqlite3")
    cancelled = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/primary":
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        assert cancelled.is_set(), "Backup must not race an uncancelled primary task"
        return httpx.Response(200, json=success())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = Router(
            client,
            SlidingWindow(path),
            "http://mock/primary",
            "http://mock/secondary",
            timeout=0.02,
        )
        result = await router.complete("a", CompletionInput(prompt="hello", max_tokens=10))
    assert result["provider"] == "secondary" and calls == ["/primary", "/secondary"]
    assert used(path) == 15 + 4


class SlowBody(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        for byte in json.dumps(success()).encode():
            await asyncio.sleep(0.01)
            yield bytes([byte])

    async def aclose(self):
        self.closed = True


async def test_deadline_includes_trickling_body_and_closes_response(tmp_path):
    body = SlowBody()

    def handler(request):
        if request.url.path == "/primary":
            return httpx.Response(200, stream=body)
        assert body.closed
        return httpx.Response(200, json=success())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = Router(
            client,
            SlidingWindow(str(tmp_path / "quota.sqlite3")),
            "http://mock/primary",
            "http://mock/secondary",
            timeout=0.03,
        )
        started = time.perf_counter()
        result = await router.complete("a", CompletionInput(prompt="hello"))
        assert result["provider"] == "secondary" and body.closed
        assert time.perf_counter() - started < 1


async def test_default_timeout_is_three_seconds(tmp_path):
    async with httpx.AsyncClient() as client:
        router = Router(client, SlidingWindow(str(tmp_path / "quota.sqlite3")), "a", "b")
        assert router.timeout == 3.0


async def test_backup_cannot_escape_quota_after_timeout(tmp_path):
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        await asyncio.sleep(10)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = Router(
            client,
            SlidingWindow(str(tmp_path / "quota.sqlite3"), limit=15),
            "http://mock/primary",
            "http://mock/secondary",
            timeout=0.01,
        )
        with pytest.raises(LimitExceeded):
            await router.complete("a", CompletionInput(prompt="hello", max_tokens=10))
    assert calls == ["/primary"]


async def test_client_cancellation_does_not_fallback_or_release_ambiguous_quota(tmp_path):
    path = str(tmp_path / "quota.sqlite3")
    entered, calls = asyncio.Event(), []

    async def handler(request):
        calls.append(request.url.path)
        entered.set()
        await asyncio.sleep(10)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = Router(client, SlidingWindow(path), "http://mock/primary", "http://mock/secondary")
        task = asyncio.create_task(
            router.complete("a", CompletionInput(prompt="hello", max_tokens=10))
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == ["/primary"] and used(path) == 15


@pytest.mark.parametrize("status", [400, 401, 403, 500, 502, 503, 302])
async def test_non_429_http_failure_no_fallback(gateway, viewer, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private-stack-and-token")

    async with gateway(handler) as (client, _):
        response = await client.post("/v1/completions", headers=viewer, json={"prompt": "hello"})
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "upstream_unavailable"
        assert "private-stack" not in response.text and len(calls) == 1
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


@pytest.mark.parametrize("failure", ["429", "timeout", "network", "invalid_json", "oversize"])
async def test_exhaustion_and_transport_errors_sanitized(gateway, viewer, failure):
    async def handler(request):
        if failure == "429":
            return httpx.Response(429, text="private-stack")
        if failure == "timeout":
            raise httpx.ReadTimeout("private-stack")
        if failure == "network":
            raise httpx.ConnectError("private-stack")
        if failure == "oversize":
            return httpx.Response(200, content=b"x" * 65537)
        return httpx.Response(200, text="private-stack")

    async with gateway(handler) as (client, _):
        response = await client.post("/v1/completions", headers=viewer, json={"prompt": "hello"})
        assert response.status_code in (502, 503) and "private-stack" not in response.text
        assert set(response.json()) == {"error"}


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 5},
        {"prompt_tokens": 500, "completion_tokens": 2, "total_tokens": 502},
        {"prompt_tokens": 2, "completion_tokens": 500, "total_tokens": 502},
        {"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1},
        {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3},
        {"prompt_tokens": 2, "completion_tokens": 2},
    ],
)
async def test_untrusted_usage_cannot_credit_quota(gateway, viewer, usage):
    async with gateway(
        lambda request: httpx.Response(200, json={"text": "ok", "usage": usage})
    ) as (client, app):
        response = await client.post("/v1/completions", headers=viewer, json={"prompt": "hello"})
        assert response.status_code == 502
        assert used(app.state.router.limiter.path) == 133


async def test_http_quota_identity_and_error_envelope(gateway, viewer):
    async with gateway(lambda request: httpx.Response(200, json=success())) as (client, app):
        await app.state.router.limiter.reserve("tenant-a", 50_000)
        response = await client.post("/v1/completions", headers=viewer, json={"prompt": "hello"})
        assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
        assert response.json()["error"]["code"] == "rate_limit_exceeded"
        response = await client.post(
            "/v1/completions",
            json={"prompt": "hello"},
            headers={"authorization": "Bearer demo-other-tenant-token"},
        )
        assert response.status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": ""},
        {"prompt": 12},
        {"prompt": "a" * 4097},
        {"prompt": "hello", "max_tokens": True},
        {"prompt": "hello", "max_tokens": 0},
        {"prompt": "hello", "max_tokens": "10"},
        {"prompt": "hello", "tenant": "victim"},
        {"prompt": "hello", "url": "http://private-network"},
    ],
)
async def test_input_cannot_spoof_tenant_or_select_upstream(gateway, viewer, payload):
    async with gateway(lambda request: pytest.fail("invalid input forwarded")) as (client, _):
        response = await client.post("/v1/completions", headers=viewer, json=payload)
        assert response.status_code == 400


async def test_database_outage_returns_sanitized_503(gateway, viewer):
    async with gateway(lambda request: pytest.fail("quota failure must not dispatch")) as (
        client,
        app,
    ):
        with closing(sqlite3.connect(app.state.router.limiter.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            response = await client.post(
                "/v1/completions", headers=viewer, json={"prompt": "hello"}
            )
        assert response.status_code == 503
        assert "locked" not in response.text and "sqlite" not in response.text


async def test_utf8_byte_budget_prevents_multibyte_underreservation(tmp_path):
    path = str(tmp_path / "quota.sqlite3")

    def handler(request):
        assert used(path) == len("你好".encode()) + 10
        return httpx.Response(200, json=success())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = Router(client, SlidingWindow(path), "http://mock/primary", "http://mock/secondary")
        await router.complete("a", CompletionInput(prompt="你好", max_tokens=10))


async def test_internal_errors_sanitized(gateway, viewer):
    def handler(request):
        raise RuntimeError("private-stack")

    async with gateway(handler) as (client, _):
        response = await client.post("/v1/completions", headers=viewer, json={"prompt": "hello"})
        assert response.status_code == 500 and "private-stack" not in response.text
