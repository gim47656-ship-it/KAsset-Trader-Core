"""NH 국내 실시간 관찰창·진입·흐름 악화 청산 판정의 결정적 replay."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.extensions.kasset.automation.realtime_tape import (
    KST,
    BookTick,
    RealtimeEntryStatus,
    TapeSnapshot,
    TapeWindow,
    TradeTick,
    VolumePowerState,
    evaluate_realtime_entry,
    evaluate_realtime_trend_exit,
    requires_realtime_entry,
)
from app.extensions.kasset.nhplug.protocol import parse_push

D = Decimal
T0 = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)  # 11:00 KST, KRX 정규장
SYMBOL = "005930"

# NH krstock/openapi.json x-realtime-channels push_example 원형(oc, ob 일부).
_OFFICIAL_OC = {
    "header": {"tr_cd": "oc", "tr_key": "005940"},
    "body": {
        "code": "005940",
        "time": "13:58:26",
        "sign": "2",
        "change": "2450",
        "price": "31700",
        "chrate": "8.38",
        "high": "32200",
        "low": "29600",
        "offer": "31750",
        "bid": "31700",
        "volume": "660224",
        "volrate": "85.65",
        "movolume": "1",
        "value": "20867",
        "open": "29800",
        "avgprice": "31607",
        "janggubun": "0",
        "bidrate": "52.09",
        "volpower": "109.75",
        "new_volume": "660224",
        "bidvolall": "343916",
        "offvolall": "313366",
        "kospigb": "1",
        "value_won": "20867545950",
    },
}
_OFFICIAL_OB = {
    "header": {"tr_cd": "ob", "tr_key": "005940"},
    "body": {
        "code": "005940",
        "hotime": "13:49:56",
        "offer": "31900",
        "bid": "31850",
        "offerrem": "298",
        "bidrem": "188",
        "T_offerrem": "19915",
        "T_bidrem": "8490",
    },
}


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _exchange(moment: datetime) -> object:
    return moment.astimezone(KST).time().replace(microsecond=0)


def _trade(
    seconds: float,
    *,
    price: str,
    volume: int,
    buy: int | None,
    sell: int | None,
    vwap: str | None = "69900",
    symbol: str = SYMBOL,
) -> TradeTick:
    moment = _at(seconds)
    return TradeTick(
        symbol=symbol,
        received_at=moment,
        exchange_time=_exchange(moment),  # type: ignore[arg-type]
        price=D(price),
        cumulative_volume=volume,
        cumulative_buy_volume=buy,
        cumulative_sell_volume=sell,
        session_vwap=D(vwap) if vwap is not None else None,
    )


def _book(
    seconds: float,
    *,
    bid: str | None = "70000",
    ask: str | None = "70100",
    symbol: str = SYMBOL,
) -> BookTick:
    moment = _at(seconds)
    return BookTick(
        symbol=symbol,
        received_at=moment,
        exchange_time=_exchange(moment),  # type: ignore[arg-type]
        best_bid=D(bid) if bid is not None else None,
        best_ask=D(ask) if ask is not None else None,
        best_bid_size=100,
        best_ask_size=100,
        total_bid_size=10_000,
        total_ask_size=10_000,
    )


@dataclass(frozen=True)
class _Tape:
    """초 단위 시나리오: 매초 호가, 2초마다 체결."""

    seconds: int
    start_price: int = 70000
    price_step: int = 5
    buy_per_trade: int = 30
    sell_per_trade: int = 20
    start_second: int = 0


def _replay(window: TapeWindow, tape: _Tape, *, volume: int = 10_000) -> int:
    buy, sell = 5_000, 4_000
    price = tape.start_price
    for second in range(tape.start_second, tape.start_second + tape.seconds + 1):
        window.on_book(_book(second))
        if second % 2 == 0:
            volume += tape.buy_per_trade + tape.sell_per_trade
            buy += tape.buy_per_trade
            sell += tape.sell_per_trade
            price += tape.price_step
            window.on_trade(
                _trade(second, price=str(price), volume=volume, buy=buy, sell=sell)
            )
    return volume


def _ready_window() -> TapeWindow:
    window = TapeWindow(SYMBOL)
    _replay(window, _Tape(seconds=62))
    return window


@pytest.mark.unit
def test_official_push_examples_parse_into_ticks_without_inventing_fields() -> None:
    received = datetime(2026, 8, 31, 4, 58, 27, tzinfo=UTC)  # 13:58:27 KST

    trade = parse_push(_OFFICIAL_OC, received_at=received)
    book = parse_push(_OFFICIAL_OB, received_at=received)

    assert isinstance(trade, TradeTick)
    assert trade.symbol == "005940"
    assert trade.price == D("31700")
    assert trade.cumulative_volume == 660224
    assert trade.cumulative_buy_volume == 343916
    assert trade.cumulative_sell_volume == 313366
    assert trade.session_vwap == D("31607")
    assert isinstance(book, BookTick)
    assert (book.best_bid, book.best_ask) == (D("31850"), D("31900"))
    assert (book.total_bid_size, book.total_ask_size) == (8490, 19915)

    # 선택 필드가 빠진 프레임은 None으로 두고, 가격이 이상하면 버린다.
    partial = {
        "header": {"tr_cd": "oc"},
        "body": {"code": "005940", "time": "135826", "price": "31700", "volume": "5"},
    }
    parsed = parse_push(partial, received_at=received)
    assert isinstance(parsed, TradeTick)
    assert parsed.cumulative_buy_volume is None
    assert parsed.session_vwap is None
    negative = {"header": {"tr_cd": "oc"}, "body": {**_OFFICIAL_OC["body"]}}
    negative["body"]["price"] = "-31700"
    assert parse_push(negative, received_at=received) is None
    other_venue = {"header": {"tr_cd": "mc"}, "body": {**_OFFICIAL_OC["body"]}}
    assert parse_push(other_venue, received_at=received) is None


@pytest.mark.unit
def test_official_example_volume_power_matches_cumulative_buy_sell_ratio() -> None:
    body = _OFFICIAL_OC["body"]
    buy, sell, volume = (
        D(str(body["bidvolall"])),
        D(str(body["offvolall"])),
        D(str(body["volume"])),
    )
    assert (buy / sell * 100).quantize(D("0.01")) == D(str(body["volpower"]))
    assert (buy / volume * 100).quantize(D("0.01")) == D(str(body["bidrate"]))


@pytest.mark.unit
def test_warmup_needs_sixty_continuous_seconds_of_trade_and_book() -> None:
    window = TapeWindow(SYMBOL)
    _replay(window, _Tape(seconds=58))

    early = evaluate_realtime_entry(window.snapshot(_at(58.5)), now=_at(58.5))
    assert early.status is RealtimeEntryStatus.NOT_READY
    assert early.reasons == ("warmup_incomplete",)

    # 같은 흐름이 60초를 넘기면 다른 조건 변화 없이 준비 상태가 된다.
    ready = evaluate_realtime_entry(_ready_window().snapshot(_at(62.5)), now=_at(62.5))
    assert ready.status is RealtimeEntryStatus.READY, ready.reasons
    assert ready.evidence["snapshot"]["volumePowerState"] == "RATIO"  # type: ignore[index]


@pytest.mark.unit
def test_stable_tape_passes_and_each_deterioration_blocks_entry() -> None:
    stable = _ready_window().snapshot(_at(62.5))
    assert evaluate_realtime_entry(stable, now=_at(62.5)).ready
    assert stable.volume_power == D("150.00")

    sellers = TapeWindow(SYMBOL)
    _replay(sellers, _Tape(seconds=62, buy_per_trade=10, sell_per_trade=40))
    assert (
        "volume_power_weak"
        in evaluate_realtime_entry(sellers.snapshot(_at(62.5)), now=_at(62.5)).reasons
    )

    falling = TapeWindow(SYMBOL)
    _replay(falling, _Tape(seconds=62, price_step=-5))
    falling_reasons = evaluate_realtime_entry(
        falling.snapshot(_at(62.5)), now=_at(62.5)
    ).reasons
    assert "price_falling" in falling_reasons

    below_vwap = TapeWindow(SYMBOL)
    _replay(below_vwap, _Tape(seconds=62, start_price=69000))
    assert (
        "below_session_vwap"
        in evaluate_realtime_entry(
            below_vwap.snapshot(_at(62.5)), now=_at(62.5)
        ).reasons
    )


@pytest.mark.unit
def test_buy_only_window_is_neutral_but_empty_flow_is_unavailable() -> None:
    buy_only = TapeWindow(SYMBOL)
    _replay(buy_only, _Tape(seconds=62, sell_per_trade=0))
    snapshot = buy_only.snapshot(_at(62.5))
    assert snapshot.volume_power_state is VolumePowerState.BUY_ONLY
    assert snapshot.volume_power is None
    assert evaluate_realtime_entry(snapshot, now=_at(62.5)).ready

    flat = TapeWindow(SYMBOL)
    volume = 10_000
    for second in range(0, 63):
        flat.on_book(_book(second))
        if second % 2 == 0:
            volume += 10
            # 누적 매수·매도가 움직이지 않는 체결(보합)만 있다.
            flat.on_trade(
                _trade(second, price="70100", volume=volume, buy=5000, sell=4000)
            )
    flat_snapshot = flat.snapshot(_at(62.5))
    assert flat_snapshot.volume_power_state is VolumePowerState.UNAVAILABLE
    assert (
        "volume_power_unavailable"
        in evaluate_realtime_entry(flat_snapshot, now=_at(62.5)).reasons
    )

    missing = TapeWindow(SYMBOL)
    for second in range(0, 63):
        missing.on_book(_book(second))
        if second % 2 == 0:
            missing.on_trade(
                _trade(
                    second, price="70100", volume=10_000 + second, buy=None, sell=None
                )
            )
    assert (
        missing.snapshot(_at(62.5)).volume_power_state is VolumePowerState.UNAVAILABLE
    )


@pytest.mark.unit
def test_quiet_thin_name_keeps_its_window_and_can_pass_entry() -> None:
    # 2026-09-30 장중: 중·소형주는 호가 5초·체결 15초 공백 리셋으로 60초를
    # 채우지 못했다. 2만원대(호가단위 50원) 종목의 2틱 스프레드, 8초 간격 호가,
    # 20초 간격 체결도 흐름 조건이 맞으면 통과해야 한다.
    window = TapeWindow(SYMBOL)
    for second in range(0, 65, 8):
        window.on_book(_book(second, bid="20000", ask="20100"))
    for index, second in enumerate(range(0, 61, 20)):
        window.on_trade(
            _trade(
                second,
                price=str(20050 + index * 50),
                volume=10_000 + index * 50,
                buy=5_000 + index * 30,
                sell=4_000 + index * 20,
                vwap="20000",
            )
        )

    for moment in (_at(64.5), _at(75)):
        snapshot = window.snapshot(moment)
        assert snapshot.resets == 0
        assert snapshot.last_reset_reason is None
        decision = evaluate_realtime_entry(snapshot, now=moment)
        assert decision.ready, decision.reasons


@pytest.mark.unit
def test_duplicate_and_out_of_order_frames_do_not_change_the_window() -> None:
    window = _ready_window()
    before = window.snapshot(_at(62.5))
    last_volume = 10_000 + 32 * 50

    assert not window.on_trade(
        _trade(62.2, price="99999", volume=last_volume, buy=1, sell=1)
    )
    assert not window.on_trade(
        _trade(62.3, price="99999", volume=last_volume - 50, buy=1, sell=1)
    )
    assert not window.on_book(_book(61.0))

    after = window.snapshot(_at(62.5))
    assert after.last_price == before.last_price
    assert after.trade_count == before.trade_count
    assert after.dropped_frames == before.dropped_frames + 3
    assert after.resets == before.resets


@pytest.mark.unit
def test_cumulative_counter_reset_and_session_change_restart_observation() -> None:
    window = _ready_window()
    window.on_trade(_trade(63, price="70200", volume=99_999, buy=10, sell=10))
    assert window.snapshot(_at(63.5)).last_reset_reason == "cumulative_counter_reset"

    next_day = _ready_window()
    tomorrow = _at(86_400)
    next_day.on_book(
        BookTick(
            symbol=SYMBOL,
            received_at=tomorrow,
            exchange_time=tomorrow.astimezone(KST).time().replace(microsecond=0),
            best_bid=D("70000"),
            best_ask=D("70100"),
            best_bid_size=1,
            best_ask_size=1,
            total_bid_size=1,
            total_ask_size=1,
        )
    )
    assert next_day.snapshot(tomorrow).last_reset_reason == "session_changed"


@pytest.mark.unit
def test_exchange_clock_skew_frames_are_dropped_as_stale() -> None:
    window = TapeWindow(SYMBOL)
    stale = BookTick(
        symbol=SYMBOL,
        received_at=_at(0),
        exchange_time=(_at(-120)).astimezone(KST).time().replace(microsecond=0),
        best_bid=D("70000"),
        best_ask=D("70100"),
        best_bid_size=1,
        best_ask_size=1,
        total_bid_size=1,
        total_ask_size=1,
    )
    assert not window.on_book(stale)
    assert window.snapshot(_at(0)).book_count == 0


@pytest.mark.unit
def test_stale_publisher_or_book_and_invalid_book_block_immediately() -> None:
    snapshot = _ready_window().snapshot(_at(62.5))
    # 4초 뒤 재확인: 발행이 멈췄고 마지막 호가도 오래됐다.
    later = evaluate_realtime_entry(snapshot, now=_at(66.6))
    assert later.status is RealtimeEntryStatus.NOT_READY
    assert "snapshot_stale" in later.reasons

    # 발행은 살아 있어도 호가·체결이 관찰창(60초) 밖이면 판단하지 않는다.
    quiet_too_long = _ready_window().snapshot(_at(130))
    assert {"book_stale", "trade_stale", "trades_sparse"} <= set(
        evaluate_realtime_entry(quiet_too_long, now=_at(130)).reasons
    )

    crossed = _ready_window()
    crossed.on_book(_book(62.8, bid="70200", ask="70100"))
    assert (
        "book_invalid_frames"
        in evaluate_realtime_entry(crossed.snapshot(_at(63.0)), now=_at(63.0)).reasons
    )

    locked = _ready_window()
    locked.on_book(_book(62.8, bid="70100", ask="70100"))
    assert (
        "book_invalid_frames"
        in evaluate_realtime_entry(locked.snapshot(_at(63.0)), now=_at(63.0)).reasons
    )

    wide = TapeWindow(SYMBOL)
    for second in range(0, 63):
        wide.on_book(_book(second, bid="70000", ask="70500"))
        if second % 2 == 0:
            wide.on_trade(
                _trade(
                    second,
                    price=str(70000 + second),
                    volume=10_000 + second * 50,
                    buy=5_000 + second * 30,
                    sell=4_000 + second * 20,
                )
            )
    assert (
        "spread_wide"
        in evaluate_realtime_entry(wide.snapshot(_at(62.5)), now=_at(62.5)).reasons
    )

    assert evaluate_realtime_entry(None, now=_at(0)).reasons == (
        "snapshot_unavailable",
    )


@pytest.mark.unit
def test_snapshot_json_round_trip_and_malformed_payload_rejection() -> None:
    snapshot = _ready_window().snapshot(_at(62.5))
    assert TapeSnapshot.from_json(snapshot.as_json()) == snapshot

    broken = snapshot.as_json()
    broken["schemaVersion"] = "other"
    with pytest.raises(ValueError):
        TapeSnapshot.from_json(broken)
    bad_number = snapshot.as_json()
    bad_number["lastPrice"] = "NaN"
    with pytest.raises(ValueError):
        TapeSnapshot.from_json(bad_number)


def _deteriorating_window() -> TapeWindow:
    window = TapeWindow(SYMBOL)
    buy, sell, volume, price = 5_000, 4_000, 10_000, 70_000
    for second in range(0, 63):
        window.on_book(_book(second, bid=str(price - 50), ask=str(price + 50)))
        if second % 2 == 0:
            buy += 10
            sell += 40
            volume += 50
            price -= 5
            window.on_trade(
                _trade(
                    second,
                    price=str(price),
                    volume=volume,
                    buy=buy,
                    sell=sell,
                    vwap="70100",
                )
            )
    return window


def _closes(values: Iterable[str]) -> list[Decimal]:
    return [D(value) for value in values]


@pytest.mark.unit
def test_trend_exit_needs_all_four_conditions_in_profit_above_stop() -> None:
    snapshot = _deteriorating_window().snapshot(_at(62.5))
    last = snapshot.last_price
    assert last == D("69840")
    ma_above = _closes(["70100", "70050", "70000", "69990", "69980"])

    triggered = evaluate_realtime_trend_exit(
        snapshot,
        now=_at(62.5),
        entry_price=D("69000"),
        current_stop=D("67000"),
        completed_closes=ma_above,
    )
    assert triggered.triggered
    assert triggered.reference_price == last
    assert set(triggered.reasons) == {
        "volume_power_weak",
        "price_falling",
        "below_session_vwap",
        "below_moving_average",
    }

    # 손실 구간은 기존 손절선이 담당한다.
    losing = evaluate_realtime_trend_exit(
        snapshot,
        now=_at(62.5),
        entry_price=D("70500"),
        current_stop=D("67000"),
        completed_closes=ma_above,
    )
    assert not losing.triggered
    assert "below_entry_price" in losing.reasons

    # 2026-09-30: 진입가 바로 위 흐름 청산이 왕복 비용(0.21%)으로 손실이 됐다.
    # 69840은 진입가 69700의 +0.2%라 최소 평가익 0.3% 미만이다.
    thin_profit = evaluate_realtime_trend_exit(
        snapshot,
        now=_at(62.5),
        entry_price=D("69700"),
        current_stop=D("67000"),
        completed_closes=ma_above,
    )
    assert not thin_profit.triggered
    assert thin_profit.reasons == ("below_min_profit",)
    # 최소 평가익을 넘으면(69600의 +0.34%) 같은 흐름에서 청산한다.
    assert evaluate_realtime_trend_exit(
        snapshot,
        now=_at(62.5),
        entry_price=D("69600"),
        current_stop=D("67000"),
        completed_closes=ma_above,
    ).triggered

    # 현재가가 저장된 보호선 이하면 손절이 먼저다(경쟁 금지).
    stop_first = evaluate_realtime_trend_exit(
        snapshot,
        now=_at(62.5),
        entry_price=D("69000"),
        current_stop=D("69840"),
        completed_closes=ma_above,
    )
    assert not stop_first.triggered
    assert "protective_stop_owns_exit" in stop_first.reasons

    # MA5를 만들 완료 5분봉이 부족하면 신호를 내지 않는다.
    no_ma = evaluate_realtime_trend_exit(
        snapshot,
        now=_at(62.5),
        entry_price=D("69000"),
        current_stop=D("67000"),
        completed_closes=ma_above[:4],
    )
    assert not no_ma.triggered

    # 가격이 MA5 위에 있으면 세 조건만으로는 청산하지 않는다.
    ma_below = _closes(["69700", "69700", "69700", "69700", "69700"])
    above_ma = evaluate_realtime_trend_exit(
        snapshot,
        now=_at(62.5),
        entry_price=D("69000"),
        current_stop=D("67000"),
        completed_closes=ma_below,
    )
    assert not above_ma.triggered


@pytest.mark.unit
def test_trend_exit_ignores_stable_tape_and_unwarmed_observation() -> None:
    stable = _ready_window().snapshot(_at(62.5))
    assert not evaluate_realtime_trend_exit(
        stable,
        now=_at(62.5),
        entry_price=D("69000"),
        current_stop=D("67000"),
        completed_closes=_closes(["80000"] * 5),
    ).triggered

    young = TapeWindow(SYMBOL)
    _replay(young, _Tape(seconds=20, buy_per_trade=5, sell_per_trade=50, price_step=-5))
    decision = evaluate_realtime_trend_exit(
        young.snapshot(_at(20.5)),
        now=_at(20.5),
        entry_price=D("69000"),
        current_stop=D("67000"),
        completed_closes=_closes(["80000"] * 5),
    )
    assert not decision.triggered
    assert "warmup_incomplete" in decision.reasons


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "market", "action", "required"),
    [
        ("kasset-automation", "KRX", "BUY", True),
        ("kasset-automation", "US", "BUY", False),
        ("manual", "KRX", "BUY", False),
        ("kasset-automation", "KRX", "SELL", False),
    ],
)
def test_realtime_requirement_uses_policy_scope_not_marker(
    source: str, market: str, action: str, required: bool
) -> None:
    assert (
        requires_realtime_entry(source=source, market=market, action=action) is required
    )


@pytest.mark.unit
def test_latest_wide_book_blocks_even_when_window_median_is_narrow() -> None:
    window = _ready_window()
    window.on_book(_book(62.8, bid="70000", ask="70700"))
    snapshot = window.snapshot(_at(63))
    assert snapshot.median_spread_bps < D("30")
    decision = evaluate_realtime_entry(snapshot, now=_at(63))
    assert not decision.ready
    assert decision.reasons == ("latest_spread_wide",)


@pytest.mark.unit
def test_depth_and_total_sizes_survive_snapshot_round_trip() -> None:
    body = {
        "code": SYMBOL,
        "hotime": "11:01:03",
        "T_bidrem": "1500",
        "T_offerrem": "500",
    }
    prefixes = ("", "P_", "S_", "S4_", "S5_", "S6_", "S7_", "S8_", "S9_", "S10_")
    for index, prefix in enumerate(prefixes):
        body.update(
            {
                f"{prefix}bid": str(70000 - index * 100),
                f"{prefix}offer": str(70100 + index * 100),
                f"{prefix}bidrem": str(150 + index),
                f"{prefix}offerrem": str(50 + index),
            }
        )
    tick = parse_push({"header": {"tr_cd": "ob"}, "body": body}, received_at=_at(63))
    assert isinstance(tick, BookTick)
    window = _ready_window()
    window.on_book(tick)
    snapshot = TapeSnapshot.from_json(window.snapshot(_at(63)).as_json())
    assert [(level.bid_size, level.ask_size) for level in snapshot.book_depth] == [
        (150 + index, 50 + index) for index in range(10)
    ]
    assert snapshot.book_depth[9].bid == D("69100")
    assert snapshot.book_depth[9].ask == D("71000")
    assert (snapshot.total_bid_size, snapshot.total_ask_size) == (1500, 500)
    assert snapshot.book_imbalance == D("0.5")
    # 총잔량 불균형은 관측 evidence일 뿐 진입 조건이 아니다.
    body["T_bidrem"], body["T_offerrem"] = "0", "500"
    body.pop("S10_bidrem")
    tick = parse_push({"header": {"tr_cd": "ob"}, "body": body}, received_at=_at(63.1))
    assert isinstance(tick, BookTick)
    window.on_book(tick)
    snapshot = window.snapshot(_at(63.1))
    assert snapshot.book_depth[9].bid_size is None
    assert snapshot.book_imbalance == D("-1")
    assert evaluate_realtime_entry(snapshot, now=_at(63.1)).ready
    body.pop("T_offerrem")
    tick = parse_push({"header": {"tr_cd": "ob"}, "body": body}, received_at=_at(63.2))
    assert isinstance(tick, BookTick)
    window.on_book(tick)
    assert window.snapshot(_at(63.2)).as_json()["bookImbalanceStatus"] == "UNAVAILABLE"
