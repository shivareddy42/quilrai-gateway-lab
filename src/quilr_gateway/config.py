import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Identity:
    role: str
    tenant: str


DEMO_TOKENS = {
    "demo-viewer-token": Identity("viewer", "tenant-a"),
    "demo-admin-token": Identity("admin", "tenant-a"),
    "demo-other-tenant-token": Identity("viewer", "tenant-b"),
}


@dataclass
class Settings:
    tokens: dict[str, Identity] = field(repr=False)
    database: str = "var/quota.sqlite3"
    mcp_url: str = "http://127.0.0.1:9001/mcp"
    stream_url: str = "http://127.0.0.1:9001/llm/stream"
    primary_url: str = "http://127.0.0.1:9001/llm/primary"
    secondary_url: str = "http://127.0.0.1:9001/llm/secondary"
    timeout: float = 3.0

    def authenticate(self, header: str | None) -> Identity | None:
        parts = (header or "").split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        digest = hashlib.sha256(parts[1].encode()).digest()
        identity = None
        for token, candidate in self.tokens.items():
            if hmac.compare_digest(digest, hashlib.sha256(token.encode()).digest()):
                identity = candidate
        return identity

    @classmethod
    def from_env(cls):
        raw = os.environ.get("QUILR_TOKENS_JSON")
        if raw:
            tokens = {token: Identity(**value) for token, value in json.loads(raw).items()}
        elif os.environ.get("QUILR_DEMO") == "1":
            tokens = DEMO_TOKENS.copy()
        else:
            raise RuntimeError("Set QUILR_TOKENS_JSON or explicitly enable QUILR_DEMO=1")
        if not tokens or any(
            not key or value.role not in {"admin", "viewer"} or not value.tenant
            for key, value in tokens.items()
        ):
            raise RuntimeError("Invalid token configuration")
        return cls(
            tokens=tokens,
            database=os.environ.get("QUILR_DATABASE", "var/quota.sqlite3"),
            mcp_url=os.environ.get("QUILR_MCP_URL", "http://127.0.0.1:9001/mcp"),
            stream_url=os.environ.get("QUILR_STREAM_URL", "http://127.0.0.1:9001/llm/stream"),
            primary_url=os.environ.get("QUILR_PRIMARY_URL", "http://127.0.0.1:9001/llm/primary"),
            secondary_url=os.environ.get(
                "QUILR_SECONDARY_URL", "http://127.0.0.1:9001/llm/secondary"
            ),
        )
