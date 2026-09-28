import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
sys.path.insert(0, '/work')
sys.argv = ['/work/research_backtest.py', '/work']
from research_backtest import Arm, D, PriceBar, replay

symbol = 'SMOKE'
entry_day = date(2026, 9, 21)
days = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)]
entry_at = datetime(2026, 9, 21, tzinfo=UTC)
daily = {symbol: []}
for n in range(15, 0, -1):
    at = entry_at - timedelta(days=n)
    daily[symbol].append(PriceBar(at, D(100), D(101), D(99), D(100), D(1000)))
for day in days:
    at = datetime(day.year, day.month, day.day, tzinfo=UTC)
    daily[symbol].append(PriceBar(at, D(100), D(101), D(99), D(100), D(1000)))
intraday = {symbol: {}}
for day in days:
    at = datetime(day.year, day.month, day.day, tzinfo=UTC)
    bars = []
    for n in range(77):
        if day == days[2]:
            price = (D(101), D('101.5'), D('100.5'), D(101))
        else:
            high = D('101.5') if day == days[1] and n == 76 else D('100.5')
            price = (D(100), high, D('99.5'), D(100))
        bars.append(PriceBar(at + timedelta(minutes=5*n), *price, D(100)))
    intraday[symbol][day] = bars
for arm in (Arm(hold=2), Arm(hold=2, force=True)):
    result = replay(symbol, entry_at, D(100), D(10), daily, intraday, days, arm)
    print(arm.label(), result['fills'])
    if arm.force:
        assert [(kind, qty) for kind, _, qty, _ in result['fills']] == [('TIME_LIMIT', '10')]
    else:
        assert [(kind, qty) for kind, _, qty, _ in result['fills']] == [('PARTIAL_SELL', '3')]
print('deadline_collision_ok')
