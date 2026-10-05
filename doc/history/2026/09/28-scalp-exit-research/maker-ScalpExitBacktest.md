# 단타 청산 사다리 재설계 전 오프라인 비교 — 2026-09-28
RECORD:
DATE: 2026-09-28
SCOPE: KR PAPER, 단타 청산, 일봉 포트폴리오, 5분봉 동일 진입 비교
PATHS: app/extensions/kasset/automation/position_manager.py, app/extensions/kasset/automation/portfolio_backtest.py, doc/history/2026/09/28-scalp-exit-research/evidence/
STATUS: partial

## 계약과 재현 경계

운영 규칙·코드·설정·주문·DB 자료는 바꾸지 않았다. 로컬 Python도 실행하지 않았다. `9af66f554609110c4aed79a1478c59b9ab1ca152` 전용 `/tmp/kasset-scalp-exit-20260928/repo` checkout과 동일 태그의 앱 이미지를 사용한다. 장중 추적은 운영 규칙이 아니라 [연구 전용 실행기](evidence/research_backtest.py)의 반사실이다. 구성은 기본 `initial_stop_atr=3`, `partial_profit_atr=0.5`, `partial_fraction=0.3`, 부분익절 뒤 본전 바닥, `trailing_stop_atr=3`, 조건부 `TIME_STOP`(기본 10 bars, 현재 종가 진전폭 < 0.5 ATR)을 HEAD로 한다.
실행 중 `docker inspect kasset-scalp-research-v3`는 `network=none`, `memory=3221225472`, `nano_cpus=1000000000`, `/work:false`, 이미지 태그 `kasset-trader-core:9af66f554…`를 반환했다. `/app/tmp:true`는 Docker 이미지의 익명 임시 볼륨(`Type=volume`)이며 운영 checkout·DB 볼륨을 바인드하지 않았다.
[09/22 선행 결과](../22-sell-strategy-app-surface/evidence/exitstrategy-backtest-arms.md)의 채택 arm +12.49%/손익비 1.764 재현을 먼저 검사했다. 이번 초기 진단 실행의 HEAD는 **+12.566483%**, 완료 96·미청산 5, 완료 사이클 손익비 **1.773499**, MDD 7.278506%; 선행 수치와 **일치하지 않는다**([원문 진단 출력](evidence/baseline-diagnostic.jsonl)). 따라서 09/22 입력 동치라는 주장은 하지 않는다. 선행 문서의 100종목×417봉 표현과 달리, 이번 09/22 종료 입력은 100종목 총 41,470봉이며 09/23까지 41,570봉이다([원본 조회](evidence/input-audit.sql)). [INFERENCE] 집계 종료 시각·데이터 스냅샷·사이클 분모 정의 차이가 원인 후보이며, 선행 JSON 원본은 이 작업 폴더에 남아 있지 않아 특정 원인으로 단정할 수 없다. 현재 코드에서 `portfolio_backtest._queue_position_exits`가 `starts_at` 없는 일봉을 넘기지만 `evaluate_position`은 이를 요구하므로, 연구 실행기에 한해 일봉 라벨 00:00 UTC 시작/06:30 UTC 종료를 부여했다. 선행 실행기에도 동일 시각 보정이 있었는지 증명되지 않았다.
선행값 대비 이번 HEAD는 총수익 **+0.076483%p**, 손익비 **+0.009499**, 완료 사이클 **−2**다. MDD 7.278506%는 선행 표의 두 자리 반올림값 7.28%와만 일치한다. 이 정도 차이라도 수치 재현 또는 입력 동치로 취급하지 않는다.
09/22 표는 당시 미커밋 반사실 실행기의 수치이고, 이번 실행은 09/23 `c1dad9060`에서 구현된 뒤 09/27 `9af66f554`까지 청산·포트폴리오 소스에 추가 diff가 없는 HEAD다(`git diff --shortstat c1dad9060 9af66f554 -- app/extensions/kasset/automation/{position_manager,portfolio_backtest}.py` 빈 출력). 코드·데이터 입력을 09/22 당시와 완전히 고정한 A/B가 아니므로 이번 표는 **이번 HEAD 내부 비교만** 허용한다.
선행 일봉 원본 `/tmp/kasset-exit-dataset.json` 및 09/22·09/23 임시 checkout은 서버에서 모두 **ABSENT**(`test -e`, exit 0)였다. 그래서 09/22의 각 봉 값·실행기와 현재 스냅샷을 행 단위로 대조할 수 없다.
[09/23 장중 선행 기록](../23-intraday-exit-ladder/evidence/intraday-arm-comparison.md)은 같은 코호트·필터의 09/01~23 5분봉을 124,321개로 적었다. 현재 09/23까지는 124,363개로 **42개 많다**. 어느 종목·시각의 수정/추가인지 과거 원본이 없어 확정할 수 없다. 일봉 기준선 차이를 이 42개 장중 봉 탓으로 돌리지 않는다.

## 방법

- 출처: 코호트 `67f1059ab7e3a370ab5b9dd89ec3991ad4860d7e4560004674b4f02dda917547`의 `active` 100종목; `public.kr_candles_1d` KRX(2025-01-07~2026-09-23), `research.kr_candles_5m_toss` 정규장·non-padding(2026-09-01~23 완료 세션). 원본 서버 CSV는 추가로 진행 중인 09/28 일봉 4행·5분봉 582행을 담았으나, 일봉 신호 창을 09/22 종료로 고정하고 미완성 장중 세션 09/28은 **전부 제외**했다. 09/24~26 추석 휴장·09/27 일요일에는 5분봉이 없다. [입력 감사 SQL](evidence/input-audit.sql), [읽기 전용 일봉 추출](evidence/extract-daily.sql), [읽기 전용 5분봉 추출](evidence/extract-intraday.sql)을 참고한다. CSV는 민감한 원본 데이터라 기록 폴더에 두지 않고 실행 직후 서버 `/tmp`에서 지운다.
- 일봉: `run_portfolio_backtest`의 진입 `breakout-baseline`, 랭킹·수량·보수적 KR 비용(수수료 0.15%, 불리한 슬리피지 0.10%)과 다음 일봉 시가 체결을 그대로 사용한다. arm마다 일봉 포트폴리오 전체를 다시 굴리므로 총수익·MDD·회전율(전체 진입 사이클)이 재진입 차이를 포함한다. 일봉 `+1/+1.5 ATR` 2차 익절은 **1차 익절 완료 이후** 고가 도달 시 현 잔량 30%를 다음 거래일 시가에 매도(1주 미만이면 원 엔진대로 미체결)한다. 같은 일봉의 장중 순서는 모르므로 일봉 추격선은 **오늘 고가 − k ATR를 오늘 마감 후 올려 다음 일봉부터만** 적용하는 근사이며, 동일 봉의 선행 고가·후행 저가를 임의로 연결하지 않는다.
  기본 `CONSERVATIVE_COST_PROFILE["KR"]`은 수수료 0.15%·불리한 슬리피지 0.10%를 양쪽 체결에 적용하지만 `sell_tax_rate=0` 기본값을 쓴다. 과세 종목의 실제 매도세는 이 연구 수익에 반영되지 않는다.
  일봉의 동일 일자 고가·저가가 목표와 손절선 모두를 통과하면 원 평가기의 손절 우선순위를 그대로 따른다. 그러므로 이 표의 2차익절·추격은 분봉 체결 경로를 대신하지 못한다.
  일봉 엔진의 `evaluate_position`은 1차 익절 신호에서 `partial_exit_completed`를 기록해도 본전 보호선을 그 자리에서 올리지 않는다. 09/23 PAPER 런타임은 실제 체결 화해 tick에서 즉시 올리고, 이 장중 재생도 다음 bucket 시가 체결 직후 올린다. 따라서 일봉 arm의 보호선 적용은 실운영·5분봉보다 늦을 수 있다. 일봉·장중 총수익 숫자 자체를 같은 체결 모형의 추정치로 비교하지 않는다.
  소수 보유의 부분매도도 두 엔진이 다르다. 일봉 포트폴리오는 지시 비율을 1주 아래로 내리면 미체결로 두고, 5분봉 재생은 2주 이상에서 최소 1주를 팔고 runner 1주를 남기는 09/23 PAPER 보완을 따른다. 작은 수량의 수익·사유 분포가 다를 수 있다.
  잔량 30%를 고른 이유는 1차 익절이 원보유의 약 30%를 이미 팔았기 때문이다. 2차에서 초기 보유의 30%까지 또 팔면 runner가 약 40%만 남고, 잔량 30%라면 이론상 초기 보유의 약 49%가 남는다(실제 주식 수는 정수주 규칙으로 달라진다). 선행 장중 arm의 `second`도 잔량 30% 기준이다.
- 장중: 일봉 HEAD 진입의 종목·시각·가격·수량을 모든 arm에 고정하고 77개 연속 5분 bucket이 필요한 진입 이후 **원본 관측 창 끝까지** 재생한다. 첫날 09/01 부분 적재와 종목별 77 bucket 결손은 사이클 단위 제외한다. 완료 bucket에서 판정, 다음 bucket 시가에 불리한 슬리피지·수수료 반영; 1차 익절 후 2차는 **그 시점 보유의 30%**(초기 보유의 30%가 아님)를 정수주 내림·최소 1주·잔여 1주 보존으로 매도한다. 1차 익절 이후에 관측한 장중 고점의 `−1/−1.5 ATR` 추격선은 bucket 종료부터 활성, 동일 bucket 저가를 소급해 발동시키지 않는다. 잔여 추격선과 2차 익절을 함께 쓸 수 있다. 하루 5분봉에서 이미 손절/부분익절을 판정한 뒤 06:30 UTC에 일봉 종가의 기간·보호 상태를 갱신한다. 일봉 날짜 라벨(00:00 UTC)을 그대로 장중 뒤에 평가하면 시각이 역전되므로 이 시각 보정이 필수이며, 일봉 고저가를 장중 뒤에 재적용하지 않는다. 장중 MDD·총수익은 1억원 고정 원금 대비 **고정 진입 사이클의 합성 손익**으로 포트폴리오 재진입 수익률이 아니다.
  장중 재생의 ATR은 진입 전 일봉 15개에서 이웃 종가 대비 True Range 14개 산술평균으로 만든다. 같은 고정 진입에 arm별로 `−3/−2 ATR` 초기선을 적용하고, ATR의 원본 봉·진입가격은 모든 arm에서 같다.
  한 5분봉 안에서 손절과 익절선이 모두 지나가도 고저가 순서는 복원할 수 없다. `evaluate_position_intraday`의 시가/저가 손절 우선순위를 따른다.
  실제 PAPER는 10분 producer·5분 execution sweep과 추천 승인·주문 체결 지연을 가진다. 이 재생은 5분봉 다음 시가 체결을 가정하므로 장중 익절·추격의 실제 체결 성과나 06:30 UTC 마감 직전 주문 가능성을 보증하지 않는다.
  장중 고정 진입 재생에는 일봉 엔진의 종목별 랭커 `TREND_BROKEN` 판정을 재수행하지 않는다(`trend_intact=True`); 따라서 장중 HEAD 수익을 일봉 HEAD와 직접 동치 취급하지 않고 **장중 arm끼리만** 비교한다.
- `h2/h3`은 **진입일을 1거래일째**로 셈한다. 현행 엔진 `bars_held`는 진입일 평가를 건너뛰므로, 조건부/무조건 상한 2일→`bars_held>=1`, 3일→`>=2`로 환산한다. `C`는 종가 진전폭 < 0.5 ATR일 때만 TIME_STOP, `F`는 진전과 무관하게 N일째 종가 전량청산 신호(일봉은 다음 거래일 시가 매도)다. `F`의 만기 시 아직 1차/2차 익절 다리가 대기 중이어도 전량청산으로 대체하고, 이미 전량 손절/추세 청산 신호라면 그 사유를 보존한다. 장중도 만기 종료 bucket 다음 가용 bucket의 시가에 체결하므로 **N일째 종가 체결 보장**은 없으며, 이 차이를 실제 정책 결정 때 감안해야 한다. `s2`는 진입 손절만 −2 ATR, `p1/p1.5`는 2차 익절, `t1/t1.5`는 부분익절 후 장중 고점 추격폭이다.
  09/23 종가에 걸린 `F` 만기는 09/24~26 휴장·09/27 일요일 뒤 09/28 첫 장 시가가 와야 집행할 수 있다. 이번 고정 5분봉 입력에서는 09/28이 불완전해 제외됐으므로 그 시점의 미집행 포지션은 **미청산**으로 남는다. `2일 강제 신호`와 `실제 2일 안에 전량 체결`은 다르다.
  기존 본전 바닥과 새 장중 고점 추격선은 모두 같은 `TRAILING_STOP` 계열 신호를 낸다. 사유 분포에서 새 추격선 단독 기여 건수를 분해할 수 없고, arm 간 총수익·청산 빈도 차이만 볼 수 있다.
- 완료 사이클의 순손익률 = 전체 매도 다리 순손익 합계 / (진입가×초기 수량). 승률 = 양의 완료 사이클 비중, 손익비 = 평균 양의 사이클 수익률 / 평균 음의 사이클 수익률 절댓값, 기대값 = 완료 사이클 순수익률 산술평균. 미청산은 총수익·MDD에 평가손익을 포함하지만 승률·손익비·기대값에서 제외한다. `청산 사유`는 완료 사이클 수가 아닌 **매도 다리 건수**다. 장중 회전율은 고정 진입이라 arm마다 같고, 일봉 회전율은 청산 뒤 재진입에 따라 달라진다.

손익비에서 이익 또는 손실 사이클 한쪽이 0건이면 비율을 정의하지 않고 `—`로 표시한다. 5분봉 표의 고정 회전율은 청산 속도가 달라도 진입을 다시 생성하지 않는 계산 방식의 산물이다.

추천은 단일 arm의 최고 총수익을 따라 뽑지 않는다. HEAD 대비 **동일 방향의 총수익 변화가 일봉·5분봉 양쪽에서 보이는 조합**만 연구 후보로 비교하고, MDD·손익비·완료 수와 미청산 편향을 같이 적는다. 5분봉 표본이 기존 최소 30 완료 사이클 기준에 못 미치면 정책 채택 근거로 보지 않는다.

유효 진입이 5건이라 완료가 모두 5건이어도 승패 한 건에 승률이 **20%p** 움직인다(미청산이 있으면 분모가 줄어 더 크게 흔들린다). 기존 [장중 연구](../23-intraday-exit-ladder/evidence/intraday-arm-comparison.md)의 재판정 조건인 **3개월 이상·완료 30사이클 이상**에도 못 미친다. arm 순위는 정책 승격 근거가 아니다.

일봉도 26개 arm을 같은 417거래일 한 구간에 대입한 단일 전구간 비교다. walk-forward나 별도 검증 기간이 없으므로 여러 조합 중 수익 최댓값을 채택하면 선택 편향이 생긴다. 아래 후보를 제시하더라도 **재검증 우선순위**이지 운영 수치 확정이 아니다.

## 입력·환경의 초기 확인

날짜 중복 제거 계수는 일봉 CSV의 419 거래일 + `SET`/헤더 2종 = 421이다. 09/28·09/23을 뺀 09/22 종료 구간은 **417 거래일**, 실제 OHLC는 **41,470행**이다. `100종목 × 417일 = 41,700행`보다 **230행 적어** 전 종목 각 417봉이라는 선행 문서의 표현을 그대로 재사용할 수 없다. 5분봉은 09/01~23의 17 거래일 + 진행 중 09/28 하루(및 `SET`/헤더)다.

서버의 `SET default_transaction_read_only=on` 세션 `input-audit.sql` 결과: `public` 테이블 **102**, 코호트 active **100**, 09/22까지 일봉 **41,470**, 09/23까지 **41,570**, 09/23까지 완료 장중 원본 **124,363**. 실험 시작 시 `docker ps --filter name=kasset-trader-`는 api·worker·scheduler·mcp·ai-mcp·caddy·db·redis 8개로 모두 Up(해당 상태의 상세는 최종 전후 비교에 기록). 초기 서버 CSV는 `SET`+헤더 포함 일봉 41,576행(09/28 진행 중 4), 장중 124,947행(09/28 진행 중 582)이었고, 09/23까지의 행수는 감사 결과와 일치한다. 진행 중인 09/28을 통째로 누락 종목 처리해 표본을 잘못 0개로 만들지 않도록 제외했다.

09/01 장중 자료는 75종목·1,125봉(평균 15봉)으로 09:00 KST부터 77 bucket을 덮지 못한다. [세션 품질 검사](evidence/coverage.py)의 [원시 출력](evidence/coverage.txt)(exit 0)상 09/02~23은 대부분 하루 100종목 중 **92종목만 정확히 77개 연속 봉**이며 8종목은 여분/불연속 봉이다. 09/09·11·16·17은 91종목만 완전하다. 보유 창 전체에 77개 연속 bucket을 요구해 표본을 선택한다. 진행 중인 09/28은 42종목·582봉이고 완전한 종목이 0개라 통째로 제외했다. 누락 이력을 보간하지 않았다.
HEAD 전체 101진입 중 [표본 정밀 계수](evidence/eligibility-audit.py)의 [원시 결과](evidence/eligibility.json)(격리 실행 exit 0)로 **93건은 첫 완성 세션 09/02 이전 진입**, **3건은 진입 후 보유 창의 77개 연속 봉 결손/초과**, **5건만 유효**함을 확인했다. 관측 창 안에서 시작한 8건 중 5건(**62.5%**)이다. 원래 없었던 09/01·이전 진입 경로를 09/02부터 새 포지션처럼 재생하지 않도록 v3에서 명시적으로 제외한다. 장중 arm의 진입·가격은 이 5건에서만 동일하다.
유효 5건의 진입은 **09/02 두 건, 09/04·09/09·09/10 각 한 건**으로 9일 안에 몰려 있다. 관측은 09/23까지 이어졌어도 서로 다른 시장 국면의 독립 표본 5개가 아니므로 장중 지표의 외삽을 금지한다.

[09/02 초과 봉 검사](evidence/extra-buckets.py)의 [출력](evidence/extra-buckets.txt)(exit 0)에서 불완전한 8종목은 모두 00:00~06:20 UTC의 77개 정규 bucket에 **06:30 UTC bucket 1개가 추가된 78개**였다. 06:20→06:30 간격이 10분이라 선행 연구의 연속성 검사도 통과하지 않는다. `77` 조건은 누락뿐 아니라 장 마감 추가 봉도 제외한다. 날짜별·종목별 선택 편향이 있다.

격리 이미지 `--network none`에서 [행 단위 검사](evidence/compare-input.py)로 원본 CSV와 재추출 CSV를 09/23까지 대조한 결과 `daily_equal_rows=41570`, `intraday_equal_rows=124363`(**exit 0**). 즉 **이번 09/23 이하 고정 입력 자체는 현재 DB 재추출과 정확히 일치**한다. 이는 09/22 선행 결과의 과거 입력 스냅샷 동치까지 입증하지는 않는다. 서버 `/tmp/.../compare-input.py`도 최종 정리 대상이다.
고정 CSV SHA-256은 일봉 `912ee58421d1855ec4e0340ed6621b33a1b5e65ba8b9032178b8c9336ea907f7`, 장중 `e6c15d906f6b4cea74f5ed49830c743734faae57348d5ae588355178fb51334f`이고, 최종 연구 실행기 서버 SHA-256은 `ea1947388ef9ff16f1dfc98ee7c34caf27d93485e859ffcd17ab56068a67cdb2`다. 원본 CSV는 비공개 데이터이므로 커밋하지 않는다.

## 실행 명령과 원시 산출물

서버 `/tmp/kasset-scalp-exit-20260928`에서 `scp evidence/{input-audit,extract-daily,extract-intraday}.sql`로 전달한 SQL을 `docker exec -i kasset-trader-db-1 sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < /tmp/kasset-scalp-exit-20260928/<SQL명>`으로 실행했다. 감사 명령에는 `-F '|' -At`을 추가했다. 세 파일은 모두 첫 줄 `SET default_transaction_read_only=on;`, 추출은 `COPY (...) TO STDOUT WITH (FORMAT csv, HEADER true)`이며 각각 **exit 0**. [감사 출력 원문](evidence/input-audit.txt)과 SQL에 쿼리·행수가 남는다. 증명용 재추출은 일봉 41,570행, 5분봉 124,363행(각 출력의 `SET`+헤더 별도)이다.

구문 확인: `docker run --rm --name kasset-scalp-parse-v3 --network none --cpus=1.0 --memory=3g --mount type=bind,src=/tmp/kasset-scalp-exit-20260928/repo,dst=/work,readonly -w /work --entrypoint /app/.venv/bin/python kasset-trader-core:9af66f554609110c4aed79a1478c59b9ab1ca152 -c 'import ast; ast.parse(open("research_backtest.py").read()); print("syntax_ok")'` → `syntax_ok`, **exit 0**. 연구 실행기는 Windows 로컬에서 실행하지 않았다.
실제 코호트 봉 09/21~23 중 77 bucket이 모두 완전한 첫 종목을 고른 일회성 장중 smoke(`docker run --rm --name kasset-scalp-replay-smoke --network none --cpus=1.0 --memory=3g ... /source/smoke-replay.py`, 서버 cwd `/work`, **exit 0**)에서는 HEAD 미청산, `s2-h2F-p1-t1`은 09/23 00:00 UTC 다음 시가에 `TIME_LIMIT` **10주 전량**을 체결했다. 이는 보유 2일 만기 전량 경로의 단건 증거이고, 부분익절 대기와 만기가 충돌한 사례를 검증한 것은 아니다. 실제 포트폴리오 진입 성과도 증명하지 않는다. `/tmp` smoke 스크립트는 최종 정리 대상이다.
중단한 연구 초안 세 실행은 각각 진행 중 09/28을 완전 세션으로 요구해 표본이 사라지는 문제, `F` 만기에 미체결 부분익절이 전량 청산을 가리는 문제, 완료된 5분봉 첫날(09/02)보다 이른 역사적 진입을 09/02부터 새로 보유한 것처럼 재생할 위험을 드러냈다. 세 컨테이너를 정지해 **exit 137**로 끝냈고, stdout에는 HEAD 진단만 남았다. 아래 표는 그 수치를 섞지 않고 수정판 `v3`의 최종 결과만 쓴다.
[만기와 부분익절 충돌 재현](evidence/smoke-deadline.py)의 [원문](evidence/deadline-smoke.txt)(격리 컨테이너 exit 0)은 09/22 마지막 5분봉의 +0.5 ATR 도달 신호를 다음 날 시가까지 대기시켰다. 동일한 가상 10주 진입에서 조건부 `h2C`는 `PARTIAL_SELL` **3주**만 매도했고, 무조건 `h2F`는 09/23 시가에 `TIME_LIMIT` **10주**를 매도했다. 이는 실제 체결 기록이 아닌 연구 실행기의 만기 우선순위 회귀 사례다.
[2차 익절·추격 공존 재현](evidence/smoke-second-trail.py)의 [원문](evidence/second-trail-smoke.txt)(격리 컨테이너 exit 0)은 가상 100주 진입의 5분봉 경로에서 1차 **30주**, 그때 잔량 70주의 30%인 2차 **21주**, 새 고점−1 ATR 보호선에 남은 **49주 전량** 순서로 기록했다. 이 경로 검사는 임의 OHLC를 쓴 기능 검증이며 실제 표본의 수익·발동 빈도 근거는 아니다.
표본 정밀 계수는 Core checkout에서 `ssh kasset-server 'docker run --rm --name kasset-scalp-eligibility-dates --network none --cpus=1.0 --memory=3g --mount type=bind,src=/tmp/kasset-scalp-exit-20260928/repo,dst=/work,readonly -e PYTHONPATH=/work -e PYTHONDONTWRITEBYTECODE=1 -e OPENDART_API_KEY=offline-research -e DATABASE_URL=postgresql+asyncpg://offline:offline@127.0.0.1:5432/offline -e SECRET_KEY=OfflineResearchOnly1234567890abcdefghijklmno -w /work --entrypoint /app/.venv/bin/python kasset-trader-core:9af66f554609110c4aed79a1478c59b9ab1ca152 eligibility-audit.py > /tmp/kasset-scalp-exit-20260928/eligibility-dates.json'`로 실행해 [원문](evidence/eligibility.json)을 받았다(**exit 0**). 앞선 날짜 열 없는 동일 카운터 실행(`--name kasset-scalp-eligibility-audit`, `/tmp/.../eligibility.json`)도 **exit 0**이었다.

이번 arm 실행의 **실제 명령**(cwd: Core checkout, 공개 기록의 로컬 경로는 일반화했다. `ssh kasset-server` 원격의 작업 입력은 서버 `/tmp`):

```sh
ssh kasset-server 'docker run --rm --name kasset-scalp-research-v3 --network none --cpus=1.0 --memory=3g --mount type=bind,src=/tmp/kasset-scalp-exit-20260928/repo,dst=/work,readonly -e PYTHONPATH=/work -e PYTHONDONTWRITEBYTECODE=1 -e OPENDART_API_KEY=offline-research -e DATABASE_URL=postgresql+asyncpg://offline:offline@127.0.0.1:5432/offline -e SECRET_KEY=OfflineResearchOnly1234567890abcdefghijklmno -w /work --entrypoint /app/.venv/bin/python kasset-trader-core:9af66f554609110c4aed79a1478c59b9ab1ca152 research_backtest.py /work > /tmp/kasset-scalp-exit-20260928/v3.jsonl 2> /tmp/kasset-scalp-exit-20260928/v3.err; code=$?; printf "EXIT=%s\n" "$code"; wc -c /tmp/kasset-scalp-exit-20260928/{v3.jsonl,v3.err}; if test "$code" -ne 0; then cat /tmp/kasset-scalp-exit-20260928/v3.err; fi; exit "$code"'
```

입력 추출도 정확히 `scp doc/history/2026/09/28-scalp-exit-research/evidence/extract-daily.sql doc/history/2026/09/28-scalp-exit-research/evidence/extract-intraday.sql kasset-server:/tmp/kasset-scalp-exit-20260928/` → `ssh kasset-server 'docker exec -i kasset-trader-db-1 sh -c '\\''psql -X -v ON_ERROR_STOP=1 -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\"'\\'' < /tmp/kasset-scalp-exit-20260928/extract-daily.sql > /tmp/kasset-scalp-exit-20260928/daily-verified.csv && docker exec -i kasset-trader-db-1 sh -c '\\''psql -X -v ON_ERROR_STOP=1 -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\"'\\'' < /tmp/kasset-scalp-exit-20260928/extract-intraday.sql > /tmp/kasset-scalp-exit-20260928/intraday-verified.csv'`(**exit 0**)로 실행했다.

## 동일 입력 내 일봉 포트폴리오 arm 비교

표기: `HEAD`는 `s3-h10C-p0-t0`(현행 `max_holding_bars=10`, 진입일은 세지 않음), `s`는 초기 손절 ATR 배수다. 새 `h2/h3`은 진입일 포함 2/3거래일, `C/F`는 조건부/무조건 기간 신호, `p`는 2차 익절의 +ATR, `t`는 부분익절 후 고점 추격폭의 ATR 배수다. `p0/t0`은 새 단을 끈 단일 변경 arm이다.

| arm | 총수익 | MDD | 완료 (미청산) | 승률 | 손익비 | 기대값 | 회전율(진입 사이클) | 청산 사유(매도 다리 건수) |
|---|---:|---:|---:|---:|---:|---:|---:|---|

## 같은 진입의 5분봉 arm 비교

| arm | 총수익 | MDD | 완료 (미청산) | 승률 | 손익비 | 기대값 | 회전율(고정 진입) | 청산 사유(매도 다리 건수) |
|---|---:|---:|---:|---:|---:|---:|---:|---|
