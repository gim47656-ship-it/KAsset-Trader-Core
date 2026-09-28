import csv
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

buckets = defaultdict(list)
with Path('/work/intraday.csv').open(newline='', encoding='utf-8') as stream:
    assert stream.readline().strip() == 'SET'
    for row in csv.DictReader(stream):
        buckets[(row['session_date_kst'], row['symbol'])].append(datetime.fromisoformat(row['bucket']).astimezone(timezone.utc))

by_day = defaultdict(lambda: [0, 0, 0])
for (day, _), times in buckets.items():
    times.sort()
    good = len(times) == 77 and times[0].hour == 0 and times[0].minute == 0 and all(b-a == timedelta(minutes=5) for a,b in zip(times,times[1:]))
    by_day[day][0] += int(good)
    by_day[day][1] += int(not good)
    by_day[day][2] += len(times)
for day, (good, bad, count) in sorted(by_day.items()):
    print(f'{day} complete_symbols={good} incomplete_symbols={bad} rows={count}')
