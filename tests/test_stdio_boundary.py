import asyncio
import json
import sys


async def test_raw_protocol_errors_have_standard_codes_and_never_echo_input():
    lines = [
        "{broken-private-input",
        "[]",
        '{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}',
        '{"jsonrpc":"2.0","id":true,"method":"ping"}',
        '{"jsonrpc":"2.0","id":3,"method":"unknown"}',
        '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":123}}',
        '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":[]}',
        '{"jsonrpc":"2.0","method":"tools/call","params":[]}',
    ]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "quilr_gateway.mcp_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(("\n".join(lines) + "\n").encode()), 5
    )
    results = [json.loads(line) for line in stdout.splitlines()]
    assert [value["error"]["code"] for value in results] == [
        -32700,
        -32600,
        -32700,
        -32600,
        -32601,
        -32602,
        -32602,
    ]
    assert [value["id"] for value in results] == [None, None, None, None, 3, 4, 5]
    assert b"private-input" not in stdout + stderr
    assert process.returncode == 0
