# HANDOFF — KAsset-Trader-Core
갱신: 2026-09-23 (우선주 종목명 누락·확정손익 부분실현 반영)

## 현재 목표·운영 상태
- 사용자 확정 전략은 **장중 돌파 단기매매**다. 손절 조건을 장중에 평가하고 손절·실현손실 자체가 다음 매수 후보를 막지 않도록 한다. 당일 강제청산은 추가하지 않는다.
- **2026-09-22 사용자 승인 변경(매도 규칙 전권 위임)**: 진입은 단타인데 청산이 스윙 기준이라 2026-09-22 보유 6종목이 장중 최대 +3.81%까지 가고도 SELL 0건이었다. 익절을 계단으로 나눠 장중에 닿게 고쳤다. **초기 손절 `진입가 - 3 ATR`은 유지**한다.
- **현재 청산 사다리 (5단)**: ① 초기 손절 `진입가 - 3 ATR` ② 진전폭 `+1 ATR` 뒤 켜지는 `최고종가 - 2 ATR` 조기 보호선 ③ `+0.5 ATR` 1차 익절 30% ④ 부분익절 뒤 `max(진입가, 최고종가 - 3 ATR)` 바닥 ⑤ TIME_STOP 진전폭을 **현재 종가** 기준으로 판정(과거 최고 종가 latch 제거). 백테스트 근거는 [22-sell-strategy-app-surface](doc/history/2026/09/22-sell-strategy-app-surface/main.md).
- **추천 유효기간 80분**(`_OWNER_BUY_COOLDOWN` 60분 + `_PRODUCER_TICK` 10분 × 2). 이전 60분은 배치 간격 70분을 못 덮어 모든 추천이 다음 배치 전에 만료됐고, 그래서 같은 종목이 매 배치 새로 추천됐다. 같은 종목 재진입 한도(`same_symbol_reentry_limit`)를 추천 생성 단계에도 적용하되 **체결 성공 + 미만료 PENDING만** 세고 예산 소진 실패는 세지 않는다(재시도 경로 보존).
- **2026-09-16 사용자 승인 변경**: 2026-09-07의 -3% 자동 손절 바닥을 **제거**했다. [07-stop-loss-floor](doc/history/2026/09/07-stop-loss-floor/main.md)의 "-3% 바닥 도입" 기록은 역사이며 현재 계약이 아니다. ATR 근거가 없는 보유분은 상태를 만들지 않고 그 tick을 관리하지 않는다.
- **2026-09-23 사용자 승인 변경(확정손익 정의)**: 부분 익절한 종목을 '진행 중 1건'으로 세어 그때까지의 실현손익을 금액·건수·승패에 반영한다. 전량 청산되면 같은 1건이 완료로 바뀌며 중복 카운트가 없다. 한 주도 팔지 않은 보유는 계속 제외한다. 열린 매매의 `quantity`·`cost_basis`는 판 만큼만 잡고 매수 수수료도 그 비중만 부담시킨다 — 전량 청산은 비중이 1이라 **기존 계산과 정확히 일치**한다. 근거는 [23-preferred-name-pnl](doc/history/2026/09/23-preferred-name-pnl/main.md).
- **우선주 종목명**: `symbol_master`의 CHECK가 `COMMON_STOCK`/`ETF`만 받아 우선주는 행이 없다. 이름 해석은 `kr_symbol_universe` 2차 fallback으로 메우며 `symbol_master` 결과가 항상 우선이다. **`symbol_master`의 `security_type` 분류는 스크리너 유니버스 분모 의미를 가지므로 우선주를 적재하는 방향으로 바꾸지 않는다.**
- **2026-09-23 앱 확인 완료(사용자 관측)**: 배포 후 앱에서 우선주 한글명 표시와 부분 익절분의 확정손익 반영이 정상 동작함을 사용자가 확인했다. 두 결함은 닫혔다.
- **다음 행동**: 다음 정규장의 자연 추천·체결·청산을 읽기 전용으로 관찰한다. 특히 부분 익절 뒤 잔량이 4단 바닥(`max(진입가, 최고종가 - 3 ATR)`)에서 어떻게 빠지는지를 본다. 검증 목적으로 주문을 제조하지 않는다.

### 유지 중인 계약·경계 (이관한 기록에서 통합)
- 10분 producer + 5분 execution sweep이며 틱 즉시 손절이 아니다. 장중 조건 발생 후 봉 완료·다음 평가·다음 집행을 기다린다. 장 마감 직전 bucket, provider 지연/실패, 시장 종료 후의 체결은 보장하지 않는다. 장외 강제 주문은 하지 않는다.
- 초기 ATR 손절 폭(`-3 ATR`), 일봉 추세/기간 판정, 목표 수익 EXIT_ONLY, STAGED_REDUCTION의 BUY 수량×0.75, BUY 1시간 중복 방지·일일 주문/동일종목 재진입 횟수 제한은 보존했다. 손실 원인 veto 제거를 이 모든 제한의 제거로 해석하지 않는다. **부분익절선과 trailing 구조는 2026-09-22에 위 5단 사다리로 바뀌었다** — 이 줄의 "기존 trailing"은 그 이전 계약을 가리킨다.
- 기존 일봉 백테스트 수익률은 새 장중 집행 전략의 성과 검증이 아니다. 실시간 체결·장마감 경계·운영 rollout은 별도 승인/관찰 대상이다.
- maxDailyLossRatePct/maxDailyLossAmount는 앱 wire에 남으며 참고값이다. 앱이 이를 강제 매수중단/종목손절로 표현하지 않는지 소비자 문구를 별도로 검토해야 한다.
- 배포 승인 후 CI·promotion fingerprint·운영 이미지 정합과 자연 SELL→후속 BUY 후보 흐름을 관찰한다. **진입 임계값**(상대거래량 1.5배·no-chase 2%·돌파 버퍼 0.2%)은 `same_time_rvol_shadow` 채점 없이 변경하지 않는다. 청산 임계값은 2026-09-22에 사용자 승인으로 변경됐다.
- **미국장 재개 결정(사용자 몫)**: 미국장 재개(`KR_ONLY`→`KR_US`)는 앱 설정만으로 가능하다. 상대거래량 1.5배·no-chase 2%·돌파 버퍼 0.2%는 `intraday_triggers.py` 코드 상수이며, 변경은 `same_time_rvol_shadow` 채점 뒤에만 한다(AGENTS.md 12).
- **롤백 금지·주의(유효)**: 운영에 nullable 컬럼(`initial_atr`, `exit_levels_effective_at`, `exit_level_history`)이 적용됐다. 문제가 생기면 구버전 이미지로 되돌리지 않는다. 새 컬럼 downgrade는 temporal provenance를 삭제하고, 새 행/history가 생긴 뒤 이 revision을 모르는 구버전 image로 rollback하면 같은 과거 소급 결함을 재도입하므로 **구버전 rollback과 Alembic downgrade를 금지**하고 roll-forward한다. `initial_atr IS NULL` 행이 0건일 때만 `alembic downgrade` 후 이전 이미지가 가능하고, 1건 이상이면 nullable을 이해하는 버전으로 roll-forward한다(migration downgrade 자체도 NULL 행이 있으면 `RuntimeError`로 거부한다). 실패 시 worker·scheduler 정지를 유지하며, 자동 rollback 경로도 쓰지 않는다. 백업은 `backups/pre-stoploss-9fefab61/database.dump`다.

## 완료 이력 (doc/history/2026/09/)
- [2026-09-23 — 우선주 종목명 누락과 확정손익 부분실현 반영](doc/history/2026/09/23-preferred-name-pnl/main.md)
- [2026-09-22 — 매도 미발생·매수 과다·앱 표면 오류 조사와 수정](doc/history/2026/09/22-sell-strategy-app-surface/main.md)
- [2026-09-19 — KR Breakout / First Pullback / NR7 비교 및 PAPER 런타임 연결](doc/history/2026/09/19-kr-entry-paths/main.md)
- [2026-09-16 — 매수 부재 원인 조사와 -3% 손절 바닥 제거](doc/history/2026/09/16-stop-floor-removal/main.md)
- [2026-09-14 — 자동매매 사유·점수 조회 응답 스키마 복구](doc/history/2026/09/14-recommendation-schema/main.md)
- [2026-09-08 — PAPER stop 시간 소급 방지·US 시세 가용성 확인](doc/history/2026/09/08-temporal-stop-levels/main.md)
- [2026-09-08 — PAPER/Toss 외 broker 표면 제거](doc/history/2026/09/08-broker-surface-cleanup/main.md)
- [2026-09-07 — PAPER 자동 손절 -3% 바닥](doc/history/2026/09/07-stop-loss-floor/main.md)
- [2026-09-07 — 장중 보호 청산과 손절 이후 후보 검토](doc/history/2026/09/07-intraday-exit/main.md)

## 운영 배포 방식 (2026-09-05부터 자동, PR #56 `fde4d4e2`)
- **main merge → Test 워크플로 성공 → `.github/workflows/deploy-kasset.yml`이 운영서버 self-hosted runner(`kasset-prod`, systemd `actions.runner.gim47656-ship-it-KAsset-Trader-Core.kasset-prod`, 사용자 `ghrunner`, docker 그룹)에서 `deploy/kasset/deploy.sh <sha>`를 실행한다.** 승인 단계 없음 — merge가 승인이다. SSH 포트는 열지 않는다.
- `deploy.sh`는 기존 수동 절차와 동일: `git checkout <sha>` → `.env.kasset`의 `CORE_IMAGE_TAG/VCS_REF` 갱신(`.env.kasset.pre-<sha8>` 백업) → `compose build api` → `up -d api worker scheduler mcp ai-mcp` → `https://$KASSET_DOMAIN/health` 200 + 5개 컨테이너 새 이미지 확인(최대 180초) → 실패 시 이전 SHA로 롤백.
- **alembic/versions 변경이 포함되면 자동배포는 exit 2로 멈춘다.** `workflow_dispatch`에서 `allow_migration=true`로 수동 실행하면 `backups/kasset-pre-migration-*.dump.gz` 백업 후 `compose --profile migration run migration`을 돌리고 배포한다. DB는 자동 롤백하지 않는다.
- 롤백/재배포: Actions → Deploy → Run workflow에 `sha` 입력.
- `/opt/kasset-trader-core`는 `ghrunner` 소유로 바꿨다(root가 아닌 runner가 checkout·env 갱신·compose를 실행). 기존 root cron 백업(`deploy/kasset-db-backup.sh`, `/root/backups`)은 영향 없다.
- 저장소가 public이라 fork PR 워크플로는 외부 기여자 전원 승인 필수로 설정했다. 사용자가 fork network 이탈 후 private 전환 예정(Free 플랜에서는 branch protection·environment 승인이 비활성화되지만 위 자동배포 모델은 그것에 의존하지 않는다).
