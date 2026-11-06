import httpx
import pytest


def rpc(method="tools/call", name="admin_reset_key", request_id="request-1"):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {"name": name}}


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Basic demo-admin-token",
        "Bearer forged",
        "Bearer demo-admin-token extra",
        "Bearer admin",
    ],
)
async def test_auth_fail_closed(gateway, header):
    calls = []
    async with gateway(lambda request: calls.append(request)) as (client, _):
        headers = {"authorization": header} if header is not None else {}
        response = await client.post("/mcp", headers=headers, json=rpc())
        assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
        assert not calls


async def test_duplicate_authorization_denied(gateway):
    async with gateway(lambda request: pytest.fail("must not call upstream")) as (client, _):
        response = await client.post(
            "/mcp",
            json=rpc(),
            headers=[
                ("authorization", "Bearer demo-viewer-token"),
                ("authorization", "Bearer demo-admin-token"),
            ],
        )
        assert response.status_code == 401


async def test_viewer_admin_call_never_reaches_upstream(gateway, viewer):
    async with gateway(lambda request: pytest.fail("denied request escaped policy")) as (
        client,
        app,
    ):
        response = await client.post("/mcp", headers=viewer, json=rpc())
        assert response.json() == {
            "jsonrpc": "2.0",
            "id": "request-1",
            "error": {"code": -32001, "message": "Unauthorized Tool Call"},
        }
        assert app.state.metrics[("mcp_denied", "none")] == 1


@pytest.mark.parametrize(
    "method,name,role",
    [
        ("tools/list", "ignored", "viewer"),
        ("tools/call", "get_customer_record", "viewer"),
        ("tools/call", "admin_reset_key", "admin"),
        ("tools/call", "ADMIN_reset_key", "viewer"),
    ],
)
async def test_forwarding_preserves_rpc_and_strips_credentials(gateway, method, name, role):
    body = rpc(method, name, 0)
    import json

    def handler(request):
        assert "authorization" not in request.headers
        assert json.loads(request.content) == body
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {"tools": []}})

    async with gateway(handler) as (client, _):
        response = await client.post(
            "/mcp", json=body, headers={"authorization": f"Bearer demo-{role}-token"}
        )
        assert response.json()["result"] == {"tools": []}


@pytest.mark.parametrize(
    "body,code",
    [
        ([], -32600),
        ([rpc()], -32600),
        (None, -32600),
        ({"jsonrpc": "1.0", "id": 1, "method": "tools/list"}, -32600),
        ({"jsonrpc": "2.0", "id": True, "method": "tools/list"}, -32600),
        ({"jsonrpc": "2.0", "id": None, "method": "tools/list"}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 1}}, -32602),
        ({"jsonrpc": "2.0", "id": 1, "method": "execute"}, -32601),
    ],
)
async def test_invalid_envelopes_never_forward(gateway, viewer, body, code):
    async with gateway(lambda request: pytest.fail("invalid payload forwarded")) as (client, _):
        import json

        response = await client.post(
            "/mcp", headers={**viewer, "content-type": "application/json"}, content=json.dumps(body)
        )
        assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    "raw", [b"{broken", b'{"jsonrpc":"2.0","id":1,"id":2}', b'{"id":NaN}', b'"\xff"']
)
async def test_malformed_and_ambiguous_json(gateway, viewer, raw):
    async with gateway(lambda request: pytest.fail("invalid JSON forwarded")) as (client, _):
        response = await client.post(
            "/mcp", content=raw, headers={**viewer, "content-type": "application/json"}
        )
        assert response.json()["error"]["code"] == -32700


async def test_notifications_no_reply_and_policy_still_applies(gateway, viewer):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(202)

    async with gateway(handler) as (client, _):
        body = rpc()
        del body["id"]
        response = await client.post("/mcp", headers=viewer, json=body)
        assert response.status_code == 202 and not response.content and not calls
        body["params"]["name"] = "get_customer_record"
        response = await client.post("/mcp", headers=viewer, json=body)
        assert response.status_code == 202 and len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="private-stack"),
        httpx.Response(200, text="not-json"),
        httpx.Response(302, headers={"location": "http://secret"}),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": "wrong", "result": {}}),
    ],
)
async def test_upstream_faults_are_sanitized(gateway, viewer, response):
    async with gateway(lambda request: response) as (client, _):
        result = await client.post("/mcp", json=rpc("tools/list"), headers=viewer)
        assert result.json()["error"]["code"] == -32603
        assert "private-stack" not in result.text and "secret" not in result.text


async def test_size_media_type_and_metrics_permissions(gateway, viewer, admin):
    async with gateway(lambda request: pytest.fail("must not forward")) as (client, _):
        assert (await client.post("/mcp", content=b"{}", headers=viewer)).status_code == 415
        assert (
            await client.post(
                "/mcp", content=b"x" * 32769, headers={**viewer, "content-type": "application/json"}
            )
        ).status_code == 413
        assert (await client.get("/metrics", headers=viewer)).status_code == 403
        assert (await client.get("/metrics", headers=admin)).status_code == 200
