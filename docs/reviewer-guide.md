# A five-minute review

## Run once

```bash
uv sync --locked
uv run --locked quilr-demo
```

This uses real child processes and real loopback HTTP, including the three-second timeout path. Its timing comes from that execution. The test suite also checks behavior with deterministic transports so failure cases do not depend on unreliable external services.

## Follow the trust decisions

1. Open `mcp_server.py`. The models are the published tool schemas. Validation errors cross the SDK session boundary as `-32602`, with no invalid values echoed back.
2. Open `app.py`, then `config.py`. Identity comes from a configured bearer key. The viewer's `admin_` call returns before the HTTP client is invoked. `tools/list` preserves the upstream result.
3. Open `guardrail.py`. The stream is framed before JSON is parsed, then validated text passes through two bounded state machines. Nothing copies raw provider bytes to the client.
4. Open `limiter.py`. The read/check/insert operation is one `BEGIN IMMEDIATE` transaction. Open `router.py` to see each attempt reserve, run under a whole-attempt deadline, and reconcile in `finally`.

## Tests that do more than say hello

| Test family | Failure it would catch |
| :--- | :--- |
| Raw stdout subprocess | A stray print corrupting MCP framing |
| Official MCP client | A server that speaks JSON but fails initialization or schema discovery |
| Standard error preflight | SDK upgrades changing malformed-input semantics |
| Denied call with a failing upstream spy | A proxy that checks authorization after forwarding |
| Every PII split and generated partitions | Redaction that works only within one provider delta |
| One-byte SSE, CRLF, UTF-8, comments | A parser that mistakes transport chunks for events |
| Truncated candidate and malformed Unicode | A flush/error path leaking the pending PII prefix |
| ASGI client disconnect | A leaked upstream stream after the caller leaves |
| Multiple SQLite connections and processes | A check-then-write quota race hidden by one worker |
| Exact cutoff and reopened database | Off-by-one expiry and accidental in-memory enforcement |
| Slow trickling response body | A read timeout mistaken for a whole-attempt deadline |
| Cancelled primary before backup | A timeout race that leaves local generation tasks running |
| Quota before secondary | Fallback bypassing the tenant's remaining allowance |
| Invalid usage, lock contention, provider errors | Free retries or internal-detail leakage in failure paths |

The atomic-quota contention tests retry only SQLite `BUSY`, with a five-second retry budget. Shared CI runners may legitimately exceed the production 250ms lock-wait limit. These tests still require the exact number of accepted reservations and exact ledger row/token totals; I/O errors and corruption fail immediately. Separate tests hold a write lock to verify production fails closed without charging quota. CI repeats the contention checks five times per Python version.

## Operating locally

All supported environment settings are shown in `.env.example`. The app does not load that file implicitly. Use shell exports or `uvicorn --env-file .env` after copying and editing it. The default URLs target the local mock server on port 9001. `/v1/completions` is a small plain-text adapter with required `prompt` and optional `max_tokens`, not a drop-in implementation of a vendor's full chat API.

For a clean quota slate, stop the gateway, then move its `var/` directory aside and restart. This changes persisted accounting, so it is a local-demo reset, not an operating procedure for a shared service. The automated demo already uses a fresh temporary database and removes it on exit.

| Symptom | First check |
| :--- | :--- |
| Startup requests credentials | Set a real `QUILR_TOKENS_JSON` map or explicitly use `QUILR_DEMO=1` |
| 401 | Send exactly one Authorization header with a configured bearer token |
| MCP `-32001` | The chosen tool begins with `admin_` and the identity is a viewer |
| 429 with Retry-After | The tenant has live settled charges or pending reservations |
| 503 after a timeout | Check whether the backup failed, storage is locked, or quota prevented a second attempt |
| Stream appears buffered | Use `curl -N`; disable ingress buffering; inspect `X-Accel-Buffering: no` |
| Stream has `event: error` | Check provider SSE framing and terminal marker; client content remains sanitized |
| Stdio client fails parsing | Inspect stdout separately from stderr; never merge them with `2>&1` |

The test suite owns every subprocess it terminates and never kills a process by port number. Ephemeral-port selection has a small bind race; readiness checks fail explicitly if another process wins. Persistent services are started manually on fixed loopback ports.
