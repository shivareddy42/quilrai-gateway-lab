"""Local-only upstreams. No credentials, external APIs, or customer data."""

import asyncio
import json

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


async def health(request):
    return JSONResponse({"status": "ok", "service": "mock-upstreams"})


async def mcp(request):
    value = await request.json()
    if "id" not in value:
        return Response(status_code=202)
    if value["method"] == "tools/list":
        result = {
            "tools": [
                {"name": name, "inputSchema": {"type": "object"}}
                for name in ("get_customer_record", "admin_reset_key")
            ]
        }
    elif value["method"] == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mock-http-mcp", "version": "1.0.0"},
        }
    elif value["method"] == "tools/call":
        result = {"content": [{"type": "text", "text": "Mock tool executed"}]}
    else:
        result = {}
    return JSONResponse({"jsonrpc": "2.0", "id": value["id"], "result": result})


async def llm(request):
    value = await request.json()
    primary = request.path_params["provider"] == "primary"
    if primary and value["prompt"].startswith("[429]"):
        return JSONResponse({"internal": "private-provider-detail"}, status_code=429)
    if primary and value["prompt"].startswith("[timeout]"):
        await asyncio.sleep(3.2)
    if value["prompt"].startswith("[error]"):
        return JSONResponse({"stack": "private-provider-detail"}, status_code=500)
    # Synthetic usage is deliberately deterministic and stays inside the contract.
    prompt_tokens = max(1, len(value["prompt"].encode()) // 4)
    completion_tokens = min(4, value["max_tokens"])
    return JSONResponse(
        {
            "text": "Mock completion.",
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


async def stream(request):
    value = await request.json()

    async def chunks():
        text = (
            "Ready. Email alice@example.com; SSN 123-45-6789; card 4111 1111 1111 1111. Café done."
        )
        # Break both PII and UTF-8 at the HTTP byte boundary.
        for i in range(0, len(text), 3):
            payload = {"choices": [{"index": 0, "delta": {"content": text[i : i + 3]}}]}
            wire = ("data: " + json.dumps(payload, ensure_ascii=False) + "\r\n\r\n").encode()
            for offset in range(0, len(wire), 7):
                yield wire[offset : offset + 7]
            await asyncio.sleep(0.003)
        if value["prompt"].startswith("[broken]"):
            yield b"data: {broken}\n\n"
        else:
            yield b"data: [DONE]\n\n"

    return StreamingResponse(chunks(), media_type="text/event-stream")


app = Starlette(
    routes=[
        Route("/healthz", health),
        Route("/mcp", mcp, methods=["POST"]),
        Route("/llm/stream", stream, methods=["POST"]),
        Route("/llm/{provider}", llm, methods=["POST"]),
    ]
)
