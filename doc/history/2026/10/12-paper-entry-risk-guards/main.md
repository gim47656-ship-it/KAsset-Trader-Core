# 2026-10-12 — PAPER 하락장 손실 원인과 진입 위험 관문 4종

## 요청
사용자: "오늘 망했는데 왜 그랬는지 원인분석" → 분석 결과 보고 뒤 수정 1·2·3 모두 진행 승인, 계좌 설정은 그대로 둠.

## 관측(운영 DB 읽기 전용, 2026-10-12 20:50 KST)
- 실계좌 레저(KIS·Toss 등) 10/5 이후 신규 행 0. 손실은 PAPER(`qullamaggie_breakout_portfolio`, AUTO_PAPER)뿐.
- `paper_daily_snapshots`: 계좌 3 24,131,228 → 23,399,919(−3.03%), 계좌 4 10,359,281 → 9,959,137(−3.86%).
- 시장: KODEX200(069500) −3.8%, KODEX코스닥150(229200) −5.1%, KRX 일봉 3,935종목 중 72.1% 하락(중앙값 −1.72%). 10/7·10/8도 61.8%·63.9% 하락.
- 오늘 실현손실은 10/7~10/8 매수분을 한글날 연휴 동안 보유한 뒤 손절한 것: 계좌 3 009155 −549,586, 009150 −491,804, 051915 −318,645 / 계좌 4 009155 −148,387, 051915 −111,351, 307950 −92,083.

## 원인
1. **사이징 손절가 ≠ 관리 손절선**: 009155 BUY는 `positionSizing` entry 527,000·strategyStop 515,000·riskPerUnit 12,000·riskBudget 150,000으로 11주를 정했다. 그러나 MARKET 체결가는 7분 뒤 542,500이었고, 포지션 손절선은 `체결가 − 2×ATR` = 496,286(`position_manager.py:288`)이었다. 손실은 예산의 3.7배였고, 009150은 1,582,000 기준을 1,666,000에 체결해 3.3배였다.
2. **국면 판정 순서**: `regime.py`가 VOLATILE(ATR≥0.035)을 BEAR보다 먼저 판정했다. 10/7~10/8 지표 breadth 0.37·return20 −0.048은 BEAR 조건이지만 ATR 0.042라 VOLATILE(×0.5)로 판정됐고, BEAR(×0, 신규 매수 차단)이 적용되지 않았다. 같은 기간 BUY 57건(6,090만 원)이 전부 VOLATILE에서 나갔다. AI shadow가 BUY에 동의한 것은 4건(118만 원)뿐이었다(비게이팅).
3. **반복·쏠림**: 051915는 같은 first-pullback 신호(기준 121,100)가 10분마다 다시 떠 30분 사이 8+20+20주를 샀다(`sameSymbolBuys=2/unlimited`). 같은 날 청산 9분 뒤 재진입도 있었다. 계좌 4는 009150+009155(같은 발행사)를 자산의 약 46%까지 보유했다.

## 변경(브랜치 `fix/paper-entry-risk-guards`)
- `regime.py`: BEAR → VOLATILE → BULL → SIDEWAYS 순(Main).
- `position_manager.py` `initialize_position(strategy_stop=…)`: 초기 손절 = max(ATR 손절, 유효한 전략 손절가). `position_manager_service._entry_strategy_stop`이 생성 시각 ±30초 안에서 가장 가까운 AI BUY 체결의 추천 evidence에서 전략 손절가를 읽는다(Maker StopChaseGuard).
- `chase_guard.py`(신규)·`consumer.py`·`job.py`·`contracts.py`: AUTO_PAPER BUY 제출 직전 preview 가격으로 `chase_limit_exceeded`(0.5R 초과), `below_strategy_stop`, `chase_guard_unavailable`(근거 없음)을 판정해 BLOCKED 처리하고, 통과하면 위험 예산으로 수량을 줄이기만 한다. 수동 승인 주문은 바뀌지 않는다(Maker StopChaseGuard).
- `policy.py`: Hard Risk `SAME_ISSUER_HELD`·`SAME_ISSUER_DAILY_BUY_LIMIT`(KRX 코드 앞 5자리, 모든 등급, BUY 전용)를 추가했다(Maker IssuerRepeatCap).
- 문서: `docs/kasset/AUTOMATION_BREAKOUT_CONTRACT.md`.

## 검수·검증
- Main이 diff를 검수했다. 체결가 ≤ 전략 손절가 허용 결함(F1)은 재작업해 차단했다.
- cross_review(OpenAI gpt-6-astra) 1회차 major 2건: ATR 손절 0 이하 시 전략 손절가 적용 전 예외(R1-OAI-1), 이전 사이클 BUY 오선택(R1-OAI-2). 수정 후 2회차 지적 0, resolve pass.
- 로컬 테스트 금지(하드룰 14). 테스트는 PR CI에서 실행한다.
- 하네스 한계: `git.exe`로 만든 worktree는 cross_review 경로 확인(Linux `git rev-parse`)이 읽지 못한다. 사용자 선택으로 본 체크아웃을 기능 브랜치로 잠시 전환해 진행했다. autocrlf CRLF 작업본은 리뷰 diff를 부풀리므로 리뷰 대상 8개 파일을 LF로 맞췄다(커밋 blob은 동일).

## 남은 확인
배포 뒤 첫 정규장 관찰 항목은 HANDOFF 「PAPER 진입 위험 관문 4종」.
