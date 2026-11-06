"""Bounded, chunk-independent text redaction and incremental SSE framing."""

import codecs
import json
import re
from collections.abc import AsyncIterator

MASK = "[REDACTED]"
# ASCII email local/domain characters. Conservative: any such atom containing @
# is sensitive, including malformed addresses. Unicode EAI is outside this policy.
EMAIL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.!#$%&'*+/=?^_`{|}~@-"
)


class GuardrailError(Exception):
    """An upstream stream cannot be safely interpreted."""


class AtomRedactor:
    def __init__(self, kind: str, cap: int) -> None:
        self.kind, self.cap = kind, cap
        self.pending = ""
        self.draining = False
        self.redactions = 0
        self.peak = 0

    def _mask(self) -> str:
        self.redactions += 1
        return MASK

    def _flush(self) -> str:
        value, self.pending = self.pending, ""
        if self.kind == "email":
            return self._mask() if "@" in value else value
        digits = sum("0" <= char <= "9" for char in value)
        sensitive = (
            digits == 9 or digits >= 13 or bool(re.search(r"[0-9]{3}-[0-9]{2}-[0-9]{4}", value))
        )
        # Preserve trailing spaces outside the sensitive value.
        return self._mask() + value[len(value.rstrip(" -")) :] if sensitive else value

    def feed(self, text: str) -> str:
        output = []
        for char in text:
            eligible = (
                char in EMAIL_CHARS
                if self.kind == "email"
                else "0" <= char <= "9" or (char in " -" and (self.pending or self.draining))
            )
            if eligible:
                if self.draining:
                    continue
                if len(self.pending) == self.cap:
                    output.append(self._mask())
                    self.pending = ""
                    self.draining = True
                else:
                    self.pending += char
                    self.peak = max(self.peak, len(self.pending))
            else:
                output.append(self._flush())
                self.draining = False
                output.append(char)
        return "".join(output)

    def finish(self) -> str:
        self.draining = False
        return self._flush()


class Redactor:
    """Email stage first, numeric stage second; never emit an unresolved candidate.

    Memory: at most 254 + 64 pending characters. Work: O(n), including long
    delimiter-free input. Overlong atoms are masked once, then drained.
    """

    def __init__(self) -> None:
        self.email = AtomRedactor("email", 254)
        self.number = AtomRedactor("number", 64)

    def feed(self, text: str) -> str:
        return self.number.feed(self.email.feed(text))

    def finish(self) -> str:
        return self.number.feed(self.email.finish()) + self.number.finish()

    @property
    def redactions(self) -> int:
        return self.email.redactions + self.number.redactions


class SSEDecoder:
    """UTF-8, LF/CRLF/CR, comments, multiline data; capped per event, not stream."""

    def __init__(self, limit: int = 16_384) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.limit = limit
        self.line = ""
        self.data: list[str] = []
        self.size = 0
        self.after_cr = False

    def _line(self) -> str | None:
        line, self.line = self.line, ""
        if not line:
            event = "\n".join(self.data) if self.data else None
            self.data.clear()
            self.size = 0
            return event
        if line.startswith("data:"):
            value = line[5:]
            self.data.append(value[1:] if value.startswith(" ") else value)
        return None

    def feed(self, chunk: bytes, *, final: bool = False) -> list[str]:
        try:
            decoded = self.decoder.decode(chunk, final=final)
        except UnicodeDecodeError as exc:
            raise GuardrailError("Invalid UTF-8") from exc
        events = []
        for char in decoded:
            if self.after_cr and char == "\n":
                self.after_cr = False
                continue
            self.after_cr = char == "\r"
            self.size += 1
            if self.size > self.limit:
                raise GuardrailError("SSE event too large")
            if char in "\r\n":
                value = self._line()
                if value is not None:
                    events.append(value)
            else:
                self.line += char
        if final and (self.line or self.data):
            raise GuardrailError("Truncated SSE event")
        return events


def content_event(text: str) -> bytes:
    payload = {"choices": [{"index": 0, "delta": {"content": text}}]}
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def parse_delta(event: str) -> str:
    try:
        value = json.loads(event)
        choices = value["choices"]
        if len(choices) != 1 or choices[0]["index"] != 0:
            raise ValueError("Only one choice supported")
        delta = choices[0]["delta"]
        if not isinstance(delta, dict) or set(delta) - {"content", "role"}:
            raise ValueError("Unsupported delta")
        content = delta.get("content")
        if content is None:
            return ""
        if not isinstance(content, str):
            raise ValueError("Non-text content")
        content.encode("utf-8")  # Reject unpaired JSON surrogate escapes before emitting bytes.
        return content
    except (ValueError, KeyError, TypeError, IndexError, RecursionError, UnicodeError) as exc:
        raise GuardrailError("Invalid provider event") from exc


async def redact_sse(chunks: AsyncIterator[bytes], redactor: Redactor) -> AsyncIterator[bytes]:
    decoder = SSEDecoder()
    async for chunk in chunks:
        # Bound parser work units even if a provider sends one huge transport chunk.
        for offset in range(0, len(chunk), 1024):
            for event in decoder.feed(chunk[offset : offset + 1024]):
                if event == "[DONE]":
                    tail = redactor.finish()
                    if tail:
                        yield content_event(tail)
                    yield b"data: [DONE]\n\n"
                    return
                safe = redactor.feed(parse_delta(event))
                if safe:
                    yield content_event(safe)
    decoder.feed(b"", final=True)
    # EOF alone is not proof that a candidate is complete.
    raise GuardrailError("Missing terminal marker")
