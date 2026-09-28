SET default_transaction_read_only=on;
COPY (
  SELECT symbol, session_date_kst, bucket, open, high, low, close, volume
  FROM research.kr_candles_5m_toss
  WHERE session_segment = 'KRX_REGULAR' AND is_padding = false
    AND session_date_kst BETWEEN '2026-09-01' AND '2026-09-23'
    AND symbol IN (
      SELECT symbol FROM public.kasset_research_cohort_members
      WHERE cohort_id = '67f1059ab7e3a370ab5b9dd89ec3991ad4860d7e4560004674b4f02dda917547'
        AND member_kind = 'active'
    )
  ORDER BY symbol, bucket
) TO STDOUT WITH (FORMAT csv, HEADER true);
