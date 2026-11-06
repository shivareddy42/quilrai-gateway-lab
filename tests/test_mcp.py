import asyncio
import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from quilr_gateway.mcp_server import CustomerInput, RefundInput


@pytest.mark.parametrize("customer_id", ["CUST-00001", "CUST-99999"])
def test_customer_valid(customer_id):
    assert CustomerInput(customer_id=customer_id).customer_id == customer_id


@pytest.mark.parametrize(
    "customer_id",
    [
        "CUST-1234",
        "CUST-123456",
        "cust-12345",
        "CUST-１２３４５",
        "CUST-12345\n",
        " CUST-12345",
        12345,
        None,
        True,
        "",
    ],
)
def test_customer_invalid(customer_id):
    with pytest.raises(ValidationError):
        CustomerInput(customer_id=customer_id)


@pytest.mark.parametrize("amount", [0, -0.1, True, "1.0", float("inf"), float("nan"), None])
def test_refund_amount_rejects_coercion_and_nonfinite(amount):
    with pytest.raises(ValidationError):
        RefundInput(customer_id="CUST-00001", amount=amount, reason="Duplicate purchase")


@pytest.mark.parametrize("reason", ["too short", " " * 10, "         a", 123, None, "a" * 1001])
def test_refund_reason(reason):
    with pytest.raises(ValidationError):
        RefundInput(customer_id="CUST-00001", amount=1.25, reason=reason)


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        CustomerInput(customer_id="CUST-00001", admin=True)


async def test_official_client_handshake_schema_and_rpc_errors(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "quilr_gateway.mcp_server"])
    with (tmp_path / "stderr.log").open("w+") as logs:
        async with stdio_client(params, errlog=logs) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                listed = await client.list_tools()
                assert {t.name for t in listed.tools} == {"get_customer_record", "trigger_refund"}
                assert all(t.inputSchema["additionalProperties"] is False for t in listed.tools)
                result = await client.call_tool(
                    "trigger_refund",
                    {"customer_id": "CUST-00001", "amount": 1.25, "reason": "Duplicate purchase"},
                )
                assert result.structuredContent["status"] == "simulated"
                for name, arguments in [
                    ("get_customer_record", {"customer_id": "bad"}),
                    (
                        "trigger_refund",
                        {
                            "customer_id": "CUST-00001",
                            "amount": True,
                            "reason": "Duplicate purchase",
                        },
                    ),
                    ("unknown", {}),
                ]:
                    with pytest.raises(McpError) as error:
                        await client.call_tool(name, arguments)
                    assert error.value.error.code == -32602
                    assert "bad" not in error.value.error.message
        logs.seek(0)
        assert "tool_completed" in logs.read()


async def test_stdout_every_line_is_jsonrpc_and_logs_are_stderr():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "quilr_gateway.mcp_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    seen = []

    async def exchange(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()
        if "id" in message:
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), 5)
                value = json.loads(line)  # Any stray print makes this test fail.
                assert value["jsonrpc"] == "2.0"
                seen.append(value)
                if value.get("id") == message["id"]:
                    return value

    try:
        await exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "wire-test", "version": "1"},
                },
            }
        )
        await exchange({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        result = await exchange(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_customer_record",
                    "arguments": {"customer_id": "CUST-00001"},
                },
            }
        )
        assert "result" in result
        result = await exchange(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "get_customer_record",
                    "arguments": {"customer_id": "CUST-00001", "extra": 1},
                },
            }
        )
        assert result["error"]["code"] == -32602
    finally:
        process.stdin.close()
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
    for line in stdout.splitlines():
        assert json.loads(line)["jsonrpc"] == "2.0"
    assert b"tool_completed" in stderr and len(seen) == 4
    assert process.returncode == 0
