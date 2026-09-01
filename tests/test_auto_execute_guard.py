"""투자 리포트 자동 실행 계정 가드 테스트."""

import pytest

from app.services.investment_reports.auto_execute_guard import (
    AutoExecuteLiveBlocked,
    AutoExecuteUnsupported,
    assert_auto_execute_account_allowed,
)


@pytest.mark.parametrize("account_mode", ["toss_live", "upbit", "upbit_live"])
def test_active_live_accounts_are_permanently_blocked(account_mode: str):
    with pytest.raises(AutoExecuteLiveBlocked):
        assert_auto_execute_account_allowed("auto_execute_mock", account_mode)


@pytest.mark.parametrize("account_mode", ["kis_live", "kis_mock", "kiwoom_mock"])
def test_kis_and_unwired_mock_accounts_are_unsupported(account_mode: str):
    with pytest.raises(AutoExecuteUnsupported):
        assert_auto_execute_account_allowed("auto_execute_mock", account_mode)


def test_non_auto_mode_keeps_historical_kis_read_flow():
    assert_auto_execute_account_allowed("notify_only", "kis_live")
