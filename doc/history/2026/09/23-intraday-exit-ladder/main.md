# 2026-09-23 — 장중 익절 사다리 결함 조사와 수정

RECORD:
DATE: 2026-09-23
SCOPE: 장중 익절, 부분익절, 본전 바닥, 추격 보호선, position_manager, 소수 보유 수량
PATHS: app/extensions/kasset/automation/position_manager.py, app/extensions/kasset/automation/position_manager_service.py, app/extensions/kasset/automation/portfolio_backtest.py
STATUS: accepted

## 사용자 관측
"오늘 잘 매수는 됬는데 익절부분이 좀 작동이 어려웠던것같음" (2026-09-23 장 마감 후)

## 운영 read-only 관측 (owner 4, 2026-09-23 KST)
- 자동 SELL은 09:15~09:25 부분익절 3건뿐이다: 005935 1주, 005930 6주, 000155 3주. 세 건 모두
  `evaluationHorizon=intraday`, 5분봉, evidence의 `currentStop`은 `진입가 - 3 ATR`.
- 13:15~13:16 SELL 7건은 `LIMIT` + UUID client id로 사용자의 앱 수동 매도다(자동 경로는
  `ai-rec:` 접두 client id와 `MARKET`).
- 보유 중이던 모든 state 행이 `highest_close = entry_price`, `current_stop = initial_stop`,
  `last_evaluated_at = NULL`이었다. 부분익절한 종목도 바닥이 오르지 않았다.
- 005940(3주)은 `research.kr_candles_5m_toss` 기준 09:00~10:25에 고가가 1차 익절선 26,393 위(고점
  26,800, +2.88%)였는데 SELL 추천이 0건이었다.

## 원인 (코드 대조)
1. `_quantity_for_signal`이 `3 × 0.3 = 0.9`를 ROUND_DOWN해 0으로 만들고 `_manage_position`이
   `quantity <= 0`에서 신호를 조용히 버린다.
2. PARTIAL_SELL `SUCCEEDED`는 `partial_exit_completed=True`만 반영하고, 본전 바닥은 완료
   일봉 `evaluate_position`의 `_protective_stop`에서만 적용된다. 장중 잔량은 하루 동안
   `진입가 - 3 ATR`로만 보호된다.
3. `evaluate_position_intraday`는 설계상 상태 전이가 없어서 1차 익절 뒤 장중 추가 익절이나 추격
   보호선이 없다.
4. (범위 밖, 관찰) 일봉 `as_of`가 날짜 label(00:00 UTC)이라 진입 당일 봉이 `row_at <= entry_at`로
   걸러져 추격선 갱신이 하루 더 늦다. 그날 봉의 고가·저가에는 진입 전 가격이 섞여 있어서 따로
   판단해야 한다.
5. (범위 밖, 관찰) 005930 수동 LIMIT 매도 `67b1f6c8…`가 `OPEN`으로 남아 있다. 포지션은 0주다.

## 결정
- 사용자 승인(“그렇게해줘 3번까지”): 1·2번은 버그로 수정하고, 3번은 백테스트로 방식을 고른 뒤
  구현한다. 4·5번은 이번 범위에 넣지 않는다.
- Maker `ExitLadder`(openai-codex/gpt-6-sol:xhigh, Jev HARD_CODE_SYSTEM) 1명에게 맡겼다. 세 항목이
  같은 두 파일의 같은 함수 근처라서 나누지 않았다.
- 브랜치 `fix/kasset-intraday-exit-ladder`(origin/main `c1e4371f9` 기준). merge하면 운영에 자동
  배포되므로 merge 직전에 사용자 확인을 받는다.

## 결과와 수용 (Main)
- **1번 ACCEPTED.** KRX에서 2주 이상 보유하면 부분익절 수량을 `min(보유-1, max(1, 내림값))`으로 잡는다.
  1주 보유는 팔지 않고, 도달 bucket 종료 시각부터 본전 바닥만 올린다. `partial_exit_completed`는
  false로 유지해서 확정손익 정의(HANDOFF)와 충돌하지 않는다.
- **2번 ACCEPTED.** PARTIAL_SELL `SUCCEEDED`를 화해하는 tick에서 `effective_at=now`로
  `entry + post_partial_floor_atr × ATR` 바닥까지 올린다. 기존 손절선이 더 높으면 그대로 두고, 과거
  bucket에는 소급하지 않는다.
- **3번은 판정 불가, 출고하지 않음.** 장중 분봉 원본(`research.kr_candles_1m_toss`)이 2026-08-27부터라
  어떤 해상도로 봐도 한 달이 안 된다. 유효 사이클은 7개였다. 총수익은 b가, 기대값·손익비는 추격선
  k=1이 앞서서 방향도 엇갈렸다. 재검토 조건은 같은 코호트에서 3개월 이상의 장중 이력과, 결측을 뺀
  완료 사이클 30개 이상이다. 근거는 [maker-ExitLadder](maker-ExitLadder.md),
  [arm 비교](evidence/intraday-arm-comparison.md).
- 전략 fingerprint 범위(`strategy_artifact.py` `STRATEGY_CODE_PATHS`)에 `position_manager_service.py`가
  없으므로 이번 수정으로 version·fingerprint·promotion 게이트는 바뀌지 않는다. 3번을 나중에
  `position_manager.py`에 넣으면 fingerprint mismatch로 PAPER 주문이 막히므로 재승격이 필요하다.
- 검증: 서버 격리 runner에서 `tests/extensions/kasset/automation/test_position_manager.py` 67 passed(exit 0),
  scoped Ruff/ty exit 0([server-validation](evidence/server-validation.txt)). 서버 임시 checkout
  `/tmp/kasset-exit-ladder-20260923`은 Main이 삭제했다. 브랜치 upstream이 `origin/main`으로 잡혀 있어서
  마감 전에 해제했다(main 직접 push 방지).

## 운영 배포 (사용자 승인: "지금 먼저 운영에배포해봐")
- PR #72를 merge했다(merge 커밋 `c9c4db89f0b765a29bcdcf5385836366a612d95b`). Test run `35846904926`이 success,
  Deploy run `35847569656`이 success였다.
- 서버에서 직접 확인한 결과: `/opt/kasset-trader-core` HEAD와 `.env.kasset`의 `CORE_IMAGE_TAG`·`VCS_REF`가 모두
  `c9c4db89f…`다. api·worker·scheduler·mcp·ai-mcp 5개 컨테이너가 같은 이미지로 running이다(10:15:15 UTC 기동).
  worker 컨테이너 안의 `position_manager_service.py`에 수정 코드가 들어 있고, `/health`는 200이다. 기동 뒤
  worker·scheduler 로그의 `Traceback|ERROR`는 0건이다.
- 자연 체결로 확인하는 것은 다음 정규장 관찰로 남긴다(HANDOFF 다음 행동).
