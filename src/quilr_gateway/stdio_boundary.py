"""Preflight errors without replacing the official SDK transport or session.

SDK 1.26 logs malformed lines rather than replying with a parse error. Preflight
also prevents SDK validation exceptions from echoing supplied argument values.
"""

import json

import anyio
from mcp import types
from pydantic import ValidationError


def no_constants(value):
    raise ValueError("Non-finite number")


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


class SerializedOutput:
    def __init__(self, target):
        self.target, self.lock = target, anyio.Lock()

    async def write(self, text):
        async with self.lock:
            await self.target.write(text)
            await self.target.flush()

    async def flush(self):
        # Each whole JSON-RPC line is already written and flushed under the lock.
        pass

    async def error(self, code, message, request_id=None):
        await self.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
            )
            + "\n"
        )


class StrictInput:
    def __init__(self, source, output):
        self.source, self.output = source, output

    async def __aiter__(self):
        async for line in self.source:
            try:
                body = json.loads(
                    line, parse_constant=no_constants, object_pairs_hook=no_duplicate_keys
                )
            except (ValueError, RecursionError):
                await self.output.error(-32700, "Parse error")
                continue
            if (
                not isinstance(body, dict)
                or body.get("jsonrpc") != "2.0"
                or not isinstance(body.get("method"), str)
                or ("id" in body and type(body["id"]) not in (str, int))
                or set(body) - {"jsonrpc", "id", "method", "params"}
            ):
                await self.output.error(-32600, "Invalid Request")
                continue
            notification = "id" not in body
            if not notification and body["method"] not in {
                "initialize",
                "ping",
                "tools/list",
                "tools/call",
            }:
                await self.output.error(-32601, "Method not found", body["id"])
                continue
            try:
                if "params" in body and not isinstance(body["params"], dict):
                    raise ValueError("Invalid params")
                if notification:
                    types.ClientNotification.model_validate(body)
                else:
                    types.ClientRequest.model_validate(body)
            except (ValidationError, ValueError):
                if not notification:
                    await self.output.error(-32602, "Invalid params", body["id"])
                continue
            # SDK owns framing, initialization, dispatch and cancellation from here.
            yield json.dumps(body) + "\n"
