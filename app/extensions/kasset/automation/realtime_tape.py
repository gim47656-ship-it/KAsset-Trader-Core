"""국내 실시간 체결·호가 관찰창과 단타 진입·흐름 악화 청산 판정.

이 모듈은 순수 계산만 한다. 네트워크·DB·Redis를 모르고, NH 원문 파싱은
``app.extensions.kasset.nhplug.protocol``이 이 모듈의 틱 타입으로 바꿔 넘긴다.
실행 프로세스(``nh-stream``)와 판정 소비자(선택 단계·제출 직전 재확인·1분
청산 task)가 같은 코드를 쓰므로 replay 테스트가 운영 경로를 그대로 재현한다.

필드 의미의 근거 (NH ``krstock/openapi.json``):

- ``oc.bidvolall``/``oc.offvolall``은 누적 매수/매도 체결량으로 읽는다. REST
  ``/krstock/quote/v1/currentExecution`` 응답이 같은 개념을
  ``shnu_cntg_smtn``(누적매수체결량)·``seln_cntg_smtn``(누적매도체결량)·
  ``stnr_cntg_smtn``(누적보합체결량)·``cttr``(체결강도)로 정의한다. 실시간 채널
  예시 payload에서 ``volpower = bidvolall / offvolall × 100``(109.75),
  ``bidrate = bidvolall / volume × 100``(52.09, 당일매수비중)이 정확히 맞는다.
  실시간 필드 자체의 한글 정의는 명세에 없으므로 장중 실수신 대조는 별도
  확인 대상이다.
- ``oc.avgprice``는 당일 누적 VWAP으로 읽는다. 예시에서
  ``value_won / volume``(20867545950 / 660224 ≈ 31607.2)과 일치한다.
- ``volrate``·``janggubun``·``marketgb``·``market_*``는 의미를 확인하지 못해
  판단에 쓰지 않는다.

이 판정은 기존 손절선을 실시간으로 움직이지 않는다. 진입은 기존 일봉 setup과
장중 trigger를 통과한 BUY에만 추가 관문으로 붙고, 청산은 평가익 구간에서
체결 흐름이 나빠질 때 보유분을 먼저 정리하는 별도 신호다.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from statistics import median
from typing import Final
from zoneinfo import ZoneInfo

KST: Final = ZoneInfo("Asia/Seoul")

REALTIME_TAPE_SCHEMA_VERSION: Final = "kasset.realtime-tape.v1"
#: producer가 KRX BUY에 남기는 설명 evidence. 실제 관문 대상은
#: source/market/action으로 판별하므로 배포 전 marker 없는 추천도 포함한다.
REALTIME_ENTRY_EVIDENCE_KIND: Final = "realtime_entry"
REALTIME_ENTRY_PROVIDER: Final = "nhplug"
REALTIME_ENTRY_VENUE: Final = "KRX"
#: Position Manager 결정론 청산 evidence의 ``exitKind``. ``ExitKind`` enum에
#: 넣지 않는다 — 기존 사다리의 단계가 아니라 별도 조기 청산 신호다.
REALTIME_TREND_EXIT_KIND: Final = "REALTIME_TREND_EXIT"

_HUNDRED: Final = Decimal("100")
_ZERO: Final = Decimal("0")
_BPS: Final = Decimal("10000")


@dataclass(frozen=True, slots=True)
class RealtimeTapeConfig:
    """PAPER 초기 실험값(2026-09-29 승인). 수익성 검증을 거친 값이 아니다."""

    observation_seconds: int = 60
    max_book_gap_seconds: int = 5
    max_trade_gap_seconds: int = 15
    minimum_trades: int = 10
    max_book_age_seconds: int = 3
    max_trade_age_seconds: int = 15
    #: 발행 프로세스가 살아 있는지 보는 snapshot 자체의 최대 나이.
    max_snapshot_age_seconds: int = 3
    #: 거래소 시각(HH:MM:SS)과 수신 시각이 이보다 벌어진 프레임은 버린다.
    max_exchange_clock_skew_seconds: int = 60
    entry_min_volume_power: Decimal = Decimal("100")
    max_median_spread_bps: Decimal = Decimal("30")
    exit_max_volume_power: Decimal = Decimal("100")
    ma_bars: int = 5


DEFAULT_REALTIME_TAPE_CONFIG: Final = RealtimeTapeConfig()


@dataclass(frozen=True, slots=True)
class TradeTick:
    """정규화된 KRX 체결 1건. 누적값은 당일 세션 누계다."""

    symbol: str
    received_at: datetime
    exchange_time: time
    price: Decimal
    cumulative_volume: int
    cumulative_buy_volume: int | None
    cumulative_sell_volume: int | None
    session_vwap: Decimal | None


@dataclass(frozen=True, slots=True)
class BookLevel:
    """같은 호가 단계의 가격·잔량. 없는 필드는 추정하지 않는다."""

    bid: Decimal | None
    ask: Decimal | None
    bid_size: int | None
    ask_size: int | None

    def as_json(self) -> dict[str, object]:
        return {
            "bid": str(self.bid) if self.bid is not None else None,
            "ask": str(self.ask) if self.ask is not None else None,
            "bidSize": self.bid_size,
            "askSize": self.ask_size,
        }

    @classmethod
    def from_json(cls, item: Mapping[str, object]) -> BookLevel:
        return cls(
            bid=Decimal(str(item["bid"])) if item["bid"] is not None else None,
            ask=Decimal(str(item["ask"])) if item["ask"] is not None else None,
            bid_size=int(str(item["bidSize"])) if item["bidSize"] is not None else None,
            ask_size=int(str(item["askSize"])) if item["askSize"] is not None else None,
        )


@dataclass(frozen=True, slots=True)
class BookTick:
    """정규화된 KRX 10호가. 잔량 불균형은 관측 evidence이며 진입 gate가 아니다."""

    symbol: str
    received_at: datetime
    exchange_time: time
    best_bid: Decimal | None
    best_ask: Decimal | None
    best_bid_size: int | None
    best_ask_size: int | None
    total_bid_size: int | None
    total_ask_size: int | None
    depth: tuple[BookLevel, ...] = ()


class VolumePowerState(StrEnum):
    RATIO = "RATIO"
    #: 창 안에 매도 체결이 없고 매수 체결만 있다. 비율은 정의되지 않지만
    #: 매도우위가 아니므로 진입에는 중립 이상, 청산에는 악화 아님으로 본다.
    BUY_ONLY = "BUY_ONLY"
    #: 매수·매도 체결 증분이 모두 0이거나 누적값이 없다. 판단 근거가 없다.
    UNAVAILABLE = "UNAVAILABLE"


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _session_date(moment: datetime) -> date:
    return moment.astimezone(KST).date()


def _exchange_skew_seconds(received_at: datetime, exchange_time: time) -> float:
    local = received_at.astimezone(KST)
    exchange = datetime.combine(local.date(), exchange_time, tzinfo=KST)
    return abs((local - exchange).total_seconds())


@dataclass(frozen=True, slots=True)
class _TradeSample:
    received_at: datetime
    price: Decimal
    cumulative_volume: int
    cumulative_buy_volume: int | None
    cumulative_sell_volume: int | None
    session_vwap: Decimal | None


@dataclass(frozen=True, slots=True)
class _BookSample:
    received_at: datetime
    exchange_time: time
    valid: bool
    spread_bps: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    tick: BookTick


@dataclass(frozen=True, slots=True)
class TapeSnapshot:
    """한 종목의 관찰창 요약. 발행 프로세스가 매초 Redis에 남긴다."""

    symbol: str
    as_of: datetime
    session_date: date
    coverage_started_at: datetime | None
    last_trade_at: datetime | None
    last_book_at: datetime | None
    trade_count: int
    book_count: int
    invalid_book_count: int
    median_spread_bps: Decimal | None
    volume_power_state: VolumePowerState
    volume_power: Decimal | None
    buy_volume_delta: int | None
    sell_volume_delta: int | None
    price_change: Decimal | None
    last_price: Decimal | None
    session_vwap: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    dropped_frames: int
    resets: int
    last_reset_reason: str | None
    book_depth: tuple[BookLevel, ...] = ()
    total_bid_size: int | None = None
    total_ask_size: int | None = None

    @property
    def book_imbalance(self) -> Decimal | None:
        """총잔량 (매수-매도)/(매수+매도), 체결강도와 무관한 관측값."""
        bid, ask = self.total_bid_size, self.total_ask_size
        if bid is None or ask is None or bid + ask == 0:
            return None
        return Decimal(bid - ask) / Decimal(bid + ask)

    def coverage_seconds(self, now: datetime) -> float:
        if self.coverage_started_at is None:
            return 0.0
        return max(0.0, (now - self.coverage_started_at).total_seconds())

    def as_json(self) -> dict[str, object]:
        def _time(value: datetime | None) -> str | None:
            return None if value is None else value.astimezone(UTC).isoformat()

        def _dec(value: Decimal | None) -> str | None:
            return None if value is None else format(value, "f")

        return {
            "schemaVersion": REALTIME_TAPE_SCHEMA_VERSION,
            "symbol": self.symbol,
            "asOf": _time(self.as_of),
            "sessionDate": self.session_date.isoformat(),
            "coverageStartedAt": _time(self.coverage_started_at),
            "lastTradeAt": _time(self.last_trade_at),
            "lastBookAt": _time(self.last_book_at),
            "tradeCount": self.trade_count,
            "bookCount": self.book_count,
            "invalidBookCount": self.invalid_book_count,
            "medianSpreadBps": _dec(self.median_spread_bps),
            "volumePowerState": self.volume_power_state.value,
            "volumePower": _dec(self.volume_power),
            "buyVolumeDelta": self.buy_volume_delta,
            "sellVolumeDelta": self.sell_volume_delta,
            "priceChange": _dec(self.price_change),
            "lastPrice": _dec(self.last_price),
            "sessionVwap": _dec(self.session_vwap),
            "bestBid": _dec(self.best_bid),
            "bestAsk": _dec(self.best_ask),
            "bookDepth": [level.as_json() for level in self.book_depth],
            "totalBidSize": self.total_bid_size,
            "totalAskSize": self.total_ask_size,
            "bookImbalance": _dec(self.book_imbalance),
            "bookImbalanceStatus": (
                "AVAILABLE" if self.book_imbalance is not None else "UNAVAILABLE"
            ),
            "droppedFrames": self.dropped_frames,
            "resets": self.resets,
            "lastResetReason": self.last_reset_reason,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> TapeSnapshot:
        """저장된 snapshot을 엄격하게 되읽는다. 형식이 틀리면 ``ValueError``."""

        if payload.get("schemaVersion") != REALTIME_TAPE_SCHEMA_VERSION:
            raise ValueError("unsupported realtime tape schema")

        def _time(key: str, *, required: bool = False) -> datetime | None:
            raw = payload.get(key)
            if raw is None and not required:
                return None
            if not isinstance(raw, str):
                raise ValueError(f"{key} must be an ISO timestamp")
            return _aware_utc(datetime.fromisoformat(raw), key)

        def _dec(key: str) -> Decimal | None:
            raw = payload.get(key)
            if raw is None:
                return None
            if not isinstance(raw, str):
                raise ValueError(f"{key} must be a decimal string")
            try:
                value = Decimal(raw)
            except InvalidOperation as exc:
                raise ValueError(f"{key} must be a decimal string") from exc
            if not value.is_finite():
                raise ValueError(f"{key} must be finite")
            return value

        def _int(key: str, *, optional: bool = False) -> int | None:
            raw = payload.get(key)
            if raw is None and optional:
                return None
            if not isinstance(raw, int) or isinstance(raw, bool):
                raise ValueError(f"{key} must be an integer")
            return raw

        symbol = payload.get("symbol")
        session_raw = payload.get("sessionDate")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol is required")
        if not isinstance(session_raw, str):
            raise ValueError("sessionDate is required")
        reset_reason = payload.get("lastResetReason")
        if reset_reason is not None and not isinstance(reset_reason, str):
            raise ValueError("lastResetReason must be text")
        as_of = _time("asOf", required=True)
        assert as_of is not None
        depth = payload.get("bookDepth", [])
        if not isinstance(depth, list) or not all(
            isinstance(item, Mapping) for item in depth
        ):
            raise ValueError("bookDepth must be an array of levels")
        try:
            levels = tuple(BookLevel.from_json(item) for item in depth)
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise ValueError("bookDepth contains an invalid level") from exc
        return cls(
            symbol=symbol,
            as_of=as_of,
            session_date=date.fromisoformat(session_raw),
            coverage_started_at=_time("coverageStartedAt"),
            last_trade_at=_time("lastTradeAt"),
            last_book_at=_time("lastBookAt"),
            trade_count=_int("tradeCount") or 0,
            book_count=_int("bookCount") or 0,
            invalid_book_count=_int("invalidBookCount") or 0,
            median_spread_bps=_dec("medianSpreadBps"),
            volume_power_state=VolumePowerState(str(payload.get("volumePowerState"))),
            volume_power=_dec("volumePower"),
            buy_volume_delta=_int("buyVolumeDelta", optional=True),
            sell_volume_delta=_int("sellVolumeDelta", optional=True),
            price_change=_dec("priceChange"),
            last_price=_dec("lastPrice"),
            session_vwap=_dec("sessionVwap"),
            best_bid=_dec("bestBid"),
            best_ask=_dec("bestAsk"),
            dropped_frames=_int("droppedFrames") or 0,
            resets=_int("resets") or 0,
            last_reset_reason=reset_reason,
            book_depth=levels,
            total_bid_size=_int("totalBidSize", optional=True),
            total_ask_size=_int("totalAskSize", optional=True),
        )


@dataclass(slots=True)
class TapeWindow:
    """한 종목의 연속 관찰창.

    연속성은 "마지막 리셋 이후 체결과 호가가 모두 들어온 첫 시각"부터 센다.
    프레임 간격이 한도를 넘거나, 누적값이 거꾸로 가거나, 세션 날짜가 바뀌거나,
    연결을 다시 맺으면 창을 비우고 다음 프레임부터 다시 센다.
    """

    symbol: str
    config: RealtimeTapeConfig = DEFAULT_REALTIME_TAPE_CONFIG
    _trades: deque[_TradeSample] = field(default_factory=deque)
    _books: deque[_BookSample] = field(default_factory=deque)
    _session_date: date | None = None
    _first_trade_at: datetime | None = None
    _first_book_at: datetime | None = None
    _last_trade: _TradeSample | None = None
    _last_book: _BookSample | None = None
    _dropped: int = 0
    _resets: int = 0
    _last_reset_reason: str | None = None

    def reset(self, reason: str) -> None:
        self._trades.clear()
        self._books.clear()
        self._first_trade_at = None
        self._first_book_at = None
        self._last_trade = None
        self._last_book = None
        self._resets += 1
        self._last_reset_reason = reason

    def _enter_session(self, received_at: datetime) -> None:
        session = _session_date(received_at)
        if self._session_date is None:
            self._session_date = session
        elif session != self._session_date:
            self._session_date = session
            self.reset("session_changed")

    def _skewed(self, received_at: datetime, exchange_time: time) -> bool:
        return (
            _exchange_skew_seconds(received_at, exchange_time)
            > self.config.max_exchange_clock_skew_seconds
        )

    def on_trade(self, tick: TradeTick) -> bool:
        """체결을 창에 넣는다. 버린 프레임이면 ``False``."""

        received_at = _aware_utc(tick.received_at, "received_at")
        if tick.symbol != self.symbol or tick.price <= _ZERO:
            self._dropped += 1
            return False
        if self._skewed(received_at, tick.exchange_time):
            self._dropped += 1
            return False
        self._enter_session(received_at)
        previous = self._last_trade
        if previous is not None:
            if received_at < previous.received_at:
                self._dropped += 1
                return False
            if tick.cumulative_volume < previous.cumulative_volume:
                # 누적 거래량은 단조 증가다. 작아지면 늦게 도착한 옛 프레임이다.
                self._dropped += 1
                return False
            if tick.cumulative_volume == previous.cumulative_volume:
                # 거래량이 늘지 않은 프레임은 새 체결이 아니다(중복 전송).
                self._dropped += 1
                return False
            if _counter_went_backwards(
                previous.cumulative_buy_volume, tick.cumulative_buy_volume
            ) or _counter_went_backwards(
                previous.cumulative_sell_volume, tick.cumulative_sell_volume
            ):
                self.reset("cumulative_counter_reset")
            elif (
                received_at - previous.received_at
            ).total_seconds() > self.config.max_trade_gap_seconds:
                self.reset("trade_gap")
        sample = _TradeSample(
            received_at=received_at,
            price=tick.price,
            cumulative_volume=tick.cumulative_volume,
            cumulative_buy_volume=tick.cumulative_buy_volume,
            cumulative_sell_volume=tick.cumulative_sell_volume,
            session_vwap=tick.session_vwap,
        )
        self._trades.append(sample)
        self._last_trade = sample
        if self._first_trade_at is None:
            self._first_trade_at = received_at
        self._trim(received_at)
        return True

    def on_book(self, tick: BookTick) -> bool:
        """호가를 창에 넣는다. 버린 프레임이면 ``False``."""

        received_at = _aware_utc(tick.received_at, "received_at")
        if tick.symbol != self.symbol:
            self._dropped += 1
            return False
        if self._skewed(received_at, tick.exchange_time):
            self._dropped += 1
            return False
        self._enter_session(received_at)
        previous = self._last_book
        if previous is not None:
            if (
                received_at < previous.received_at
                or tick.exchange_time < previous.exchange_time
            ):
                self._dropped += 1
                return False
            if (
                received_at - previous.received_at
            ).total_seconds() > self.config.max_book_gap_seconds:
                self.reset("book_gap")
        valid, spread_bps = _book_quality(tick.best_bid, tick.best_ask)
        sample = _BookSample(
            received_at=received_at,
            exchange_time=tick.exchange_time,
            valid=valid,
            spread_bps=spread_bps,
            best_bid=tick.best_bid,
            best_ask=tick.best_ask,
            tick=tick,
        )
        self._books.append(sample)
        self._last_book = sample
        if self._first_book_at is None:
            self._first_book_at = received_at
        self._trim(received_at)
        return True

    def _trim(self, now: datetime) -> None:
        horizon = now - timedelta(seconds=self.config.observation_seconds)
        # 창 시작 직전의 체결 하나는 기준점으로 남긴다.
        while len(self._trades) >= 2 and self._trades[1].received_at <= horizon:
            self._trades.popleft()
        while self._books and self._books[0].received_at < horizon:
            self._books.popleft()

    def snapshot(self, now: datetime) -> TapeSnapshot:
        current = _aware_utc(now, "now")
        horizon = current - timedelta(seconds=self.config.observation_seconds)
        coverage_started_at = (
            max(self._first_trade_at, self._first_book_at)
            if self._first_trade_at is not None and self._first_book_at is not None
            else None
        )
        in_window_trades = [
            sample for sample in self._trades if sample.received_at > horizon
        ]
        baseline = next(
            (
                sample
                for sample in reversed(self._trades)
                if sample.received_at <= horizon
            ),
            self._trades[0] if self._trades else None,
        )
        last = self._last_trade
        state, power, buy_delta, sell_delta = _volume_power(baseline, last)
        books = [sample for sample in self._books if sample.received_at > horizon]
        spreads = [
            sample.spread_bps
            for sample in books
            if sample.valid and sample.spread_bps is not None
        ]
        last_book = self._last_book
        return TapeSnapshot(
            symbol=self.symbol,
            as_of=current,
            session_date=self._session_date or _session_date(current),
            coverage_started_at=coverage_started_at,
            last_trade_at=last.received_at if last is not None else None,
            last_book_at=last_book.received_at if last_book is not None else None,
            trade_count=len(in_window_trades),
            book_count=len(books),
            invalid_book_count=sum(1 for sample in books if not sample.valid),
            median_spread_bps=(
                Decimal(median(spreads)).quantize(Decimal("0.01")) if spreads else None
            ),
            volume_power_state=state,
            volume_power=power,
            buy_volume_delta=buy_delta,
            sell_volume_delta=sell_delta,
            price_change=(
                last.price - baseline.price
                if last is not None and baseline is not None
                else None
            ),
            last_price=last.price if last is not None else None,
            session_vwap=last.session_vwap if last is not None else None,
            best_bid=last_book.best_bid if last_book is not None else None,
            best_ask=last_book.best_ask if last_book is not None else None,
            book_depth=last_book.tick.depth if last_book is not None else (),
            total_bid_size=last_book.tick.total_bid_size
            if last_book is not None
            else None,
            total_ask_size=last_book.tick.total_ask_size
            if last_book is not None
            else None,
            dropped_frames=self._dropped,
            resets=self._resets,
            last_reset_reason=self._last_reset_reason,
        )


def _counter_went_backwards(previous: int | None, current: int | None) -> bool:
    return previous is not None and current is not None and current < previous


def _book_quality(
    best_bid: Decimal | None, best_ask: Decimal | None
) -> tuple[bool, Decimal | None]:
    """한쪽이 비었거나 매수호가가 매도호가 이상이면 무효 프레임이다.

    연속 매매 중 locked(같음)·crossed(역전) 호가는 즉시 체결돼 남지 않아야
    하므로 제출 정확성을 위해 둘 다 무효로 센다.
    """

    if best_bid is None or best_ask is None or best_bid <= _ZERO or best_ask <= _ZERO:
        return False, None
    if best_bid >= best_ask:
        return False, None
    mid = (best_bid + best_ask) / Decimal("2")
    return True, (best_ask - best_bid) / mid * _BPS


def _volume_power(
    baseline: _TradeSample | None, last: _TradeSample | None
) -> tuple[VolumePowerState, Decimal | None, int | None, int | None]:
    if (
        baseline is None
        or last is None
        or baseline.cumulative_buy_volume is None
        or baseline.cumulative_sell_volume is None
        or last.cumulative_buy_volume is None
        or last.cumulative_sell_volume is None
    ):
        return VolumePowerState.UNAVAILABLE, None, None, None
    buy_delta = last.cumulative_buy_volume - baseline.cumulative_buy_volume
    sell_delta = last.cumulative_sell_volume - baseline.cumulative_sell_volume
    if buy_delta < 0 or sell_delta < 0:
        return VolumePowerState.UNAVAILABLE, None, buy_delta, sell_delta
    if sell_delta == 0:
        if buy_delta > 0:
            return VolumePowerState.BUY_ONLY, None, buy_delta, sell_delta
        return VolumePowerState.UNAVAILABLE, None, buy_delta, sell_delta
    power = (Decimal(buy_delta) / Decimal(sell_delta) * _HUNDRED).quantize(
        Decimal("0.01")
    )
    return VolumePowerState.RATIO, power, buy_delta, sell_delta


class RealtimeEntryStatus(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"


@dataclass(frozen=True, slots=True)
class RealtimeEntryCheck:
    status: RealtimeEntryStatus
    reasons: tuple[str, ...]
    evidence: Mapping[str, object]

    @property
    def ready(self) -> bool:
        return self.status is RealtimeEntryStatus.READY


def _data_quality_reasons(
    snapshot: TapeSnapshot,
    now: datetime,
    config: RealtimeTapeConfig,
) -> list[str]:
    reasons: list[str] = []
    if (now - snapshot.as_of).total_seconds() > config.max_snapshot_age_seconds:
        reasons.append("snapshot_stale")
    if snapshot.session_date != _session_date(now):
        reasons.append("session_mismatch")
    if snapshot.coverage_seconds(now) < config.observation_seconds:
        reasons.append("warmup_incomplete")
    if (
        snapshot.last_trade_at is None
        or (now - snapshot.last_trade_at).total_seconds() > config.max_trade_age_seconds
    ):
        reasons.append("trade_stale")
    if (
        snapshot.last_book_at is None
        or (now - snapshot.last_book_at).total_seconds() > config.max_book_age_seconds
    ):
        reasons.append("book_stale")
    if snapshot.trade_count < config.minimum_trades:
        reasons.append("trades_sparse")
    return reasons


def _check_evidence(
    snapshot: TapeSnapshot | None,
    now: datetime,
    reasons: Sequence[str],
    config: RealtimeTapeConfig,
    **extra: object,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "schemaVersion": REALTIME_TAPE_SCHEMA_VERSION,
        "provider": REALTIME_ENTRY_PROVIDER,
        "venue": REALTIME_ENTRY_VENUE,
        "evaluatedAt": now.isoformat(),
        "observationSeconds": config.observation_seconds,
        "reasons": list(reasons),
        "snapshot": snapshot.as_json() if snapshot is not None else None,
    }
    evidence.update(extra)
    return evidence


def evaluate_realtime_entry(
    snapshot: TapeSnapshot | None,
    *,
    now: datetime,
    config: RealtimeTapeConfig = DEFAULT_REALTIME_TAPE_CONFIG,
) -> RealtimeEntryCheck:
    """BUY 제출을 허용할 만큼 체결·호가 흐름이 안정적인지 판정한다.

    snapshot이 없거나 형식이 틀리면 준비되지 않은 것으로 본다. 이 관문은 기존
    진입 임계값을 대신하지 않고 그 뒤에 덧붙는다.
    """

    current = _aware_utc(now, "now")
    if snapshot is None:
        reasons = ["snapshot_unavailable"]
        return RealtimeEntryCheck(
            RealtimeEntryStatus.NOT_READY,
            tuple(reasons),
            _check_evidence(None, current, reasons, config),
        )
    reasons = _data_quality_reasons(snapshot, current, config)
    if snapshot.invalid_book_count > 0:
        reasons.append("book_invalid_frames")
    if snapshot.median_spread_bps is None:
        reasons.append("spread_unavailable")
    elif snapshot.median_spread_bps > config.max_median_spread_bps:
        reasons.append("spread_wide")
    latest_valid, latest_spread = _book_quality(snapshot.best_bid, snapshot.best_ask)
    if not latest_valid or latest_spread is None:
        reasons.append("latest_book_invalid")
    elif latest_spread > config.max_median_spread_bps:
        reasons.append("latest_spread_wide")
    if snapshot.volume_power_state is VolumePowerState.UNAVAILABLE:
        reasons.append("volume_power_unavailable")
    elif (
        snapshot.volume_power_state is VolumePowerState.RATIO
        and snapshot.volume_power is not None
        and snapshot.volume_power < config.entry_min_volume_power
    ):
        reasons.append("volume_power_weak")
    if snapshot.price_change is None:
        reasons.append("price_change_unavailable")
    elif snapshot.price_change < _ZERO:
        reasons.append("price_falling")
    if snapshot.session_vwap is None or snapshot.session_vwap <= _ZERO:
        reasons.append("session_vwap_unavailable")
    elif snapshot.last_price is None or snapshot.last_price < snapshot.session_vwap:
        reasons.append("below_session_vwap")
    status = RealtimeEntryStatus.READY if not reasons else RealtimeEntryStatus.NOT_READY
    return RealtimeEntryCheck(
        status,
        tuple(reasons),
        _check_evidence(snapshot, current, reasons, config, status=status.value),
    )


@dataclass(frozen=True, slots=True)
class RealtimeTrendExitDecision:
    triggered: bool
    reasons: tuple[str, ...]
    evidence: Mapping[str, object]
    reference_price: Decimal | None


def moving_average_of_closes(
    closes: Sequence[Decimal],
    *,
    bars: int = DEFAULT_REALTIME_TAPE_CONFIG.ma_bars,
) -> Decimal | None:
    """완료 봉 종가의 단순이동평균. 봉이 모자라면 ``None``."""

    if bars < 1 or len(closes) < bars:
        return None
    window = closes[-bars:]
    return sum(window, _ZERO) / Decimal(bars)


def evaluate_realtime_trend_exit(
    snapshot: TapeSnapshot | None,
    *,
    now: datetime,
    entry_price: Decimal,
    current_stop: Decimal,
    completed_closes: Sequence[Decimal],
    config: RealtimeTapeConfig = DEFAULT_REALTIME_TAPE_CONFIG,
) -> RealtimeTrendExitDecision:
    """평가익 보유분의 흐름 악화 조기 청산 여부.

    네 조건이 모두 동시에 맞아야 한다: 60초 체결강도 < 100, 60초 가격 하락,
    당일 VWAP 아래, 완료 5분봉 종가 MA5 아래. 현재가가 진입가보다 낮으면
    기존 손절선이 담당하므로 이 신호를 내지 않는다. 현재가가 저장된 보호선
    이하이면 기존 손절이 먼저이므로 역시 내지 않는다.
    """

    current = _aware_utc(now, "now")
    ma = moving_average_of_closes(completed_closes, bars=config.ma_bars)
    extra: dict[str, object] = {
        "entryPrice": format(entry_price, "f"),
        "currentStop": format(current_stop, "f"),
        "movingAverageBars": config.ma_bars,
        "movingAverage": format(ma, "f") if ma is not None else None,
    }
    if snapshot is None:
        reasons = ["snapshot_unavailable"]
        return RealtimeTrendExitDecision(
            False,
            tuple(reasons),
            _check_evidence(None, current, reasons, config, **extra),
            None,
        )
    blockers = _data_quality_reasons(snapshot, current, config)
    last = snapshot.last_price
    if last is None:
        blockers.append("last_price_unavailable")
    else:
        if last < entry_price:
            blockers.append("below_entry_price")
        if last <= current_stop:
            blockers.append("protective_stop_owns_exit")
    conditions: list[str] = []
    if (
        snapshot.volume_power_state is VolumePowerState.RATIO
        and snapshot.volume_power is not None
        and snapshot.volume_power < config.exit_max_volume_power
    ):
        conditions.append("volume_power_weak")
    if snapshot.price_change is not None and snapshot.price_change < _ZERO:
        conditions.append("price_falling")
    if (
        last is not None
        and snapshot.session_vwap is not None
        and snapshot.session_vwap > _ZERO
        and last < snapshot.session_vwap
    ):
        conditions.append("below_session_vwap")
    if ma is not None and last is not None and last < ma:
        conditions.append("below_moving_average")
    triggered = not blockers and len(conditions) == 4
    reasons = tuple(blockers) if blockers else tuple(conditions)
    return RealtimeTrendExitDecision(
        triggered,
        reasons,
        _check_evidence(
            snapshot,
            current,
            reasons,
            config,
            triggered=triggered,
            conditions=conditions,
            **extra,
        ),
        last if triggered else None,
    )


def requires_realtime_entry(*, source: str, market: str, action: str) -> bool:
    """배포 전 marker 없는 추천도 같은 KRX 자동매수 정책을 적용한다."""
    return source == "kasset-automation" and market == "KRX" and action == "BUY"


def realtime_entry_requirement_evidence(
    config: RealtimeTapeConfig = DEFAULT_REALTIME_TAPE_CONFIG,
) -> dict[str, object]:
    """KRX BUY 추천에 남기는 요구 marker."""

    return {
        "title": "Realtime KRX tape confirmation required",
        "source": "kasset_realtime_tape",
        "kind": REALTIME_ENTRY_EVIDENCE_KIND,
        "required": True,
        "provider": REALTIME_ENTRY_PROVIDER,
        "venue": REALTIME_ENTRY_VENUE,
        "observationSeconds": config.observation_seconds,
        "schemaVersion": REALTIME_TAPE_SCHEMA_VERSION,
    }


__all__ = [
    "DEFAULT_REALTIME_TAPE_CONFIG",
    "KST",
    "REALTIME_ENTRY_EVIDENCE_KIND",
    "REALTIME_TAPE_SCHEMA_VERSION",
    "REALTIME_TREND_EXIT_KIND",
    "BookTick",
    "RealtimeEntryCheck",
    "RealtimeEntryStatus",
    "RealtimeTapeConfig",
    "RealtimeTrendExitDecision",
    "TapeSnapshot",
    "TapeWindow",
    "TradeTick",
    "VolumePowerState",
    "evaluate_realtime_entry",
    "evaluate_realtime_trend_exit",
    "moving_average_of_closes",
    "realtime_entry_requirement_evidence",
    "requires_realtime_entry",
]
