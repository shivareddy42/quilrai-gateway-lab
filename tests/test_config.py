import pytest

from quilr_gateway.config import DEMO_TOKENS, Settings


def test_no_implicit_demo_credentials(monkeypatch):
    monkeypatch.delenv("QUILR_TOKENS_JSON", raising=False)
    monkeypatch.delenv("QUILR_DEMO", raising=False)
    with pytest.raises(RuntimeError):
        Settings.from_env()
    monkeypatch.setenv("QUILR_DEMO", "1")
    assert Settings.from_env().tokens == DEMO_TOKENS


def test_configured_roles_not_client_claims(monkeypatch):
    monkeypatch.setenv("QUILR_TOKENS_JSON", '{"opaque-secret":{"role":"viewer","tenant":"a"}}')
    settings = Settings.from_env()
    assert settings.authenticate("bEaReR opaque-secret").role == "viewer"
    assert settings.authenticate("Bearer admin") is None
    assert "opaque-secret" not in repr(settings)
    monkeypatch.setenv("QUILR_TOKENS_JSON", '{"forged":{"role":"owner","tenant":"a"}}')
    with pytest.raises(RuntimeError):
        Settings.from_env()
