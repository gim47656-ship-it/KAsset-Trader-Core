# 장중 청산 arm 비교 — 2026-09-23

## 관측 범위와 방법
- 운영 PostgreSQL은 `BEGIN READ ONLY` 또는 세션 `SET default_transaction_read_only=on`으로 조회했다. 코호트 `67f1059ab7e3a370ab5b9dd89ec3991ad4860d7e4560004674b4f02dda917547`의 active 100종목 일봉 41,570개와 `research.kr_candles_5m_toss`의 `KRX_REGULAR`, `is_padding=false` 124,321봉(2026-09-01~23, 17영업일)을 같은 입력으로 고정했다. 첫날은 부분 적재였고 9월 2일부터는 코호트 100종목에 장중 기록이 있지만, 일부 종목·구간의 bucket 결손은 별도 제외했다.
- 서버의 `/tmp/kasset-exit-ladder-20260923` 전용 체크아웃(원격 GitHub에서 SHA `c1e4371f91c05a83ede36d0f5c3b77472acc2b56` checkout), 프로덕션 앱 이미지의 Python, `--network none --cpus=1.0 --memory=3g`, 임시 오프라인 설정값을 썼다. 운영 checkout·운영 env·운영 DB 볼륨은 사용하지 않았다.
- `run_portfolio_backtest`의 기존 `breakout-baseline` 진입/랭크/수량을 고정한 후, [research_backtest.py](research_backtest.py)에서 장중 청산만 5분 OHLC 완료 bucket, 다음 bucket 시가, 보수적 수수료/슬리피지로 재생했다. 장중 익절·stop은 `evaluate_position_intraday`, 일봉 보호선·기간은 `evaluate_position`을 사용한다. 전 포트폴리오 재진입까지 arm별 재최적화한 결과가 아니므로 외삽 금지.
- 일봉 엔진의 진입 사이클 11개 중 **4개는 진입~관측 종료까지 어느 하루라도 77개 연속 5분 bucket이 없어서 제외**했다. 7개 유효 사이클에서 각 arm 동일 진입·동일 가격 입력을 사용했다. 1개 이상 미청산 사이클은 총수익·MDD에는 평가손익 포함, 승률/손익비/기대값은 청산된 사이클에만 포함한다. 총수익과 MDD는 고정 1억 원 초기자본 기준, 기대값은 완료 사이클의 진입자본 대비 순수익률 평균이다. 손익비는 이익 사이클 평균 / 손실 사이클 평균 절댓값이다.

| arm | 총수익 | MDD | 완료 사이클 (미청산) | 승률 | 손익비 | 기대값 |
|---|---:|---:|---:|---:|---:|---:|
| HEAD | +1.088% | 0.969% | 6 (1) | 33.3% | 0.655 | −0.865% |
| b: 2주 이상 최소 1주, 1주 바닥만, 체결 직후 본전 | +1.588% | 0.701% | 6 (1) | 66.7% | 8.865 | +0.694% |
| b+장중 high-water−1 ATR (부분익절 전 2 ATR) | +0.943% | 0.701% | 7 (0) | 71.4% | 30.469 | +2.674% |
| b+high-water−1.5 ATR | +0.712% | 0.701% | 7 (0) | 71.4% | 24.276 | +2.123% |
| b+high-water−2 ATR | +0.474% | 0.701% | 7 (0) | 71.4% | 17.043 | +1.480% |
| b+high-water−2.5/3 ATR | +1.588% | 0.701% | 6 (1) | 66.7% | 8.865 | +0.694% |
| b+잔량 2차 익절(+1 ATR, 30%) | +1.420% | 0.701% | 6 (1) | 66.7% | 11.142 | +0.883% |

결과 원문은 `artifact://133`(종료 코드 0), 실행기는 `research_backtest.py`. 첫 계산에는 평가손익에 원금을 두 번 더하는 오류가 있어 폐기(`artifact://86`); 현 표는 이를 고친 재실행만 사용한다. 이익·손실 양쪽에서 arm 방향이 엇갈리고, 특히 7개 유효 사이클로 k를 선택하면 과적합이다.

## 장기 이력 재조사와 결정 경계
- `public.kr_candles_{5m,15m,30m,1h}` 및 `research.kr_candles_{5m,15m,30m,1h}` 8개 집계 뷰는 모두 **0행**이었다. materialized hypertable도 비었고 원본 `public.kr_candles_1m`/`research.kr_candles_1m`도 각각 0행이었다(운영 read-only 원문은 해당 도구 출력).
- `research.kr_candles_{5m,15m,30m,1h}_toss` 네 뷰는 동일한 원본 `research.kr_candles_1m_toss`를 시간별로 묶는다. 원본 chunk의 보유 창은 **2026-08-27~09-24**이며 코호트 100종목의 가장 이른 실제 정규장 봉은 **09-01 75종목만 부분 적재**, 09-02부터 100종목에 기록이 있다(개별 bucket 결손은 있음). 09-22에는 5m 100종목/7,708봉, 15m 100/2,608봉, 30m 100/1,308봉, 1h 100/700봉이다. 09-01에는 각각 75/1,125, 75/430, 75/235, 75/166봉. 따라서 15m·30m·1h로 넓혀도 3개월 이력은 생기지 않는다.
- **현 데이터로는 3번의 초과성과를 판정할 수 없음**. 3개월 이상 동일 코호트·동일 진입 계약을 덮고, 결측 세션을 제외한 유효 완료 사이클이 **최소 30개**인 장중 자료가 쌓인 뒤 같은 arm을 다시 비교해야 한다. 지금은 b(1·2 버그 수정)만 출고하고 추격·2차 익절 정책은 추가하지 않는다. 이 결론은 k=1이 손익비에서 앞섰다는 것을 반증한 게 아니라 표본 부족과 총수익 방향 불일치로 정책 변경을 보류한 것이다.

## 재생성
- SQL 입력: `kasset_research_cohort_members`에서 위 cohort id와 `member_kind='active'`; `public.kr_candles_1d`의 `venue='KRX'`·2025-01-01~2026-09-23 OHLCV; `research.kr_candles_5m_toss`의 `session_date_kst` 2026-09-01~23, `session_segment='KRX_REGULAR'`, `is_padding=false`. 두 조회 모두 `SET default_transaction_read_only=on; COPY (SELECT … ORDER BY symbol,time/bucket) TO STDOUT WITH (FORMAT csv, HEADER true)`로 같은 날짜·종목 집합에 대해 추출했다. 임시 CSV는 commit 대상이 아니며 작업 종료 시 로컬·서버에서 제거한다.
- 실제 실행: `docker run --rm --name kasset-exit-research --network none --cpus=1.0 --memory=3g --mount type=bind,src=/tmp/kasset-exit-ladder-20260923,dst=/work,readonly -e PYTHONPATH=/work -e PYTHONDONTWRITEBYTECODE=1 -e OPENDART_API_KEY=offline-research -e DATABASE_URL=postgresql+asyncpg://offline:offline@127.0.0.1:5432/offline -e SECRET_KEY=<임의의 오프라인 유효 키> -w /work --entrypoint /app/.venv/bin/python kasset-trader-core:c1e4371f91c05a83ede36d0f5c3b77472acc2b56 research_backtest.py /work` (exit 0).
