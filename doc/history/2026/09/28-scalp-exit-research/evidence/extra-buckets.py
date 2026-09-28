import csv
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from pathlib import Path
buckets=defaultdict(list)
with Path('/work/intraday.csv').open(newline='',encoding='utf-8') as f:
    assert f.readline().strip()=='SET'
    for row in csv.DictReader(f):
        if row['session_date_kst']=='2026-09-02':
            buckets[row['symbol']].append(datetime.fromisoformat(row['bucket']).astimezone(timezone.utc))
for symbol,times in sorted(buckets.items()):
    times.sort()
    if len(times)!=77 or times[0].hour!=0 or any(b-a != timedelta(minutes=5) for a,b in zip(times,times[1:])):
        gaps=[(str(a),str(b)) for a,b in zip(times,times[1:]) if b-a !=timedelta(minutes=5)]
        print(symbol,len(times),times[0],times[-1],gaps)
