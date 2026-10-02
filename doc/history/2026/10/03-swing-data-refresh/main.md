# 스윙 자료 지속 수집 보완

RECORD:
DATE: 2026-10-03
SCOPE: 스윙 데이터, DART 재무, 전종목 수급, 순환 갱신, cron
PATHS: app/jobs/financial_fundamentals_snapshots.py, app/services/financial_fundamentals_snapshots/builder.py, scripts/build_financial_fundamentals_snapshots.py, app/jobs/investor_flow_snapshots.py, app/tasks/investor_flow_snapshot_tasks.py, env.example, README.md, HANDOFF.md
STATUS: partial

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

이 기록 시점에는 소스 검수만 수용했고 운영 이미지·설정·cron은 변경하지 않았다. GitHub `Test` 결과는 해당 PR에서 확인한다. 머지는 자동 `Deploy`를 유발하므로 운영 승인 후에만 진행한다.

승인 후 대상은 현재 운영서버의 재무 wrapper `/usr/local/bin/kasset-dart-daily.sh`, `/opt/kasset-trader-core/.env.kasset`의 수급 두 플래그와 같은 revision의 배포다. 정확한 CLI와 기본 설정은 README 「수동·예약 데이터 작업」이 정본이다. 새 cron을 중복 등록하지 않는다.

수급 최근 20일은 1회 보충하고 날짜별 전체 대상 커버리지를 확인한다. 재무 요청 카운터는 프로세스 단위이므로 일일 cron과 수동 전체 실행을 같은 날 중복시키지 않는다. 초기 실제 자료 확인은 소수 종목·작은 요청 예산으로 제한하고, 전체 재무는 일일 순환을 통해 채운다. 가짜 provider 검증을 실제 OpenDART/Naver 적재 완료로 보고하지 않는다.

스윙 전략 수익성 재검증·PAPER 연결은 수행하지 않았다. 그 판단은 데이터가 충분해진 뒤 별도 근거가 필요하다.
