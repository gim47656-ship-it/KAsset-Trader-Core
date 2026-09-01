"""Tests for sell signal evaluation service."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pytest

from app.services.sell_signal_service import (
    TRIGGER_THRESHOLD,
    _check_bollinger_reentry,
    _check_foreign_selling,
    _check_rsi_momentum,
    _check_stoch_rsi,
    _check_trailing_stop,
    _fetch_current_price,
    _fetch_stock_name,
    evaluate_sell_signal,
)


def _make_ohlcv_df(closes: list[float], n: int | None = None) -> pd.DataFrame:
    if n is None:
        n = len(closes)
    return pd.DataFrame(
        {
            "open": closes[:n],
            "high": [c * 1.01 for c in closes[:n]],
            "low": [c * 0.99 for c in closes[:n]],
            "close": closes[:n],
            "volume": [1000.0] * n,
        }
    )


def _make_large_ohlcv(
    n: int = 200, base: float = 100.0, seed: int = 42
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    changes = rng.normal(0, 1, n)
    closes = [base]
    for c in changes[1:]:
        closes.append(max(closes[-1] + c, 1.0))
    return _make_ohlcv_df(closes, n)


# ---------------------------------------------------------------------------
# _fetch_current_price
# ---------------------------------------------------------------------------


class TestFetchCurrentPrice:
    @pytest.mark.asyncio
    async def test_returns_price_on_success(self):
        toss = AsyncMock()
        toss.prices.return_value = [
            SimpleNamespace(symbol="000660", last_price=1_150_000.0)
        ]
        price, err = await _fetch_current_price(toss, "000660")
        assert price == pytest.approx(1_150_000.0)
        assert err is None

    @pytest.mark.asyncio
    async def test_returns_none_when_symbol_missing(self):
        toss = AsyncMock()
        toss.prices.return_value = []
        price, err = await _fetch_current_price(toss, "000660")
        assert price is None
        assert err is None

    @pytest.mark.asyncio
    async def test_returns_error_on_exception(self):
        toss = AsyncMock()
        toss.prices.side_effect = RuntimeError("API down")
        price, err = await _fetch_current_price(toss, "000660")
        assert price is None
        assert err == "API down"


# ---------------------------------------------------------------------------
# _fetch_stock_name
# ---------------------------------------------------------------------------


class TestFetchStockName:
    @pytest.mark.asyncio
    async def test_returns_name(self):
        toss = AsyncMock()
        toss.stocks.return_value = [SimpleNamespace(symbol="000660", name="SK하이닉스")]
        name = await _fetch_stock_name(toss, "000660")
        assert name == "SK하이닉스"

    @pytest.mark.asyncio
    async def test_falls_back_to_symbol(self):
        toss = AsyncMock()
        toss.stocks.side_effect = RuntimeError("fail")
        name = await _fetch_stock_name(toss, "000660")
        assert name == "000660"


# ---------------------------------------------------------------------------
# _check_trailing_stop
# ---------------------------------------------------------------------------


class TestCheckTrailingStop:
    @staticmethod
    def _client(price: float | None) -> AsyncMock:
        toss = AsyncMock()
        toss.prices.return_value = (
            [SimpleNamespace(symbol="000660", last_price=price)]
            if price is not None
            else []
        )
        return toss

    @pytest.mark.asyncio
    async def test_met_when_price_below_threshold(self):
        cond, price, errors = await _check_trailing_stop(
            self._client(1_100_000.0), "000660", 1_150_000
        )
        assert cond.name == "trailing_stop"
        assert cond.met is True
        assert price == pytest.approx(1_100_000.0)
        assert not errors

    @pytest.mark.asyncio
    async def test_met_when_price_equals_threshold(self):
        cond, _, _ = await _check_trailing_stop(
            self._client(1_150_000.0), "000660", 1_150_000
        )
        assert cond.met is True

    @pytest.mark.asyncio
    async def test_not_met_when_price_above_threshold(self):
        cond, price, _ = await _check_trailing_stop(
            self._client(1_200_000.0), "000660", 1_150_000
        )
        assert cond.met is False
        assert price == pytest.approx(1_200_000.0)

    @pytest.mark.asyncio
    async def test_not_met_when_price_unavailable(self):
        cond, price, _ = await _check_trailing_stop(
            self._client(None), "000660", 1_150_000
        )
        assert cond.met is False
        assert price is None

    @pytest.mark.asyncio
    async def test_error_recorded_on_api_failure(self):
        toss = AsyncMock()
        toss.prices.side_effect = RuntimeError("timeout")
        cond, price, errors = await _check_trailing_stop(toss, "000660", 1_150_000)
        assert cond.met is False
        assert price is None
        assert errors[0]["condition"] == "trailing_stop"


# ---------------------------------------------------------------------------
# _check_stoch_rsi
# ---------------------------------------------------------------------------


class TestCheckStochRsi:
    @pytest.mark.asyncio
    async def test_met_when_k_below_threshold(self):
        df = _make_large_ohlcv(200, base=100)
        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_stoch_rsi",
                return_value={"k": 25.0, "d": 30.0},
            ),
        ):
            cond, errors = await _check_stoch_rsi("000660", 80)
            assert cond.name == "stoch_rsi"
            assert cond.met is True
            assert cond.value == pytest.approx(25.0)
            assert not errors

    @pytest.mark.asyncio
    async def test_not_met_when_k_above_threshold(self):
        df = _make_large_ohlcv(200, base=100)
        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_stoch_rsi",
                return_value={"k": 85.0, "d": 82.0},
            ),
        ):
            cond, errors = await _check_stoch_rsi("000660", 80)
            assert cond.met is False

    @pytest.mark.asyncio
    async def test_insufficient_data(self):
        df = _make_ohlcv_df([100.0] * 10)
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            return_value=df,
        ):
            cond, errors = await _check_stoch_rsi("000660", 80)
            assert cond.met is False
            assert "부족" in cond.detail

    @pytest.mark.asyncio
    async def test_empty_dataframe(self):
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            return_value=pd.DataFrame(),
        ):
            cond, errors = await _check_stoch_rsi("000660", 80)
            assert cond.met is False

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            side_effect=RuntimeError("network"),
        ):
            cond, errors = await _check_stoch_rsi("000660", 80)
            assert cond.met is False
            assert len(errors) == 1
            assert errors[0]["condition"] == "stoch_rsi"


# ---------------------------------------------------------------------------
# _check_foreign_selling
# ---------------------------------------------------------------------------


class TestCheckForeignSelling:
    @pytest.mark.asyncio
    async def test_is_explicitly_provider_unsupported(self):
        cond, errors = await _check_foreign_selling("000660", 2)

        assert cond.name == "foreign_selling"
        assert cond.met is False
        assert cond.value is None
        assert cond.detail == "provider_unsupported: investor flow is unavailable"
        assert errors == [
            {
                "condition": "foreign_selling",
                "error": "provider_unsupported: investor flow is unavailable",
            }
        ]


# ---------------------------------------------------------------------------
# _check_rsi_momentum
# ---------------------------------------------------------------------------


class TestCheckRsiMomentum:
    def _mock_redis(self, stored_state: dict | None = None):
        mock_r = AsyncMock()
        if stored_state:
            mock_r.get.return_value = json.dumps(stored_state)
        else:
            mock_r.get.return_value = None
        mock_r.set.return_value = True
        mock_r.aclose.return_value = None
        return mock_r

    @pytest.mark.asyncio
    async def test_met_when_rsi_drops_below_low_mark_after_high(self):
        df = _make_large_ohlcv(200)
        mock_r = self._mock_redis({"was_above_high": True, "rsi": 72.0})

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": 63.0},
            ),
            patch(
                "app.services.sell_signal_service._get_redis",
                return_value=mock_r,
            ),
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is True
            assert "하락" in cond.detail
            # After trigger, was_above_high resets to False
            set_call = mock_r.set.call_args
            saved = json.loads(set_call[0][1])
            assert saved["was_above_high"] is False

    @pytest.mark.asyncio
    async def test_not_met_when_rsi_above_low_mark(self):
        df = _make_large_ohlcv(200)
        mock_r = self._mock_redis({"was_above_high": True, "rsi": 72.0})

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": 68.0},
            ),
            patch(
                "app.services.sell_signal_service._get_redis",
                return_value=mock_r,
            ),
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is False
            assert "돌파 이력 있음" in cond.detail

    @pytest.mark.asyncio
    async def test_not_met_when_never_reached_high(self):
        df = _make_large_ohlcv(200)
        mock_r = self._mock_redis()

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": 50.0},
            ),
            patch(
                "app.services.sell_signal_service._get_redis",
                return_value=mock_r,
            ),
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is False
            assert "미돌파" in cond.detail

    @pytest.mark.asyncio
    async def test_sets_was_above_high_when_rsi_reaches_high_mark(self):
        df = _make_large_ohlcv(200)
        mock_r = self._mock_redis()

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": 75.0},
            ),
            patch(
                "app.services.sell_signal_service._get_redis",
                return_value=mock_r,
            ),
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is False
            set_call = mock_r.set.call_args
            saved = json.loads(set_call[0][1])
            assert saved["was_above_high"] is True

    @pytest.mark.asyncio
    async def test_insufficient_data(self):
        df = _make_ohlcv_df([100.0] * 10)
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            return_value=df,
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is False
            assert "부족" in cond.detail

    @pytest.mark.asyncio
    async def test_rsi_none_returns_not_met(self):
        df = _make_large_ohlcv(200)
        self._mock_redis()

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": None},
            ),
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is False
            assert "계산 불가" in cond.detail

    @pytest.mark.asyncio
    async def test_redis_state_ttl_is_7_days(self):
        df = _make_large_ohlcv(200)
        mock_r = self._mock_redis()

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": 50.0},
            ),
            patch(
                "app.services.sell_signal_service._get_redis",
                return_value=mock_r,
            ),
        ):
            await _check_rsi_momentum("000660", 70, 65)
            set_call = mock_r.set.call_args
            assert set_call[1]["ex"] == 86400 * 7

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            side_effect=RuntimeError("redis down"),
        ):
            cond, errors = await _check_rsi_momentum("000660", 70, 65)
            assert cond.met is False
            assert len(errors) == 1


# ---------------------------------------------------------------------------
# _check_bollinger_reentry
# ---------------------------------------------------------------------------


class TestCheckBollingerReentry:
    @pytest.mark.asyncio
    async def test_met_on_reentry_failure(self):
        # Build prices: above ref, then drop below ref (re-entry), current below bb_upper
        prices_above = [1_200_000.0] * 5
        prices_below = [1_100_000.0] * 5
        closes = [1_000_000.0] * 190 + prices_above + prices_below
        df = _make_ohlcv_df(closes)

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_bollinger",
                return_value={
                    "upper": 1_150_000.0,
                    "middle": 1_100_000.0,
                    "lower": 1_050_000.0,
                },
            ),
        ):
            cond, errors = await _check_bollinger_reentry(
                "000660", 1_100_000.0, 1_142_000.0
            )
            assert cond.name == "bollinger_reentry"
            assert cond.met is True
            assert "재진입" in cond.detail

    @pytest.mark.asyncio
    async def test_not_met_when_still_above_ref(self):
        closes = [1_200_000.0] * 200
        df = _make_ohlcv_df(closes)

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_bollinger",
                return_value={
                    "upper": 1_150_000.0,
                    "middle": 1_100_000.0,
                    "lower": 1_050_000.0,
                },
            ),
        ):
            cond, errors = await _check_bollinger_reentry(
                "000660", 1_200_000.0, 1_142_000.0
            )
            assert cond.met is False

    @pytest.mark.asyncio
    async def test_not_met_when_never_above_ref(self):
        closes = [1_000_000.0] * 200
        df = _make_ohlcv_df(closes)

        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_bollinger",
                return_value={
                    "upper": 1_150_000.0,
                    "middle": 1_100_000.0,
                    "lower": 1_050_000.0,
                },
            ),
        ):
            cond, errors = await _check_bollinger_reentry(
                "000660", 1_000_000.0, 1_142_000.0
            )
            assert cond.met is False

    @pytest.mark.asyncio
    async def test_not_met_when_current_price_none(self):
        df = _make_large_ohlcv(200)
        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_bollinger",
                return_value={
                    "upper": 1_150_000.0,
                    "middle": 1_100_000.0,
                    "lower": 1_050_000.0,
                },
            ),
        ):
            cond, errors = await _check_bollinger_reentry("000660", None, 1_142_000.0)
            assert cond.met is False
            assert "계산 불가" in cond.detail

    @pytest.mark.asyncio
    async def test_insufficient_data(self):
        df = _make_ohlcv_df([100.0] * 10)
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            return_value=df,
        ):
            cond, errors = await _check_bollinger_reentry("000660", 100.0, 95.0)
            assert cond.met is False
            assert "부족" in cond.detail

    @pytest.mark.asyncio
    async def test_bb_upper_none(self):
        df = _make_large_ohlcv(200)
        with (
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_bollinger",
                return_value={"upper": None, "middle": None, "lower": None},
            ),
        ):
            cond, errors = await _check_bollinger_reentry("000660", 100.0, 95.0)
            assert cond.met is False

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        with patch(
            "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
            side_effect=RuntimeError("fail"),
        ):
            cond, errors = await _check_bollinger_reentry("000660", 100.0, 95.0)
            assert cond.met is False
            assert len(errors) == 1
            assert errors[0]["condition"] == "bollinger_reentry"


# ---------------------------------------------------------------------------
# evaluate_sell_signal — Integration
# ---------------------------------------------------------------------------


class TestEvaluateSellSignal:
    def _patch_all(
        self,
        price: float | None = 1_100_000.0,
        stoch_k: float = 25.0,
        foreign_rows: list | None = None,
        rsi_val: float = 63.0,
        rsi_state: dict | None = None,
        bb_upper: float = 1_150_000.0,
        stock_name: str = "SK하이닉스",
    ):
        _ = foreign_rows
        if rsi_state is None:
            rsi_state = {"was_above_high": True, "rsi": 72.0}

        toss_mock = AsyncMock()
        toss_mock.prices.return_value = (
            [SimpleNamespace(symbol="000660", last_price=price)]
            if price is not None
            else []
        )
        toss_mock.stocks.return_value = [
            SimpleNamespace(symbol="000660", name=stock_name)
        ]
        toss_mock.aclose.return_value = None

        df = _make_large_ohlcv(200)

        mock_r = AsyncMock()
        mock_r.get.return_value = json.dumps(rsi_state)
        mock_r.set.return_value = True
        mock_r.aclose.return_value = None

        return (
            patch(
                "app.services.sell_signal_service._default_toss_client",
                return_value=toss_mock,
            ),
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                return_value=df,
            ),
            patch(
                "app.services.sell_signal_service._calculate_stoch_rsi",
                return_value={"k": stoch_k, "d": 30.0},
            ),
            patch(
                "app.services.sell_signal_service._calculate_rsi",
                return_value={"14": rsi_val},
            ),
            patch(
                "app.services.sell_signal_service._calculate_bollinger",
                return_value={
                    "upper": bb_upper,
                    "middle": 1_100_000.0,
                    "lower": 1_050_000.0,
                },
            ),
            patch("app.services.sell_signal_service._get_redis", return_value=mock_r),
        )

    @pytest.mark.asyncio
    async def test_triggered_when_two_or_more_conditions_met(self):
        # trailing_stop, stoch_rsi, rsi_momentum 조건이 충족된다.
        patches = self._patch_all(price=1_100_000.0, stoch_k=25.0, rsi_val=63.0)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await evaluate_sell_signal("000660")
            assert result["triggered"] is True
            assert result["conditions_met"] >= TRIGGER_THRESHOLD
            assert "매도 검토" in result["message"]
            assert result["symbol"] == "000660"
            assert result["name"] == "SK하이닉스"

    @pytest.mark.asyncio
    async def test_not_triggered_when_one_condition_met(self):
        # trailing_stop만 충족되고 investor flow는 provider_unsupported다.
        patches = self._patch_all(
            price=1_100_000.0,
            stoch_k=85.0,
            foreign_rows=[
                {"frgn_ntby_qty": "5000"},
                {"frgn_ntby_qty": "3000"},
            ],
            rsi_val=50.0,
            rsi_state={},
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await evaluate_sell_signal("000660")
            assert result["triggered"] is False
            assert result["conditions_met"] < TRIGGER_THRESHOLD
            assert "매도 대기" in result["message"]

    @pytest.mark.asyncio
    async def test_zero_conditions_met(self):
        patches = self._patch_all(
            price=1_200_000.0,  # above threshold
            stoch_k=85.0,  # above threshold
            foreign_rows=[
                {"frgn_ntby_qty": "5000"},
                {"frgn_ntby_qty": "3000"},
            ],
            rsi_val=50.0,
            rsi_state={},
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await evaluate_sell_signal("000660")
            assert result["triggered"] is False
            assert result["conditions_met"] == 0

    @pytest.mark.asyncio
    async def test_returns_all_five_conditions(self):
        patches = self._patch_all()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await evaluate_sell_signal("000660")
            assert len(result["conditions"]) == 5
            names = {c.name for c in result["conditions"]}
            assert names == {
                "trailing_stop",
                "stoch_rsi",
                "foreign_selling",
                "rsi_momentum",
                "bollinger_reentry",
            }

    @pytest.mark.asyncio
    async def test_errors_collected_from_evaluators(self):
        toss_mock = AsyncMock()
        toss_mock.prices.side_effect = RuntimeError("price fail")
        toss_mock.stocks.return_value = [
            SimpleNamespace(symbol="000660", name="테스트")
        ]
        toss_mock.aclose.return_value = None

        with (
            patch(
                "app.services.sell_signal_service._default_toss_client",
                return_value=toss_mock,
            ),
            patch(
                "app.services.sell_signal_service._fetch_ohlcv_for_indicators",
                side_effect=RuntimeError("ohlcv fail"),
            ),
        ):
            result = await evaluate_sell_signal("000660")
            assert result["triggered"] is False
            assert len(result["errors"]) > 0
