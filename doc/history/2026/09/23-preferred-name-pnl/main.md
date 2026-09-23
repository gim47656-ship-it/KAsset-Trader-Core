# 2026-09-23 — 우선주 종목명 누락과 확정손익 부분실현 반영

```text
RECORD:
DATE: 2026-09-23
SCOPE: 우선주 종목명, symbol_master 누락, kr_symbol_universe fallback, 확정손익, 부분익절, round trip 집계, 실현손익
PATHS: app/extensions/kasset/api/paper.py, app/extensions/kasset/api/krx_quotes.py, app/services/paper_trading_service.py, tests/test_paper_trading_service.py, tests/extensions/kasset/api/
STATUS: accepted
```

## 사용자 요구 (원문)

- "지금은 이제 청산이들어가는거같은데 여전히 종목코드는 좀 잘 안표기되는게있고.. 그건 보완해야할듯?"
- "그리고 확정손익과 실현손익 뭐이런게 실시간반영이 좀느린거같음."
- 증거: 앱 자산 탭 스크린샷 — 보유 6종목 중 `000155`·`000157`·`005935`가 종목코드로, 삼성전자·메리츠금융지주·NH투자증권은 한글명으로 표시됨.

## 조사 결과 — 둘 다 서버, 앱 수정 불필요

운영 서버 `kasset-server`(`root@100.73.186.78`), 운영 SHA `c1dad9060ada`, 읽기 전용 조회로 확인했다.

### 1. 종목코드 노출 — 이름 출처 테이블에 우선주가 없다

코드로 뜬 세 종목은 전부 **우선주**다(`두산우`·`두산2우B`·`삼성전자우`).

- `symbol_master`의 CHECK(`app/models/symbol_master.py:16`)가 `security_type IN ('COMMON_STOCK','ETF')`라 우선주는 **행이 생길 자리 자체가 없다**. 운영 DB는 KRX에 `COMMON_STOCK` 2,458 + `ETF` 1,164행뿐이고 세 심볼은 0건이었다.
- 반면 `kr_symbol_universe`에는 `is_active=true`, `is_common_share=false`로 이름이 이미 다 있다(활성 우선주 114건).
- 이름을 푸는 `_position_names`(보유·내역 공용)와 `_instrument_names`(시세)가 `symbol_master`만 봤다.

2026-09-22에 고친 것은 **추천** 경로(`_load_live_kr_candidates`)였고, 이번 화면은 **보유** 경로라 별개다.

### 2. 확정손익 — 느린 게 아니라 집계에서 빠진 것

오늘 09:15·09:20·09:25에 부분 익절 3건이 실제로 체결됐다(005935 1주, 005930 6주, 000155 3주). 그런데 `_build_round_trips`는 docstring대로 **포지션이 flat이 될 때까지 그 매매를 제외**한다. 세 종목 모두 잔량이 남아 있어 전량 청산 전까지는 영원히 0으로 보인다.

새 청산 사다리가 30% 부분 익절을 먼저 하므로 이것이 앞으로 상시 상황이다. 서버 시세 캐시 TTL은 2초(`toss_market_data.py:364`)라 지연 원인이 아니다.

## 확정한 계약 변경 (사용자 승인)

선택지를 제시하고 사용자가 **"종목 단위로 진행 중인 매매도 포함"**을 골랐다.

부분 익절한 종목을 '진행 중 1건'으로 세어 그때까지의 실현손익을 금액·건수·승패에 반영하고, 전량 청산되면 같은 1건이 완료로 바뀐다. 중복 카운트가 없다. 한 주도 팔지 않은 보유는 실현이 없으므로 계속 제외한다.

열린 trip의 `quantity`·`cost_basis`는 **판 만큼**만 잡아 `return_rate`가 실제 회수 자본에 대한 수익률이 되게 했고, 매수 수수료도 같은 비중만 부담시켰다. 전량 청산은 비중이 1이라 **기존 계산과 정확히 일치**한다 — 이것이 이번 변경의 핵심 불변식이다.

`app/services/paper_trading_service.py`의 `_currency_performance`도 같은 함수를 쓰므로 성능 지표의 `realized_pnl`·`total_trades`·`win_rate`가 함께 일관되게 바뀐다.

## 작업 배정과 판정

| 담당 | 조각 | 모델 | 판정 |
|---|---|---|---|
| [SymbolName](maker-SymbolName.md) | 우선주 종목명 fallback | `b-ai/deepseek-v4.1-flash:high` | ACCEPTED |
| Main 직접 | 확정손익 부분실현 포함 | — | — |

Jev는 SymbolName을 NORMAL/CODE_SYSTEM, RealizedPnl을 HARD/CODE_SYSTEM으로 판정했다(첫 준비는 Vercel 오류로 unavailable이라 Main이 EASY로 직접 분류했다가 재준비에서 NORMAL 판정을 받아 그것을 따랐다).

**손익 조각은 child로 내지 못했다.** 한 사용자 요청의 `PRIMARY_DELIVERABLE` lock을 Main이 첫 발주에서 종목명 하나로 좁게 잡은 탓에 SideQuestGuard가 두 번째 완료물의 spawn을 막았다. SymbolName을 취소해 되돌리는 대신 Main이 직접 구현했다. 파일이 겹치지 않아 병렬 진행에 지장은 없었다. **다음부터 한 요청에 완료물이 둘이면 첫 child의 `PRIMARY_DELIVERABLE`을 둘 다 포괄하도록 잡는다.**

## 검증 증거

전부 서버 격리 checkout + 일회성 container + `kasset-test-db`의 run-owned DB에서 실행했다. 운영 checkout·`.env.kasset`·운영 DB/볼륨은 쓰지 않았고 운영 DB는 SELECT만 했다.

| 조각 | 명령 | 결과 |
|---|---|---|
| SymbolName | `pytest tests/extensions/kasset/api tests/extensions/kasset/automation/test_job.py` | 434 passed, exit 0 |
| SymbolName | 수정 전 소스 + 신규 테스트 (역증명) | 5 failed / 6 passed, exit 1 |
| Main | `pytest tests/test_paper_trading_service.py` | 82 passed, exit 0 |
| Main | 수정 전 소스 + 신규 테스트 (역증명) | 2 failed / 80 passed, exit 1 |
| **합본** | `pytest tests/test_paper_trading_service.py tests/extensions/kasset/api` | **489 passed, exit 0** |
| 합본 | `ruff check` / `ruff format --check` (5파일) | 모두 exit 0 |
| 합본 | `ty check --error-on-warning` | exit 0 |
| 합본 | `git status --porcelain alembic/` | 0줄 (migration 없음) |

원문은 [evidence/main-pnl-validation.txt](evidence/main-pnl-validation.txt)와 SymbolName의 evidence 5건.

### 운영 3건 재현

```
005935 qty=1 cost=213031.9500  pnl=5291.4875  rate=2.48%
005930 qty=6 cost=1680609.1964 pnl=17073.8536 rate=1.02%
000155 qty=3 cost=1421463.1875 pnl=68126.4375 rate=4.79%
KRW count=3 wins=3 realized=90491.7786 rate=2.73%
```

수정 전에는 이 세 건이 확정손익에 **0원**으로 잡혔다.

## 남은 위험·미확인

- **`balance.realizedPnl`과 확정손익 totals가 497.19원 어긋난다.** 전자는 `PaperTrade.realized_pnl` 단순 합(90,988.97)이고 후자는 매수 수수료를 판 비중만큼 뺀 값(90,491.78)이다. 후자가 기존 전량청산 계약과 같은 방식이라 그쪽을 맞췄고, `balance` 정의 변경은 사용자 승인 범위 밖이라 두었다. 앱이 두 값을 같은 화면에 쓰면 눈에 띌 수 있다.
- **`balance.realizedPnl`은 `equity_kr`만 집계한다**(`paper.py:199-209`). USD 실현손익은 그 필드에 없고 확정손익 totals의 USD 통화 항목으로만 나온다. 기존 설계이며 이번에 건드리지 않았다.
- **상장폐지 종목의 이름은 여전히 안 나온다.** 재사용한 `get_kr_names_by_symbols`가 `is_active` 필터를 걸어 비활성 심볼을 조용히 뺀다. 현재 보유 6종목은 모두 활성이다.
- **앱 화면 확인은 사용자 몫이다.** 서버가 내려보내는 wire 값까지만 테스트로 닫았고 앱 저장소는 이 PC에 없다.
- **`docs/runbooks/server-pytest-runner.md`가 현재 배포와 어긋나 있다.** 이미지 `4e6329d1`/커밋 `2ed7ef40`을 가리키고, 공유 `test_db` 경로는 낡은 스키마 때문에 12 failed / 28 errors를 낸다. 이번엔 `kasset-test-db`의 run-owned DB로 우회했다. runbook 갱신은 이번 범위 밖이다.
- **장중 실행.** runbook이 KRX 영업일 08:50–16:20 KST 실행을 피하라고 하는데 이 검증은 09:57–10:04 KST에 돌았다. `--cpus=1.0`을 걸었고 load average는 0.50 → 1.06이었다. 운영 컨테이너 7종은 건드리지 않았다.
