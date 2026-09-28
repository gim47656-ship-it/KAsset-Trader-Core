SET default_transaction_read_only=on;
SELECT 'public_tables' AS kind, count(*)::text AS count, NULL::text AS earliest, NULL::text AS latest FROM pg_catalog.pg_tables WHERE schemaname = 'public'
UNION ALL
SELECT 'cohort_active', count(*)::text, NULL, NULL FROM public.kasset_research_cohort_members WHERE cohort_id = '67f1059ab7e3a370ab5b9dd89ec3991ad4860d7e4560004674b4f02dda917547' AND member_kind = 'active'
UNION ALL
SELECT 'daily', count(*)::text, min(time)::text, max(time)::text FROM public.kr_candles_1d WHERE venue = 'KRX' AND time >= '2025-01-01' AND time <= '2026-09-22 23:59:59+00' AND symbol IN (SELECT symbol FROM public.kasset_research_cohort_members WHERE cohort_id = '67f1059ab7e3a370ab5b9dd89ec3991ad4860d7e4560004674b4f02dda917547' AND member_kind = 'active')
UNION ALL
SELECT 'daily_through_0923', count(*)::text, min(time)::text, max(time)::text FROM public.kr_candles_1d WHERE venue = 'KRX' AND time >= '2025-01-01' AND time < '2026-09-24' AND symbol IN (SELECT symbol FROM public.kasset_research_cohort_members WHERE cohort_id = '67f1059ab7e3a370ab5b9dd89ec3991ad4860d7e4560004674b4f02dda917547' AND member_kind = 'active')
UNION ALL
SELECT 'intraday_through_0923', count(*)::text, min(bucket)::text, max(bucket)::text FROM research.kr_candles_5m_toss WHERE session_segment = 'KRX_REGULAR' AND is_padding = false AND session_date_kst BETWEEN '2026-09-01' AND '2026-09-23' AND symbol IN (SELECT symbol FROM public.kasset_research_cohort_members WHERE cohort_id = '67f1059ab7e3a370ab5b9dd89ec3991ad4860d7e4560004674b4f02dda917547' AND member_kind = 'active')
UNION ALL
SELECT 'intraday', count(*)::text, min(bucket)::text, max(bucket)::text FROM research.kr_candles_5m_toss WHERE session_segment = 'KRX_REGULAR' AND is_padding = false AND session_date_kst BETWEEN '2026-09-01' AND '2026-09-28' AND symbol IN (SELECT symbol FROM public.kasset_research_cohort_members WHERE cohort_id = '67f1059ab7e3a370ab5b9dd89ec3991ad4860d7e4560004674b4f02dda917547' AND member_kind = 'active');
