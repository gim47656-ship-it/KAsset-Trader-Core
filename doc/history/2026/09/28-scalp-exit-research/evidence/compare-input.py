import csv
from pathlib import Path

root = Path('/work')
verified = Path('/source')
for name, when in [('daily', 'time'), ('intraday', 'session_date_kst')]:
    def rows(path):
        with path.open(newline='', encoding='utf-8') as stream:
            assert stream.readline().strip() == 'SET'
            return [tuple(row.values()) for row in csv.DictReader(stream) if row[when][:10] <= '2026-09-23']
    prior = rows(root / f'{name}.csv')
    current = rows(verified / f'{name}-verified.csv')
    assert prior == current, f'{name} differs: {len(prior)} != {len(current)}'
    print(f'{name}_equal_rows={len(prior)}')
