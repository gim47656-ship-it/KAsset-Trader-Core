"""Offline fixed-entry coverage audit; run only in the network-disabled app image."""
import json
import sys
from collections import Counter

sys.argv = ['/work/research_backtest.py', '/work']
from research_backtest import Arm, complete, daily_run, fixed_entries, load


def main():
    daily, intraday = load()
    original, _ = daily_run(daily, Arm())
    positions = fixed_entries(original)
    all_days = sorted(
        day for day in {day for series in intraday.values() for day in series}
        if any(complete(series.get(day, [])) for series in intraday.values())
    )
    counts = Counter()
    eligible_dates = Counter()
    for symbol, at in positions:
        if at.date() < all_days[0]:
            counts['entry_before_complete_5m_window'] += 1
        elif at.date() > all_days[-1]:
            counts['entry_after_complete_5m_window'] += 1
        elif any(not complete(intraday[symbol].get(day, [])) for day in all_days if day >= at.date()):
            counts['in_window_missing_or_noncontiguous_77_buckets'] += 1
        else:
            counts['eligible'] += 1
            eligible_dates[str(at.date())] += 1
    print(json.dumps({'total': len(positions), 'counts': counts, 'eligible_dates': eligible_dates, 'first_complete': str(all_days[0]), 'last_complete': str(all_days[-1])}, sort_keys=True))


if __name__ == '__main__':
    main()
