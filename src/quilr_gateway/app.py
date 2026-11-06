"""HTTP composition: authentication, MCP policy, streaming guardrail, token router."""

import asyncio
import json
import logging
import sqlite3
import sys
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager

import httpx
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import Settings
from .guardrail import GuardrailError, Redactor, redact_sse
from .limiter import LimitExceeded, SlidingWindow
from .router import CompletionInput, GatewayError, Router, bounded_json

log = logging.getLogger("quilr.gateway")
BODY_LIMIT = 32_768


def reject_constant(value):
    raise ValueError("Non-finite JSON constant")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


async def read_json(request: Request):
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        raise GatewayError("unsupported_media_type", 415, "Use application/json.")
    body = bytearray()
    # Covers slow uploads as well as oversize bodies without trusting Content-Length.
    async with asyncio.timeout(3):
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > BODY_LIMIT:
                raise GatewayError("request_too_large", 413, "Request body exceeds the limit.")
    try:
        return json.loads(body, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise GatewayError("invalid_json", 400, "Request body is not valid JSON.") from exc


def rpc_error(code, message, request_id=None, status=200):
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


def envelope_valid(body):
    return (
        isinstance(body, dict)
        and body.get("jsonrpc") == "2.0"
        and isinstance(body.get("method"), str)
        and ("id" not in body or type(body["id"]) in (str, int))
        and ("params" not in body or isinstance(body["params"], dict))
        and not set(body) - {"jsonrpc", "id", "method", "params"}
    )


def create_app(settings: Settings | None = None, client: httpx.AsyncClient | None = None):
    settings = settings or Settings.from_env()
    metrics = Counter()
    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
    log.setLevel(logging.INFO)

    def observe(event, label="none"):
        # Callers supply only fixed vocabulary. No tenant, token, prompt, or tool args.
        metrics[(event, label)] += 1
        log.info(json.dumps({"event": event, "provider": label}))

    @asynccontextmanager
    async def lifespan(app):
        limiter = SlidingWindow(settings.database)
        owned = client is None
        upstream = client or httpx.AsyncClient(
            timeout=httpx.Timeout(3.0),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
        app.state.upstream = upstream
        app.state.router = Router(
            upstream, limiter, settings.primary_url, settings.secondary_url, settings.timeout
        )
        try:
            yield
        finally:
            if owned:
                await upstream.aclose()

    def identity(request):
        # Duplicate Authorization headers must not have parser-dependent meaning.
        headers = request.headers.getlist("authorization")
        who = settings.authenticate(headers[0]) if len(headers) == 1 else None
        if who is None:
            raise GatewayError("unauthorized", 401, "A valid Bearer token is required.")
        return who

    async def health(request):
        return JSONResponse({"status": "ok"})

    async def metric_endpoint(request):
        if identity(request).role != "admin":
            raise GatewayError("forbidden", 403, "Administrator access is required.")
        lines = ["# TYPE quilr_events_total counter"]
        lines.extend(
            f'quilr_events_total{{event="{event}",provider="{provider}"}} {count}'
            for (event, provider), count in sorted(metrics.items())
        )
        return PlainTextResponse("\n".join(lines) + "\n")

    async def mcp_proxy(request):
        who = identity(request)
        try:
            body = await read_json(request)
        except GatewayError as exc:
            if exc.code == "invalid_json":
                return rpc_error(-32700, "Parse error", status=400)
            raise
        if not envelope_valid(body):
            return rpc_error(-32600, "Invalid Request", status=400)
        request_id = body.get("id")
        notification = "id" not in body

        def reply(code, message):
            return (
                Response(status_code=202) if notification else rpc_error(code, message, request_id)
            )

        method = body["method"]
        if method not in {
            "initialize",
            "ping",
            "notifications/initialized",
            "tools/list",
            "tools/call",
        }:
            return reply(-32601, "Method not found")
        if method == "tools/call":
            name = body.get("params", {}).get("name")
            if not isinstance(name, str) or not name or len(name) > 128:
                return reply(-32602, "Invalid params")
            if name.startswith("admin_") and who.role != "admin":
                observe("mcp_denied")
                return reply(-32001, "Unauthorized Tool Call")
        try:
            async with asyncio.timeout(settings.timeout):
                # Construct a new request. Never forward the caller's token or URL.
                async with request.app.state.upstream.stream(
                    "POST",
                    settings.mcp_url,
                    json=body,
                    headers={"accept": "application/json"},
                ) as response:
                    if notification:
                        if response.status_code not in (200, 202, 204):
                            observe("mcp_upstream_error")
                        return Response(status_code=202)
                    if response.status_code != 200:
                        return rpc_error(-32603, "Upstream unavailable", request_id)
                    result = await bounded_json(response)
            if (
                not isinstance(result, dict)
                or result.get("jsonrpc") != "2.0"
                or type(result.get("id")) is not type(request_id)
                or result.get("id") != request_id
                or ("result" in result) == ("error" in result)
            ):
                return rpc_error(-32603, "Invalid upstream response", request_id)
            observe("mcp_forwarded")
            return JSONResponse(result)
        except (httpx.HTTPError, TimeoutError, GatewayError):
            return rpc_error(-32603, "Upstream unavailable", request_id)

    async def completion_input(request):
        try:
            return CompletionInput.model_validate(await read_json(request))
        except ValidationError as exc:
            raise GatewayError("invalid_request", 400, "Invalid completion parameters.") from exc

    async def complete(request):
        who = identity(request)
        value = await completion_input(request)
        result = await request.app.state.router.complete(who.tenant, value, observe)
        return JSONResponse(result)

    async def stream(request):
        identity(request)
        value = await completion_input(request)
        trace_id = request.state.trace_id

        async def events():
            redactor = Redactor()
            started = time.perf_counter()
            first = True
            try:
                async with asyncio.timeout(30):
                    async with request.app.state.upstream.stream(
                        "POST",
                        settings.stream_url,
                        json=value.model_dump(),
                        headers={"accept": "text/event-stream"},
                    ) as response:
                        if (
                            response.status_code != 200
                            or response.headers.get("content-type", "").split(";")[0]
                            != "text/event-stream"
                        ):
                            raise GuardrailError("Upstream rejected stream")
                        async for safe in redact_sse(response.aiter_bytes(), redactor):
                            if first:
                                log.info(
                                    json.dumps(
                                        {
                                            "event": "stream_first_safe_delta",
                                            "request_id": trace_id,
                                            "elapsed_ms": round(
                                                (time.perf_counter() - started) * 1000, 2
                                            ),
                                        }
                                    )
                                )
                                first = False
                            yield safe
                observe("stream_completed")
            except (GuardrailError, httpx.HTTPError, TimeoutError):
                observe("stream_failed")
                error = {
                    "error": {
                        "code": "stream_unavailable",
                        "message": "The response stream could not be completed.",
                        "request_id": trace_id,
                    }
                }
                yield ("event: error\ndata: " + json.dumps(error) + "\n\n").encode()
            finally:
                metrics[("pii_redactions", "none")] += redactor.redactions
                log.info(
                    json.dumps(
                        {
                            "event": "stream_closed",
                            "request_id": trace_id,
                            "redactions": redactor.redactions,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                        }
                    )
                )

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache, no-transform",
                "x-accel-buffering": "no",
            },
        )

    app = Starlette(
        routes=[
            Route("/healthz", health),
            Route("/metrics", metric_endpoint),
            Route("/mcp", mcp_proxy, methods=["POST"]),
            Route("/v1/completions", complete, methods=["POST"]),
            Route("/v1/stream", stream, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.metrics = metrics

    async def boundary(request, call_next):
        request.state.trace_id = uuid.uuid4().hex
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except LimitExceeded as exc:
            observe("quota_denied")
            response = JSONResponse(
                {
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "Tenant token quota exceeded.",
                        "request_id": request.state.trace_id,
                    }
                },
                status_code=429,
                headers={"retry-after": str(exc.retry_after)},
            )
        except (GatewayError, sqlite3.Error, TimeoutError) as exc:
            if not isinstance(exc, GatewayError):
                exc = GatewayError("gateway_unavailable", 503, "Gateway temporarily unavailable.")
            observe("request_failed")
            response = JSONResponse(
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "request_id": request.state.trace_id,
                    }
                },
                status_code=exc.status,
            )
            if exc.status == 401:
                response.headers["www-authenticate"] = "Bearer"
        except Exception:
            observe("internal_error")
            response = JSONResponse(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "Gateway temporarily unavailable.",
                        "request_id": request.state.trace_id,
                    }
                },
                status_code=500,
            )
        response.headers["x-request-id"] = request.state.trace_id
        log.info(
            json.dumps(
                {
                    "event": "request_headers_ready",
                    "request_id": request.state.trace_id,
                    "status": response.status_code,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
        )
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=boundary)
    return app
