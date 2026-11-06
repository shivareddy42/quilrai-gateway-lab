"""Bounded provider attempts, conservative accounting, no raw upstream errors."""

import asyncio
import json
from dataclasses import dataclass
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .limiter import SlidingWindow


class CompletionInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    prompt: Annotated[str, Field(min_length=1, max_length=4096)]
    max_tokens: Annotated[int, Field(ge=1, le=4096)] = 128


class Usage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    prompt_tokens: Annotated[int, Field(ge=0)]
    completion_tokens: Annotated[int, Field(ge=0)]
    total_tokens: Annotated[int, Field(ge=0)]


class ProviderResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    text: Annotated[str, Field(max_length=32_768)]
    usage: Usage


@dataclass
class GatewayError(Exception):
    code: str
    status: int = 502
    message: str = "The model provider could not complete this request."


async def bounded_json(response: httpx.Response, limit: int = 65_536):
    data = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=4096):
        data.extend(chunk)
        if len(data) > limit:
            raise GatewayError("upstream_invalid_response")
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise GatewayError("upstream_invalid_response") from exc


class Router:
    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: SlidingWindow,
        primary: str,
        secondary: str,
        timeout: float = 3.0,
    ) -> None:
        self.client, self.limiter = client, limiter
        self.providers = [("primary", primary), ("secondary", secondary)]
        self.timeout = timeout

    async def complete(self, tenant: str, value: CompletionInput, observe=lambda *args: None):
        # Plain text completion adapter: UTF-8 bytes upper-bound prompt tokens
        # for byte-based tokenizers. Output is capped by the provider contract.
        budget = len(value.prompt.encode("utf-8")) + value.max_tokens
        for index, (label, url) in enumerate(self.providers):
            reservation = await self.limiter.reserve(tenant, budget)
            charge = budget
            fallback = False
            try:
                observe("provider_attempt", label)
                # Encloses headers AND body, so a trickle response cannot defeat
                # a per-read timeout. Cancellation completes before fallback.
                async with asyncio.timeout(self.timeout):
                    async with self.client.stream("POST", url, json=value.model_dump()) as response:
                        if response.status_code == 429:
                            charge = 0  # Explicit rejection, no generation accepted.
                            fallback = True
                            observe("provider_429", label)
                        elif response.status_code != 200:
                            raise GatewayError("upstream_unavailable")
                        else:
                            raw = await bounded_json(response)
                            try:
                                result = ProviderResult.model_validate(raw)
                            except ValidationError as exc:
                                raise GatewayError("upstream_invalid_response") from exc
                            usage = result.usage
                            if (
                                usage.total_tokens != usage.prompt_tokens + usage.completion_tokens
                                or usage.total_tokens > budget
                                or usage.prompt_tokens > len(value.prompt.encode("utf-8"))
                                or usage.completion_tokens > value.max_tokens
                            ):
                                raise GatewayError("upstream_invalid_usage")
                            charge = usage.total_tokens
                            return {
                                "text": result.text,
                                "usage": usage.model_dump(),
                                "provider": label,
                            }
            except (TimeoutError, httpx.TimeoutException):
                fallback = True
                observe("provider_timeout", label)
            except httpx.HTTPError as exc:
                # Network failures are sanitized; only 429/timeouts trigger fallback.
                raise GatewayError("upstream_unavailable") from exc
            finally:
                # If client cancellation interrupts this await, the reservation
                # remains charged at its full amount until its crash lease expires.
                await self.limiter.settle(reservation, charge)
            if fallback and index == 0:
                observe("provider_fallback", "secondary")
                continue
            raise GatewayError("providers_exhausted", 503)
        raise GatewayError("providers_exhausted", 503)  # Defensive, never reached.
