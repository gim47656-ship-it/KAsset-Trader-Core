"""Offline second-profit and post-partial high-water research smoke."""
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

sys.argv = ['/work/research_backtest.py', '/work']
from research_backtest import Arm, D, PriceBar, replay

symbol = 'SMOKE'
entry_at = datetime(2026, 9, 21, tzinfo=UTC)
day = entry_at.date()
daily = {symbol: [PriceBar(entry_at - timedelta(days=n), D(100), D(101), D(99), D(100), D(1000)) for n in range(15, 0, -1)]}
daily[symbol].append(PriceBar(entry_at, D(100), D(103), D(99), D(101), D(1000)))
path = [
    ('100', '100.5', '99.5', '100'),
    ('100', '101.2', '99.5', '100.5'),
    ('100.5', '101.5', '100.2', '101'),
    ('101', '102.2', '100.5', '101.5'),
    ('101.5', '103', '101', '102'),
    ('102', '102.5', '100.8', '101.5'),
    ('101.5', '102', '101', '101.5'),
]
path.extend([('101.5', '102', '101', '101.5')] * (77 - len(path)))
intraday = {symbol: {day: [PriceBar(entry_at + timedelta(minutes=5*n), *(D(value) for value in values), D(100)) for n, values in enumerate(path)]}}
result = replay(symbol, entry_at, D(100), D(100), daily, intraday, [day], Arm(stop=2, hold=3, second=D(1), trail=D(1)))
assert result is not None and result['closed']
legs = [(kind, qty) for kind, _, qty, _ in result['fills']]
print(legs)
assert legs == [('PARTIAL_SELL', '30'), ('SECOND_PARTIAL_SELL', '21'), ('TRAILING_STOP', '49')]
print('second_and_trail_ok')
