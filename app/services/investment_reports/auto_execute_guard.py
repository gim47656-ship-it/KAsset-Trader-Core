"""투자 리포트 자동 실행 계정 가드."""

from __future__ import annotations

_LIVE_ACCOUNT_MODES = frozenset({"toss_live", "upbit", "upbit_live"})
_AUTO_EXECUTE_ALLOWED: frozenset[str] = frozenset()


class AutoExecuteLiveBlocked(Exception):
    """실계좌에서 auto_execute_mock을 요청하면 발생한다."""

    def __init__(self, account_mode: str) -> None:
        super().__init__(
            f"auto_execute_mock is permanently blocked for live account "
            f"'{account_mode}'"
        )
        self.account_mode = account_mode


class AutoExecuteUnsupported(Exception):
    """운영 자동 실행을 지원하지 않는 계정에서 발생한다."""

    def __init__(self, account_mode: str) -> None:
        super().__init__(
            f"auto_execute_mock is not supported for account '{account_mode}'"
        )
        self.account_mode = account_mode


def assert_auto_execute_account_allowed(action_mode: str, account_mode: str) -> None:
    if action_mode != "auto_execute_mock":
        return
    if account_mode in _LIVE_ACCOUNT_MODES:
        raise AutoExecuteLiveBlocked(account_mode)
    if account_mode not in _AUTO_EXECUTE_ALLOWED:
        raise AutoExecuteUnsupported(account_mode)
