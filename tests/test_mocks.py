import httpx

from quilr_gateway.mocks import app


async def test_mock_contracts_and_broken_stream_scenario():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://mock"
    ) as client:
        assert (await client.get("/healthz")).json()["status"] == "ok"
        for method in ("initialize", "tools/list", "tools/call", "ping"):
            response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method})
            assert response.json()["id"] == 1 and "result" in response.json()
        assert (
            await client.post("/mcp", json={"method": "notifications/initialized"})
        ).status_code == 202
        assert (await client.post("/llm/primary", json={"prompt": "[429]"})).status_code == 429
        assert (await client.post("/llm/primary", json={"prompt": "[error]"})).status_code == 500
        response = await client.post("/llm/secondary", json={"prompt": "[429]", "max_tokens": 1})
        assert response.json()["usage"]["completion_tokens"] == 1
        response = await client.post("/llm/stream", json={"prompt": "[broken]"})
        assert response.content.endswith(b"data: {broken}\n\n")
