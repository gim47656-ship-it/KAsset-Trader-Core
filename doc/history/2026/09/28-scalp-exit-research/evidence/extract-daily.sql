SET default_transaction_read_only=on;
COPY (
  SELECT symbol, time, open, high, low, close, volume
  FROM public.kr_candles_1d
  WHERE venue = 'KRX' AND time >= '2025-01-01' AND time < '2026-09-24'
    AND symbol IN (
      SELECT symbol FROM public.kasset_research_cohort_members
      WHERE cohort_id = '67f1059ab7e3a370ab5b9dd89ec3991ad4860d7e4560004674b4f02dda917547'
        AND member_kind = 'active'
    )
  ORDER BY symbol, time
) TO STDOUT WITH (FORMAT csv, HEADER true);
