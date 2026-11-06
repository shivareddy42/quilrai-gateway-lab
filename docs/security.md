# Security boundaries and trade-offs

This is an executable assessment with explicit limits. It keeps the decisions small enough to inspect and the failure paths easy to provoke.

## Identity and tool authorization

The bearer token is an opaque lookup key into a server-configured identity map. Roles are never parsed from an unsigned JWT, a token prefix, or a client-supplied role header. Token digests are compared with `hmac.compare_digest`. The small configured key set is scanned without an early return. Key storage and lookup scale are intentionally modest.

Missing, malformed, unknown, and duplicate Authorization headers fail closed. The gateway uses fixed configured upstream URLs and constructs new requests; it forwards neither the caller's authorization header nor arbitrary client headers. Redirects and environment-derived HTTP proxy settings are disabled on its owned HTTP client.

The assessment's authorization rule is a case-sensitive `admin_` prefix. This assumes the downstream uses the same exact names. A real system should maintain an explicit tool policy keyed by trusted server and tool identity. A name prefix is insufficient when aliases or case-insensitive downstream dispatch exist. Tool descriptions are discovery data, not authorization policy.

`tools/list` remains visible to viewers because the task explicitly asks for transparent listing. Visibility does not confer invocation rights. Valid denied notifications receive no JSON-RPC response and still cause no upstream call.

Demo keys are available only with `QUILR_DEMO=1`. They are public fixture strings, never usable production secrets. Non-demo tokens belong in a secret manager or environment configuration, not in the repository. The ledger hashes tenant identifiers and stores neither raw keys nor request text. A tenant hash is pseudonymous, not a guarantee of anonymity for guessable tenant names.

## Streaming policy

| Pattern | Supported policy | Intentional cost |
| :--- | :--- | :--- |
| Email | ASCII email-like atom containing `@` | Masks malformed addresses and adjacent atom punctuation too |
| SSN | Nine numeric digits across spaces/hyphens, or `NNN-NN-NNNN` inside a numeric candidate | Can mask ordinary nine-digit identifiers |
| Card | Numeric candidates with at least 13 digits, with spaces/hyphens allowed | No Luhn requirement; invalid cards and longer numeric identifiers are also masked |
| Overlong candidate | Mask once and discard through its delimiter | Long safe words or identifiers can be lost |

An email-like atom consists of ASCII letters, digits, and `.!#$%&'*+/=?^_` plus the backtick, braces, vertical bar, tilde, `@`, and hyphen. Its cap is 254 characters. Numeric candidates begin with an ASCII digit, allow ASCII spaces and hyphens, and have a 64-character cap. After overflow, drain state keeps memory bounded without releasing a partial prefix.

The redactor is chunk-safe within this documented lexical policy. It is not a universal PII classifier. Internationalized email addresses, Unicode lookalikes/digits, zero-width separators, SSNs written in words, encoded text, and semantic identifiers are outside this policy. The gateway does not normalize text and then pretend that normalization can preserve every security property. A production multilingual policy needs normalization rules and a wider detector, with a regression corpus for false positives and evasion.

There is an unavoidable latency/privacy trade-off: until a delimiter or completion marker arrives, an ordinary word may still become the prefix of an email. This implementation holds that word. It does not claim both zero character holdback and guaranteed future-aware redaction. Safe words flow at delimiters; capped candidates cannot hold the stream forever.

The parser drops provider metadata and emits freshly serialized text events. Unsupported tool-call/refusal payloads are rejected rather than copied through an unguarded field. Malformed UTF-8, unpaired JSON surrogates, deep JSON, oversized events, and incomplete termination cause a sanitized stream error. Previously emitted safe content is not retracted. Incomplete candidates are discarded on failure.

`/v1/stream` is the redacted route. `/v1/completions` is a separate quota/fallback demonstration and does not apply the PII guardrail. The streaming route authenticates callers but does not use the non-streaming token ledger. Combining quota reconciliation, streaming usage, and mid-stream fallback is a separate production design; this submission does not silently claim it.

## Quota integrity and resilience

Atomic admission prevents concurrent overspend on the same SQLite file. A timeout remains charged because the remote provider may have consumed the request. Backup attempts must also fit quota. Provider usage is schema-checked and bound-checked before any credit is given. Unknown usage, invalid usage, cancellation, and internal failures retain the full reservation.

SQLite blocking work runs in worker threads. Cancellation of a waiting coroutine cannot undo a transaction already running in a worker; a resulting orphan reservation is conservative and expires with its lease. A short busy timeout turns lock contention into a sanitized 503. There is no fallback to an in-memory counter that loses cross-process enforcement.

SQLite supplies process concurrency and local durability, not multi-host coordination. Use one local persistent volume and an appropriate OS file permission policy. Backup and restore must account for WAL files. Do not copy only the main database file while writes are active. A normal restart preserves quota; deleting or restoring old state changes the enforcement history.

The prompt-byte bound assumes a plain-text, byte-tokenized provider with no hidden additions. Real adapters must account for their exact tokenizer, templates, special tokens, and all provider attempts. Provider-side output limits must actually be enforced. Remote cancellation and remote usage remain provider contracts.

## Observability without payload logging

Application events use a fixed vocabulary and fixed-cardinality metric labels. There are no tenant labels, API-key labels, prompt logs, or raw exception messages. The gateway generates correlation IDs instead of trusting an arbitrary incoming ID. Stdio logs name only the allowlisted tool, never its arguments.

Metrics require an administrator token, while `/healthz` reports only liveness. Metrics are per process. Aggregate them externally for multiple workers. The liveness endpoint does not prove provider reachability or database health; quota admission itself fails closed if storage fails.

The demo uses loopback HTTP. Before exposing the service beyond the host, provide TLS and ingress authentication policy, connection/concurrency limits, trusted host/origin enforcement, and network egress restrictions. Keep the mock service private. For a zero-trust deployment, check DNS, certificate trust/SNI, gateway-to-provider identity, outbound allowlists, proxy behavior, and reverse-proxy SSE buffering separately. Start with `/healthz`, then an authenticated JSON request, then `curl -N` to isolate transport and buffering failures.

## Scope left for a real deployment

- Verified JWT/OAuth identities with issuer, audience, expiry, algorithm policy, key rotation, and revocation.
- Explicit server/tool authorization policy rather than a naming convention.
- Provider-specific adapters, secret injection, tokenizer accounting, circuit breaking, and idempotency where supported.
- Durable business idempotency, approval workflow, and money-safe decimal arithmetic for actual refunds. The assessment's positive float input is retained here; no money is moved.
- A distributed atomic quota store when routing across hosts, plus deliberate clock and retention policy.
- Load testing, SLOs, deployment-specific resource caps, and multilingual PII evaluation before production claims.

These are known boundaries, not hidden TODOs in the four assessment paths.
