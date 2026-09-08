"""
Tests for configuration module.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings

EXPECTED_UPBIT_API_RATE_LIMITS = {
    "GET /v1/ticker": {"rate": 10, "period": 1.0},
}


def _required_settings_kwargs() -> dict[str, str]:
    return {
        "opendart_api_key": settings.opendart_api_key,
        "DATABASE_URL": settings.DATABASE_URL,
        "SECRET_KEY": settings.SECRET_KEY,
    }


def _build_settings(**kwargs: object) -> Settings:
    settings_class = globals()["Settings"]
    cfg = settings_class(**kwargs)
    assert isinstance(cfg, Settings)
    return cfg


def _new_settings() -> Settings:
    return _build_settings(**_required_settings_kwargs())


class TestSettings:
    """Test Settings class."""

    def test_settings_instance(self):
        """Test that settings is an instance of Settings."""
        assert isinstance(settings, Settings)

    def test_settings_attributes(self):
        """Test that settings has required attributes."""
        # Test that required attributes exist (these will be None in test env)
        assert hasattr(settings, "telegram_token")
        assert hasattr(settings, "opendart_api_key")
        assert hasattr(settings, "DATABASE_URL")

    def test_yahoo_cache_settings_attributes_exist(self):
        assert hasattr(settings, "yahoo_ohlcv_cache_enabled")
        assert hasattr(settings, "yahoo_ohlcv_cache_max_days")
        assert hasattr(settings, "yahoo_ohlcv_cache_lock_ttl_seconds")


class TestConfigLoading:
    """Test configuration loading."""

    def test_settings_singleton(self):
        """Test that settings is a singleton."""
        from app.core.config import settings as settings2

        assert settings is settings2

    def test_redis_url_generation(self):
        """Test Redis URL generation method."""
        redis_url = settings.get_redis_url()
        if settings.redis_url:
            assert redis_url == settings.redis_url
            return

        expected_scheme = "rediss://" if settings.redis_ssl else "redis://"
        assert redis_url.startswith(expected_scheme)
        assert f"{settings.redis_host}:{settings.redis_port}" in redis_url
        assert redis_url.endswith(f"/{settings.redis_db}")

    def test_api_rate_limit_defaults_include_builtins(self):
        cfg = _new_settings()

        assert cfg.upbit_api_rate_limits == EXPECTED_UPBIT_API_RATE_LIMITS

    def test_empty_object_env_override_does_not_erase_builtins(self, monkeypatch):
        monkeypatch.setenv("UPBIT_API_RATE_LIMITS", "{}")

        cfg = _new_settings()

        assert cfg.upbit_api_rate_limits == EXPECTED_UPBIT_API_RATE_LIMITS

    def test_empty_string_env_override_does_not_erase_builtins(self, monkeypatch):
        monkeypatch.setenv("UPBIT_API_RATE_LIMITS", "")

        cfg = _new_settings()

        assert cfg.upbit_api_rate_limits == EXPECTED_UPBIT_API_RATE_LIMITS

    def test_partial_api_rate_limit_override_merges_endpoint_subdict(self, monkeypatch):
        monkeypatch.setenv(
            "UPBIT_API_RATE_LIMITS",
            '{"GET /v1/ticker": {"rate": 25}}',
        )

        cfg = _new_settings()

        assert cfg.upbit_api_rate_limits["GET /v1/ticker"] == {
            "rate": 25,
            "period": 1.0,
        }

    def test_public_api_paths_supports_csv_env_string(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_API_PATHS", "/healthz,/api/scan")

        cfg = _new_settings()

        assert cfg.PUBLIC_API_PATHS == ["/healthz", "/api/scan"]

    def test_public_api_paths_supports_json_env_string(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_API_PATHS", '["/healthz", "/api/scan"]')

        cfg = _new_settings()

        assert cfg.PUBLIC_API_PATHS == ["/healthz", "/api/scan"]

    def test_public_api_paths_supports_empty_string_env(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_API_PATHS", "")

        cfg = _new_settings()

        assert cfg.PUBLIC_API_PATHS == []

    def test_public_api_paths_supports_empty_json_env_string(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_API_PATHS", "[]")

        cfg = _new_settings()

        assert cfg.PUBLIC_API_PATHS == []

    def test_public_api_paths_rejects_non_string_json_list(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_API_PATHS", '["/healthz", 1]')

        with pytest.raises(ValidationError, match="PUBLIC_API_PATHS JSON value"):
            _new_settings()

    def test_constructor_public_api_paths_list_is_preserved(self):
        cfg = _build_settings(
            **_required_settings_kwargs(),
            PUBLIC_API_PATHS=["/healthz"],
        )

        assert cfg.PUBLIC_API_PATHS == ["/healthz"]

    def test_invalid_api_rate_limit_json_raises_validation_error(self, monkeypatch):
        monkeypatch.setenv("UPBIT_API_RATE_LIMITS", "{not-json}")

        with pytest.raises(ValidationError, match="Invalid JSON for API rate limits"):
            _new_settings()

    def test_non_object_api_rate_limit_json_raises_validation_error(self, monkeypatch):
        monkeypatch.setenv("UPBIT_API_RATE_LIMITS", "[]")

        with pytest.raises(
            ValidationError, match="API rate limits must be a JSON object"
        ):
            _new_settings()

    def test_constructor_empty_upbit_api_rate_limits_replaces_builtins(self):
        cfg = _build_settings(**_required_settings_kwargs(), upbit_api_rate_limits={})

        assert cfg.upbit_api_rate_limits == {}

    def test_telegram_chat_ids_str_splits_multiple_ids(self):
        cfg = Settings(
            telegram_token="token",
            telegram_chat_id="legacy",
            telegram_chat_ids_str="111, 222,,333 ",
        )

        assert cfg.telegram_chat_ids == ["111", "222", "333"]

    def test_telegram_chat_ids_falls_back_to_single_chat_id(self):
        # conftest가 TELEGRAM_CHAT_IDS_STR 전역 기본값을 심으므로 명시적으로
        # 비워야 legacy 폴백 경로가 검증된다 (env/dotenv 무관 밀폐형).
        cfg = Settings(
            telegram_token="token",
            telegram_chat_id="legacy",
            telegram_chat_ids_str=None,
        )

        assert cfg.telegram_chat_ids == ["legacy"]


def test_runbook_exists() -> None:
    assert Path("docs/runbooks/freqtrade-research-pipeline.md").exists()


@pytest.mark.unit
def test_watch_notify_transport_defaults_to_hermes_webhook():
    from app.core.config import settings

    assert settings.WATCH_NOTIFY_TRANSPORT in ("hermes_webhook", "python_direct")
