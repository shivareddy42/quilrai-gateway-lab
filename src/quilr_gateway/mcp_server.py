"""Official SDK session and stdio framing, strict application validation."""

import asyncio
import json
import logging
import sys
from io import TextIOWrapper
from typing import Annotated

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .stdio_boundary import SerializedOutput, StrictInput


class CustomerInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    customer_id: Annotated[str, Field(pattern=r"^CUST-[0-9]{5}$", max_length=10)]


class RefundInput(CustomerInput):
    amount: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    reason: Annotated[str, Field(min_length=10, max_length=1000)]

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if len(value.strip()) < 10:
            raise ValueError("reason must contain at least 10 non-padding characters")
        return value


SCHEMAS = {"get_customer_record": CustomerInput, "trigger_refund": RefundInput}
server = Server("quilr-customer-tools", version="1.0.0")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=name,
            description=(
                "Read a synthetic customer record."
                if name == "get_customer_record"
                else "Simulate a refund. No money moves."
            ),
            inputSchema=schema.model_json_schema(),
            annotations=types.ToolAnnotations(
                readOnlyHint=name == "get_customer_record",
                destructiveHint=name == "trigger_refund",
            ),
        )
        for name, schema in SCHEMAS.items()
    ]


async def call_tool(request: types.CallToolRequest) -> types.ServerResult:
    """Use the SDK handler registry to preserve protocol errors as JSON-RPC errors.

    SDK 1.26's convenience call_tool decorator catches all exceptions and converts
    them to isError tool results. The assessment explicitly asks for -32602.
    """
    schema = SCHEMAS.get(request.params.name)
    if schema is None:
        raise McpError(types.ErrorData(code=-32602, message="Unknown tool"))
    try:
        value = schema.model_validate(request.params.arguments or {})
    except ValidationError as exc:
        # Do not echo invalid values or refund reasons in errors.
        raise McpError(types.ErrorData(code=-32602, message="Invalid tool arguments")) from exc
    if isinstance(value, RefundInput):
        result = {
            "customer_id": value.customer_id,
            "amount": value.amount,
            "status": "simulated",
        }
    else:
        result = {"customer_id": value.customer_id, "name": "Demo Customer", "tier": "standard"}
    logging.getLogger(__name__).info("tool_completed tool=%s", request.params.name)
    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result))],
            structuredContent=result,
        )
    )


server.request_handlers[types.CallToolRequest] = call_tool


async def run() -> None:
    stdin = anyio.wrap_file(TextIOWrapper(sys.stdin.buffer, encoding="utf-8"))
    stdout = SerializedOutput(anyio.wrap_file(TextIOWrapper(sys.stdout.buffer, encoding="utf-8")))
    async with stdio_server(stdin=StrictInput(stdin, stdout), stdout=stdout) as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s", force=True)
    # Keep SDK diagnostic exceptions from ever echoing payloads in application logs.
    logging.getLogger("mcp").setLevel(logging.CRITICAL)
    # Only JSON-RPC messages may pass through the serialized stdout writer.
    asyncio.run(run())


if __name__ == "__main__":
    main()
