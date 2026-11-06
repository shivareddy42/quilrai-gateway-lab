"""Run a real network demo in isolated subprocesses and clean up every child."""

import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .guardrail import SSEDecoder


def free_port():
    # There is a small bind race between discovery and child startup; readiness
    # checks fail explicitly if another process wins. Never kill a foreign PID.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def ready(client, base, child):
    for _ in range(100):
        if child.returncode is not None:
            raise RuntimeError("Demo server exited before readiness")
        try:
            if (await client.get(base + "/healthz")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.05)
    raise RuntimeError("Demo server did not become ready")


async def exercise(gateway, client):
    params = StdioServerParameters(command=sys.executable, args=["-m", "quilr_gateway.mcp_server"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
        assert result.structuredContent["customer_id"] == "CUST-00001"
        print("PASS  Task 1: official SDK client completed a stdio tool call.")

    headers = {"authorization": "Bearer demo-viewer-token"}
    rpc = {
        "jsonrpc": "2.0",
        "id": "demo-1",
        "method": "tools/call",
        "params": {"name": "admin_reset_key", "arguments": {}},
    }
    response = await client.post(gateway + "/mcp", headers=headers, json=rpc)
    assert response.json()["error"]["code"] == -32001
    response = await client.post(
        gateway + "/mcp", json=rpc, headers={"authorization": "Bearer demo-admin-token"}
    )
    assert "result" in response.json()
    print("PASS  Task 2: viewer blocked, administrator forwarded.")

    started = time.perf_counter()
    first_ms = None
    decoder, text = SSEDecoder(), ""
    async with client.stream(
        "POST", gateway + "/v1/stream", headers=headers, json={"prompt": "demo"}
    ) as response:
        assert response.status_code == 200
        async for chunk in response.aiter_bytes():
            for event in decoder.feed(chunk):
                if event != "[DONE]":
                    if first_ms is None:
                        first_ms = round((time.perf_counter() - started) * 1000, 2)
                    text += json.loads(event)["choices"][0]["delta"]["content"]
    assert all(pii not in text for pii in ("alice@example.com", "123-45-6789", "4111"))
    assert text.count("[REDACTED]") == 3
    print(f"PASS  Task 3: {text}")
    print(f"      Observed first safe content: {first_ms} ms on this run.")

    for prompt in ("normal", "[429] force fallback", "[timeout] force fallback"):
        started = time.perf_counter()
        response = await client.post(
            gateway + "/v1/completions", headers=headers, json={"prompt": prompt}
        )
        expected = "primary" if prompt == "normal" else "secondary"
        assert response.status_code == 200 and response.json()["provider"] == expected
        print(f"PASS  Task 4: {prompt!r} -> {expected} ({time.perf_counter() - started:.2f}s)")
    response = await client.post(
        gateway + "/v1/completions", headers=headers, json={"prompt": "[error] sanitize"}
    )
    assert response.status_code == 502 and "private-provider-detail" not in response.text
    print("PASS  Provider failure returned a sanitized gateway error.")
    response = await client.get(
        gateway + "/metrics", headers={"authorization": "Bearer demo-admin-token"}
    )
    assert "provider_fallback" in response.text
    print("PASS  Authenticated metrics captured fallback and policy events.")


async def run():
    children = []
    with tempfile.TemporaryDirectory(prefix="quilr-demo-") as directory:
        upstream_port, gateway_port = free_port(), free_port()
        while gateway_port == upstream_port:
            gateway_port = free_port()
        upstream, gateway = f"http://127.0.0.1:{upstream_port}", f"http://127.0.0.1:{gateway_port}"
        env = {
            **os.environ,
            "QUILR_DEMO": "1",
            "QUILR_DATABASE": directory + "/quota.sqlite3",
            "QUILR_MCP_URL": upstream + "/mcp",
            "QUILR_STREAM_URL": upstream + "/llm/stream",
            "QUILR_PRIMARY_URL": upstream + "/llm/primary",
            "QUILR_SECONDARY_URL": upstream + "/llm/secondary",
        }
        # A configured real token map must not silently override the demo credentials.
        env.pop("QUILR_TOKENS_JSON", None)
        with (Path(directory) / "servers.log").open("w+") as logs:
            try:
                for target, port, factory in [
                    ("quilr_gateway.mocks:app", upstream_port, False),
                    ("quilr_gateway.app:create_app", gateway_port, True),
                ]:
                    args = [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        target,
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--log-level",
                        "warning",
                        "--no-access-log",
                    ]
                    if factory:
                        args.append("--factory")
                    children.append(
                        await asyncio.create_subprocess_exec(
                            *args, env=env, stdout=logs, stderr=logs
                        )
                    )
                async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                    await ready(client, upstream, children[0])
                    await ready(client, gateway, children[1])
                    await exercise(gateway, client)
            except BaseException:
                logs.flush()
                logs.seek(0)
                print(logs.read(), file=sys.stderr)
                raise
            finally:
                for child in children:
                    if child.returncode is None:
                        child.terminate()
                for child in children:
                    try:
                        await asyncio.wait_for(child.wait(), 5)
                    except TimeoutError:
                        child.kill()
                        await child.wait()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
