from app.core.config import Settings, settings


def test_toss_auto_reconcile_flags_default_false():
    assert settings.TOSS_LIVE_AUTO_RECONCILE_ENABLED is False
    assert settings.TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED is False


def test_kis_credentials_are_optional_when_unset(monkeypatch):
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_APP_SECRET", raising=False)

    isolated_settings = Settings(_env_file=None)

    assert isolated_settings.kis_app_key is None
    assert isolated_settings.kis_app_secret is None


def test_removed_kis_activation_fields_are_not_registered():
    removed_fields = {
        "us_quote_kis_primary",
        "invest_quotes_toss_first_kr",
        "invest_quotes_toss_first_us",
        "kis_ws_is_mock",
        "kis_mock_scalping_ws_enabled",
        "kis_mock_scalping_ws_confirm",
        "KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED",
        "KIS_MOCK_RECONCILE_PERIODIC_ENABLED",
        "KIS_LIVE_AUTO_RECONCILE_ENABLED",
        "KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED",
    }

    assert removed_fields.isdisjoint(Settings.model_fields)
