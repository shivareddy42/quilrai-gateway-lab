from contextlib import asynccontextmanager

import httpx
import pytest

from quilr_gateway.app import create_app
from quilr_gateway.config import DEMO_TOKENS, Settings


@pytest.fixture
def gateway(tmp_path):
    @asynccontextmanager
    async def make(handler, **kwargs):
        settings = Settings(tokens=DEMO_TOKENS, database=str(tmp_path / "quota.sqlite3"), **kwargs)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            app = create_app(settings, upstream)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://gateway"
                ) as client:
                    yield client, app

    return make


@pytest.fixture
def viewer():
    return {"authorization": "Bearer demo-viewer-token"}


@pytest.fixture
def admin():
    return {"authorization": "Bearer demo-admin-token"}
