"""ROB-307 PR1 — deterministic trend micro-breakout scalping signal (pure).

Binance demo 실행 어댑터가 제거되면서 원래 위치
(``app.services.brokers.binance.demo_scalping.signal``)에 있던 **순수 계산**만
이 연구 모듈로 옮겼다. 브로커·주문·DB·네트워크 의존은 원래도 없었고, 옮긴 뒤에도
없다. 시그널 값과 reason code 문자열은 기존 구현과 동일하게 유지한다(연구 산출물의
parity 테스트가 이 값을 고정한다).

Operator-selected strategy:

* **Long** when ``sma_fast > sma_slow`` (uptrend) AND the latest close
  breaks above the prior ``breakout_lookback``-bar high.
* **Short** (futures only; ``allow_short=True``) on the mirror condition:
  ``sma_fast < sma_slow`` AND close breaks below the prior-bar low. Spot
  is long-only, so shorts are suppressed.
* Exits are fixed-bps TP/SL relative to the entry candidate.

``evaluate_signal`` is pure over a candle sequence — deterministic, no
network, no broker, no LLM. Thresholds live in ``SignalConfig`` so they
are trivially tunable and test-pinned.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

Side = Literal["BUY", "SELL"]

_BPS = Decimal("10000")
# Reference separation/margin (in bps) that maps to full confidence.
_CONFIDENCE_REFERENCE_BPS = Decimal("50")


class ReasonCode:
    """Signal-outcome reason codes (append-only string constants).

    옮겨 오면서 시그널 판정에 쓰이는 코드만 남겼다. 리스크/원장 게이트 코드는
    제거된 demo 실행 경로 전용이었으므로 함께 사라졌다.
    """

    ENTER_LONG_BREAKOUT = "enter_long_breakout"
    ENTER_SHORT_BREAKDOWN = "enter_short_breakdown"
    NO_SIGNAL = "no_signal"
    INSUFFICIENT_HISTORY = "insufficient_history"


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    close_time_ms: int


@dataclass(frozen=True)
class SignalConfig:
    sma_fast: int = 7
    sma_slow: int = 25
    breakout_lookback: int = 20
    tp_bps: Decimal = Decimal("30")
    sl_bps: Decimal = Decimal("20")
    allow_short: bool = False  # spot: False; futures: True


@dataclass(frozen=True)
class SignalDecision:
    has_entry: bool
    side: Side | None
    entry_price: Decimal | None
    tp_price: Decimal | None
    sl_price: Decimal | None
    confidence: Decimal
    reason_codes: tuple[str, ...] = field(default_factory=tuple)


def _sma(closes: Sequence[Decimal], period: int) -> Decimal:
    window = closes[-period:]
    return sum(window, Decimal("0")) / Decimal(len(window))


def _clamp01(value: Decimal) -> Decimal:
    if value < 0:
        return Decimal("0")
    if value > 1:
        return Decimal("1")
    return value


def _no_entry(reason: str) -> SignalDecision:
    return SignalDecision(
        has_entry=False,
        side=None,
        entry_price=None,
        tp_price=None,
        sl_price=None,
        confidence=Decimal("0"),
        reason_codes=(reason,),
    )


def _confidence(*, separation_bps: Decimal, margin_bps: Decimal) -> Decimal:
    sep_norm = _clamp01(abs(separation_bps) / _CONFIDENCE_REFERENCE_BPS)
    margin_norm = _clamp01(abs(margin_bps) / _CONFIDENCE_REFERENCE_BPS)
    return _clamp01((sep_norm + margin_norm) / Decimal("2"))


def evaluate_signal(candles: Sequence[Candle], config: SignalConfig) -> SignalDecision:
    """Evaluate the latest candle against the trend micro-breakout rule."""

    needed = max(config.sma_slow, config.breakout_lookback + 1)
    if len(candles) < needed:
        return _no_entry(ReasonCode.INSUFFICIENT_HISTORY)

    closes = [c.close for c in candles]
    current = candles[-1]
    prior = candles[:-1]

    sma_fast = _sma(closes, config.sma_fast)
    sma_slow = _sma(closes, config.sma_slow)
    separation_bps = (sma_fast - sma_slow) / sma_slow * _BPS

    lookback = prior[-config.breakout_lookback :]
    prior_high = max(c.high for c in lookback)
    prior_low = min(c.low for c in lookback)

    long_ok = sma_fast > sma_slow and current.close > prior_high
    short_ok = config.allow_short and sma_fast < sma_slow and current.close < prior_low

    if long_ok:
        entry = current.close
        margin_bps = (entry - prior_high) / prior_high * _BPS
        return SignalDecision(
            has_entry=True,
            side="BUY",
            entry_price=entry,
            tp_price=entry * (Decimal("1") + config.tp_bps / _BPS),
            sl_price=entry * (Decimal("1") - config.sl_bps / _BPS),
            confidence=_confidence(
                separation_bps=separation_bps, margin_bps=margin_bps
            ),
            reason_codes=(ReasonCode.ENTER_LONG_BREAKOUT,),
        )

    if short_ok:
        entry = current.close
        margin_bps = (prior_low - entry) / prior_low * _BPS
        return SignalDecision(
            has_entry=True,
            side="SELL",
            entry_price=entry,
            tp_price=entry * (Decimal("1") - config.tp_bps / _BPS),
            sl_price=entry * (Decimal("1") + config.sl_bps / _BPS),
            confidence=_confidence(
                separation_bps=separation_bps, margin_bps=margin_bps
            ),
            reason_codes=(ReasonCode.ENTER_SHORT_BREAKDOWN,),
        )

    return _no_entry(ReasonCode.NO_SIGNAL)
