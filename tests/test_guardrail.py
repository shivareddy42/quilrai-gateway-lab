import asyncio
import json

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quilr_gateway.guardrail import (
    GuardrailError,
    Redactor,
    SSEDecoder,
    content_event,
    redact_sse,
)

PII = [
    "alice@example.com",
    "first.last+tag@sub.example.co.uk",
    "123-45-6789",
    "4111111111111111",
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "378282246310005",
    "123456789",
    "123 45 6789",
    "411111111111111111111",
]


@pytest.mark.parametrize("pii", PII)
def test_every_text_split_and_no_partial_candidate_release(pii):
    for split in range(len(pii) + 1):
        redactor = Redactor()
        first = redactor.feed("Before; " + pii[:split])
        assert first == "Before; "
        result = first + redactor.feed(pii[split:] + "; after.") + redactor.finish()
        assert result == "Before; [REDACTED]; after."


@given(st.lists(st.integers(min_value=1, max_value=23), min_size=1, max_size=50))
def test_redaction_is_independent_of_chunk_partition(widths):
    text = "Hello; " + "; ".join(PII) + "; café, finished."
    reference = Redactor()
    expected = reference.feed(text) + reference.finish()
    actual, offset, redactor = "", 0, Redactor()
    for width in widths:
        actual += redactor.feed(text[offset : offset + width])
        offset += width
    actual += redactor.feed(text[offset:]) + redactor.finish()
    assert actual == expected


def test_bounded_memory_and_fail_closed_for_unbounded_candidates():
    redactor = Redactor()
    emitted = []
    for _ in range(5000):
        emitted.append(redactor.feed("a" * 100))
    emitted.append(redactor.feed("@example.com; "))
    emitted.append(redactor.finish())
    assert "".join(emitted) == "[REDACTED]; "
    assert redactor.email.peak <= 254 and redactor.number.peak <= 64
    number = Redactor()
    assert (number.feed("1 " * 10000 + ";") + number.finish()) == "[REDACTED];"


def test_low_latency_at_safe_delimiters_and_eof_flush():
    redactor = Redactor()
    assert redactor.feed("Hello ") == "Hello "
    assert redactor.feed("world") == ""
    assert redactor.finish() == "world"
    redactor = Redactor()
    assert redactor.feed("alice@example.com") == ""
    assert redactor.finish() == "[REDACTED]"


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_sse_every_byte_split_utf8_comments_and_multiline(ending):
    wire = (
        f": heartbeat{ending}event: message{ending}data: café{ending}data: second{ending}{ending}"
    ).encode()
    for split in range(len(wire) + 1):
        parser = SSEDecoder()
        assert parser.feed(wire[:split]) + parser.feed(wire[split:]) == ["café\nsecond"]
        parser.feed(b"", final=True)


def test_parser_caps_even_ignored_fields_and_detects_incomplete_input():
    for wire in (b"data: " + b"x" * 100, b":" + b"x" * 100, b"id: " + b"x" * 100):
        with pytest.raises(GuardrailError):
            SSEDecoder(limit=64).feed(wire)
    with pytest.raises(GuardrailError):
        SSEDecoder().feed(b"data: pending", final=True)
    with pytest.raises(GuardrailError):
        SSEDecoder().feed(b"data: \xff\n\n")


def wire_for(parts):
    return b"".join(content_event(part) for part in parts) + b"data: [DONE]\n\n"


async def source(chunks):
    for chunk in chunks:
        yield chunk


def rendered(chunks):
    parser, output = SSEDecoder(), ""
    done = False
    for chunk in chunks:
        for event in parser.feed(chunk):
            if event == "[DONE]":
                done = True
            else:
                output += json.loads(event)["choices"][0]["delta"]["content"]
    assert done
    return output


@pytest.mark.parametrize("pii", PII[:4])
async def test_cross_delta_and_transport_splits(pii):
    for split in range(1, len(pii)):
        wire = wire_for(["Safe; ", pii[:split], pii[split:], "; done"])
        # One-byte chunks split the SSE prefix, JSON escapes, payload, UTF-8 and terminator.
        chunks = [bytes([byte]) for byte in wire]
        safe = [part async for part in redact_sse(source(chunks), Redactor())]
        assert rendered(safe) == "Safe; [REDACTED]; done"


@settings(max_examples=30)
@given(st.lists(st.integers(min_value=1, max_value=100), min_size=1, max_size=40))
def test_sse_partition_property(widths):
    wire = wire_for(["Hello; café; ", "alice@", "example.com", "; 123-45-6789; done."])
    decoder, actual, offset = SSEDecoder(), [], 0
    for width in widths:
        actual += decoder.feed(wire[offset : offset + width])
        offset += width
    actual += decoder.feed(wire[offset:])
    expected = SSEDecoder().feed(wire)
    assert actual == expected


@pytest.mark.parametrize(
    "wire",
    [
        b"data: {broken}\n\n",
        b"data: {}\n\n",
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[]}}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{"content":42}}]}\n\n',
        b'data: {"choices":[]}\n\n',
        content_event("alice@exam"),
    ],
)
async def test_invalid_or_truncated_stream_never_flushes_candidate(wire):
    emitted = []
    with pytest.raises(GuardrailError):
        async for chunk in redact_sse(source([wire]), Redactor()):
            emitted.append(chunk)
    assert b"alice" not in b"".join(emitted)


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, wire):
        self.wire, self.closed = wire, False

    async def __aiter__(self):
        for byte in self.wire:
            yield bytes([byte])

    async def aclose(self):
        self.closed = True


async def test_gateway_stream_integration_and_observability(gateway, viewer, caplog):
    upstream = ByteStream(wire_for(["Ready; ", "alice@", "example.com", "; done."]))
    caplog.set_level("INFO", logger="quilr.gateway")

    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream)

    async with gateway(handler) as (client, app):
        response = await client.post("/v1/stream", headers=viewer, json={"prompt": "secret-prompt"})
        assert rendered([response.content]) == "Ready; [REDACTED]; done."
        assert response.headers["x-accel-buffering"] == "no"
        assert app.state.metrics[("pii_redactions", "none")] == 1
        assert upstream.closed
    assert "alice" not in caplog.text and "secret-prompt" not in caplog.text
    assert "stream_first_safe_delta" in caplog.text


async def test_client_disconnect_cancels_and_closes_upstream(gateway, viewer):
    closed = asyncio.Event()

    class EndlessStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield content_event("Hello; ")
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async with gateway(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=EndlessStream()
        )
    ) as (_, app):
        incoming = asyncio.Queue()
        await incoming.put(
            {"type": "http.request", "body": b'{"prompt":"hello"}', "more_body": False}
        )

        async def receive():
            return await incoming.get()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                await incoming.put({"type": "http.disconnect"})

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/stream",
            "raw_path": b"/v1/stream",
            "query_string": b"",
            "root_path": "",
            "server": ("localhost", 80),
            "client": ("localhost", 1),
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", viewer["authorization"].encode()),
            ],
        }
        await asyncio.wait_for(app(scope, receive, send), 2)
        assert closed.is_set()


@pytest.mark.parametrize(
    "wire",
    [
        b'data: {"choices":[{"index":0,"delta":{"content":"\\ud800"}}]}\n\n',
        b"data: " + b"[" * 1100 + b"0" + b"]" * 1100 + b"\n\n",
    ],
)
async def test_hostile_json_cannot_break_stream_error_boundary(wire):
    with pytest.raises(GuardrailError):
        async for _ in redact_sse(source([wire]), Redactor()):
            pass


@pytest.mark.parametrize(
    "status,content_type,wire",
    [
        (500, "text/event-stream", b"private-stack"),
        (200, "text/html", b"private-stack"),
        (200, "text/event-stream", content_event("alice@exam")),
        (200, "text/event-stream", b"data: {broken}\n\n"),
    ],
)
async def test_stream_errors_are_sanitized_events(gateway, viewer, status, content_type, wire):
    upstream = ByteStream(wire)
    async with gateway(
        lambda request: httpx.Response(
            status, headers={"content-type": content_type}, stream=upstream
        )
    ) as (client, _):
        response = await client.post("/v1/stream", headers=viewer, json={"prompt": "test"})
        assert response.status_code == 200
        assert "event: error" in response.text
        assert "private-stack" not in response.text and "alice" not in response.text
        assert "data: [DONE]" not in response.text
        assert upstream.closed
