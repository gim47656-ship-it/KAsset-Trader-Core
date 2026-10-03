# 스윙 자료 지속 수집 보완

RECORD:
DATE: 2026-10-03
SCOPE: 스윙 데이터, DART 재무, 전종목 수급, 순환 갱신, cron
PATHS: app/jobs/financial_fundamentals_snapshots.py, app/services/financial_fundamentals_snapshots/builder.py, scripts/build_financial_fundamentals_snapshots.py, app/jobs/investor_flow_snapshots.py, app/tasks/investor_flow_snapshot_tasks.py, env.example, README.md, HANDOFF.md
STATUS: accepted

## 요구와 확인한 원인

사용자는 스윙전략 준비를 위해 맡긴 수집을 점검한 뒤, 최신 자료가 계속 쌓이도록 보완하라고 요청했다. 매매전략·주문·브로커 게이트 변경은 범위 밖이다.

운영 `04d62828e06f72ae7a4c314c796529db7e744759`를 읽기 전용으로 확인했다.

- DART cron은 9/27~10/2 매일 재무·공시 단계 exit 0이었다. 하지만 재무 2,271종목·53,745행은 2025Q4까지만 있었고 2026년 행은 0이었다.
- builder의 연도 범위는 전년도부터 시작했고, 최초 백필용 `--skip-existing`은 행이 하나라도 있으면 재방문하지 않았다.
- DART 후보에 들어간 실패 118종목은 ETF였다. 운영 마스터는 `069660`을 ETF로 구분하고 있었지만 기존 필터가 그 필드를 쓰지 않았다.
- 전종목 수급 예약은 비활성이고 16:40 보유·관심·추천 종목 수집만 작동했다. 9/23 약 3,940종목 뒤 수급은 하루 17~24종목뿐이었다.

## 결정과 보존 경계

재무와 수급을 독립적인 Maker 두 명에게 맡겼다. Main은 사용자 요구·편집 전 승인·공통 격리 검증 환경·최종 delta 검수·운영 활성화 경계를 맡았다.

재무는 올해 이미 종료된 분기를 포함한다. 조기 공시를 놓치지 않도록 법정 제출기한을 수집 개시 조건으로 사용하지 않는다. 미종료 기간은 요청하지 않고 미공시 기간의 행을 합성하지 않는다. 과거 5개 완료 연도, 누적 실적의 단독 분기 차감, 공시일·접수번호 provenance와 기존 upsert 경로를 유지한다.

`--refresh-due`를 지속 갱신 모드로 추가하고 기존 `--skip-existing`은 최초 백필 모드로 유지한다. 최신 기간 누락·부분 자료는 7일 뒤, 정정 확인은 30일 뒤 갱신 대상이 된다. 예산 이월이 있으므로 이는 완료 보장 기한이 아니다. 고정 종목 목록의 순환 구간을 먼저 배정해 실패가 다른 종목의 방문을 영구 차단하지 않도록 하고, 남은 예산은 대상 우선순위에 쓴다. 계획의 최악 요청 수로 기존 18,000건 상한 안에서 나눈다.

수급은 기존 TaskIQ 예약과 두 활성화 설정을 재사용한다. 월~토 08:30 KST에 오늘 또는 전일이 KRX 거래일이면 전체 active universe의 최근 20일을 보충한다. `symbolsWithRows`와 `status`가 일부·전체 누락을 드러낸다. 새 자동 재시도·스케줄러·DB 스키마는 추가하지 않았다. 기본 설정 false와 수동 dry-run을 유지한다.

## 검수에서 찾고 수정한 문제

1. 초기 재무 선정은 높은 우선순위의 실패가 일일 제한을 채우면 뒤의 미수집·정정 종목을 영구 차단했다. Main이 실제 선정 함수에 `limit=1`을 주어 14일간 같은 종목만 선택됨을 재현했다. [실패 재현](evidence/Main-fairness-before.txt). 최종본은 고정 pool의 fairness slice로 해결했고 한 종목 실패 및 높은 우선순위 20종목 실패 회귀를 추가했다.
2. 모든 fetch가 실패해도 `committed 0 rows`와 exit 0으로 끝나던 결과를 바로잡았다. 최종 CLI는 전부 실패/빈 응답이면 무저장 exit 4, 예산 소진이면 무저장 exit 3, 갱신 대상 없음은 `nothing due`와 exit 0이다.
3. 수집 5년 창 밖의 partial을 재선정하면 어떤 계획으로도 보충할 수 없었다. DB 집계에서 창 안의 partial만 선택해 오래된 행이 유효한 결측을 가리지 않게 했다.
4. 마스터가 확인한 보통주인데 코드 끝자리 때문에 우선주로 제외되는 경우를 smoke에서 찾아 고쳤다. 마스터 ETF·우선주 구분과 REIT/SPAC 제외는 유지했다.
5. 수급의 원천 자료가 월요일까지 반드시 완성된다는 표현과 cron 상수 문자열을 재고정하는 테스트를 제거했다. 원천의 실제 발행·복구 시점을 보장하지 않는다.

## 실행한 검증

로컬 Python test/lint/build는 실행하지 않았다. 운영과 분리된 서버 checkout `/tmp/kasset-swing-20261003-01a0fead`, `--network container:kasset-test-db`, run-owned DB, readonly 소스 mount와 1 CPU를 사용했다. 운영 env 파일과 live provider credential은 넣지 않았다. `uv.lock`과 달랐던 기존 검증 볼륨 대신 정확한 잠금 버전의 별도 볼륨을 준비했다.

| 범위 | 실제 결과 | 원문 증거 |
|---|---|---|
| 재무 초기 6파일 회귀 | 52 passed, exit 0; 최종에서 미변경 builder/orchestration 범위 재사용 | [r1 pytest](evidence/SwingFundamentals-r1-pytest.txt) |
| 재무 최종 CLI/job/commit guard 3파일 | 40 passed, exit 0 | [r3.1 검사·smoke](evidence/SwingFundamentals-r3.1-checks-smoke.txt) |
| 재무 Ruff/format/ty | 최종 영향 범위 exit 0, 이전 무변경 파일 증거 재사용 | 같은 원문과 [r2 검사](evidence/SwingFundamentals-r2-checks.txt) |
| 재무 실제 CLI + 격리 DB | 가짜 DART로 올해 분기 저장·차감·공시일, 같은 날 재선정, 중복 upsert, 실패 복구, 무대상 실행 확인; 전체 smoke exit 0, 의도한 오류는 3/4 | [최종 smoke](evidence/SwingFundamentals-r3.1-checks-smoke.txt), [상세 저장 결과](evidence/SwingFundamentals-r2-smoke-success.txt) |
| 수급 2파일 회귀 | 14 passed, exit 0; 최종에는 구현 상수 단언 1개만 제거, 나머지 동작 검증 재사용 | [회수한 원본 tool 결과](evidence/Main-Flow-original-tool-results.txt) |
| 수급 Ruff/format/ty | exit 0. 초기 format 1건은 수정 후 재확인 | 같은 원문 |
| 수급 실제 job + 격리 DB | 230종목 중 3개 실패, batch 100/100/30, 227종목·4,540행; dry-run 무쓰기, commit 4,540행, 재실행 insert 0/update 4,540 | 같은 원문 |

검사 명령은 각 Maker 기록과 원문에 있다. 최종 diff는 [재무](evidence/SwingFundamentals-r3.1.diff), [수급](evidence/SwingFlow-r3.diff). 수급 성공 로그의 spill artifact가 없었으나 Main이 세션 JSONL의 실제 `toolResult`만 추출해 원문을 보존했다. 증거 확보를 위해 통과 검사를 다시 실행하지 않았다. 재무 r2 diff 사본은 정리 지시와 보존 요청이 엇갈려 삭제됐으며, 실패 재현·당시 smoke·최종 수정 증거는 남아 있다.

## 운영 반영 경계

초기 소스 검수 뒤 사용자에게 PR #110의 머지·자동 배포, 재무 wrapper 교체, 수급 설정 활성화와 1회 보충 범위를 제시했고 승인을 받았다. PR head `fe36762934fcf8db692c05f097c5169806387456`의 [Test](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/37078025312)가 성공한 뒤 머지했다. 머지 revision `7cbb6c4804d9c0c7322bcfc29f3c3523fe0b39e7`의 [Test](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/37078753029)와 [Deploy](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/37079385612)가 성공했다. Test의 advisory 보안 스캐너는 Bandit 경고를 냈으며, 이를 경고 없음으로 해석하지 않았다. 변경 수집기 4파일의 Bandit finding은 0이었다.

운영 `/usr/local/bin/kasset-dart-daily.sh`의 재무 단계를 `--with-quarterly --refresh-due --all --commit --allow-partial`로 교체했다. 기존 18:30 cron·공시 수집·flock은 유지했다. `.env.kasset`의 `INVESTOR_FLOW_SCHEDULE_ENABLED`, `INVESTOR_FLOW_SNAPSHOTS_COMMIT_ENABLED`를 켜고 같은 배포 revision의 worker·scheduler에 적용했다. 실제 scheduler 프로세스 설정에서 두 값 `True`, 예약 `30 8 * * 1-6`, `cron_offset=Asia/Seoul`을 확인했다. 새 root cron은 추가하지 않았다.

### 운영 실제 실행과 저장 결과

- 실제 OpenDART 호출은 삼성전자 `005930`·SK하이닉스 `000660` 두 종목, 프로세스 요청 예산 200으로 제한했다. 운영 DB에서 각 종목 `2026Q1`, `2026Q2` 총 4행과 단독 분기 순이익 존재를 확인했다. 공시일은 각각 `2026-05-15`, `2026-08-14`, `source_collected_at=2026-10-03 00:03:47.267051+00`, `data_state=fresh`다. 전체 재무 수동 실행은 하지 않았다.
- 실제 Naver 수급 CLI는 `--market kr --all --days 20 --batch-size 100 --commit`으로 실행했다. 원문 결과: `built 78668 investor-flow snapshots for 3944 KR symbols (dry_run=False, batches=40)`, `wouldInsert: 19609`, `wouldUpdate: 59059`, `duplicatePayloadKeys: 0`, `committed 78668 rows.` 종료 코드 0, 소요 439.08초.
- 별도 read-only transaction에서 `market='kr' AND collected_at >= '2026-10-03T00:07:00Z'`를 조회해 **78,668행·3,944종목** 저장을 대조했다. 일자별 종목 수는 9/28 **3,936**, 9/29 **3,938**, 9/30·10/1·10/2 각각 **3,939**다.
- 대상 전부가 최신 거래일 자료를 가진 것은 아니다. `084180`의 최신 수급일은 9/30, `454180`, `464240`, `488200`, `488210`은 9/23이다. 각 종목 20행은 저장됐지만 지연 사유까지 입증한 것은 아니다. 오래된 날짜를 최신 날짜로 바꾸거나 행을 합성하지 않았다.
- 종료 후 api·worker·scheduler 등 운영 애플리케이션 이미지가 `7cbb6c4804d9c0c7322bcfc29f3c3523fe0b39e7`임을 확인했고 `/health`는 `{"status":"ok"}`였다. 이번에 시작한 수집·검증 background job은 모두 종료됐다.

**남은 관측:** 변경 이후 18:30 재무 cron과 다음 유효한 08:30 수급 예약이 실제 자동 발화한 결과는 아직 관측하지 않았다. 예약 등록·설정 적용·실제 provider 수동 실행 성공을 자동 실행 성공으로 바꿔 보고하지 않는다. 재무는 일일 순환으로 전체 최신화를 진행하며, 요청 카운터가 프로세스 단위이므로 같은 날 전체 작업을 중복 실행하지 않는다.

스윙 전략 수익성 재검증·PAPER 연결은 수행하지 않았다. 그 판단은 데이터가 충분해진 뒤 별도 근거가 필요하다.

## 외부 전략 검토의 결론과 비적용 경계

첨부 명세와 외부 소스는 기존 단기 운영을 바꾸는 승인이 아니라 스윙·장기 연구 후보로 검토했다. 현재 공통 청산 설정과 owner promotion bypass가 기간별로 분리돼 있지 않으므로, 새 기간을 추가하기 전에 cycle별 정책 고정·기간별 승격 경계를 별도로 설계해야 한다. 완료 주봉 압축은 패턴 검출 후보이지 완성된 주문 전략이 아니다. 현 재무 upsert는 이전 값과 정정별 관측 이력을 보존하지 않으므로 현재 snapshot만으로 과거 시점 실적 연구를 완성했다고 볼 수 없다.

[Hossa 원본](https://github.com/gatsjy/Project_billionaire_boys_hossa/tree/76857e4e276ed66b496d91d4859105ab17a423d4)은 ETF 포트폴리오의 이평 앙상블·히스테리시스·비대칭 재조정 아이디어만 연구 후보로 받았다. 명시적 LICENSE/COPYING/NOTICE 파일을 찾지 못했으므로 소스 복사는 하지 않았다. 원본 비용 반영·훈련/검증 분리는 확인했지만 다음 거래일 실제 주문 체결·미체결·정수 수량을 재현한 독립 성과 검증은 수행하지 않았다. ETF 거래세 0 가정을 개별주에 적용하지 않는다. 비교는 동일 진입 기록의 매도 차이와 현금·한도·미체결을 포함한 전체 포트폴리오 결과를 구분해야 한다. 새 전략 코드·주문·스케줄은 추가하지 않았다.
