# doc — 작업 기록 지도

이 폴더는 **그때 무엇을 왜 그렇게 판단했는가**를 남긴다. 현재 상태는 여기 없다.

## 문서 역할

| 문서 | 담는 것 | 담지 않는 것 |
|---|---|---|
| 루트 `HANDOFF.md` | 현재 상태, 다음 행동, 남은 경계 | 완료 이력, 검증 로그 |
| 루트 `CLAUDE.md` | 아키텍처·표면별 계약 정본 | 현재 상태, 작업 이력 |
| `docs/` | 런북, API 계약, 설계 문서 (기존 폴더) | 작업 판단 기록 |
| `doc/history/YYYY/MM/DD-주제/` | 그 작업의 판단과 실행 기록 | 현재 상태 |

`docs/`와 `doc/`는 다른 폴더다. `docs/`는 운영·설계 문서고, `doc/`는 작업 이력이다.

## 기록 구조

```
doc/history/2026/09/22-sell-strategy-app-surface/
  main.md                    Main의 판단·수용·공유 인계
  maker-<이름>.md            그 Maker의 자기 변경·검증·남은 제한
  evidence/                  긴 diff·로그·백테스트 출력
```

Maker는 자기 파일만 쓴다. `main.md`와 이 지도는 Main이 쓴다.

## 기록 헤더

각 기록 파일 첫 블록에 둔다. 이게 검색 키다.

```text
RECORD:
DATE: YYYY-MM-DD
SCOPE: <주제 키워드를 콤마로>
PATHS: <다루는 소스·설정 경로를 콤마로>
STATUS: accepted | pending-user-device-check | partial | superseded-by <path>
```

`STATUS`는 이 기록을 현재 상태의 근거로 써도 되는지를 가른다. `superseded-by`면 그 경로로 옮겨간다.

## 찾는 순서

색인기는 없다. 아래 순서를 그대로 따른다.

1. 루트 `HANDOFF.md`에서 현재 상태와 경계를 본다
2. 이 파일의 월별 목록에서 후보를 고른다
3. 못 고르면 경로·주제 키워드로 `doc/history/**/*.md`의 `RECORD:` 헤더만 `grep`한 뒤 최신 날짜부터 본다
4. 걸린 기록은 헤더와 필요한 절만 읽는다. 전문을 읽지 않는다

## 월별 목록

### 2026-09

| 날짜 | 주제 | 기록 |
|---|---|---|
| 23 | 우선주 종목명 누락과 확정손익 부분실현 반영 | [23-preferred-name-pnl](history/2026/09/23-preferred-name-pnl/main.md) |
| 22 | 매도 미발생·매수 과다·앱 표면 오류 조사와 수정 | [22-sell-strategy-app-surface](history/2026/09/22-sell-strategy-app-surface/main.md) |
| 19 | KR Breakout / First Pullback / NR7 비교 및 PAPER 런타임 연결 | [19-kr-entry-paths](history/2026/09/19-kr-entry-paths/main.md) |
| 16 | 매수 부재 원인 조사와 -3% 손절 바닥 제거 | [16-stop-floor-removal](history/2026/09/16-stop-floor-removal/main.md) |
| 14 | 자동매매 사유·점수 조회 응답 스키마 복구 | [14-recommendation-schema](history/2026/09/14-recommendation-schema/main.md) |
| 08 | PAPER stop 시간 소급 방지·US 시세 가용성 확인 | [08-temporal-stop-levels](history/2026/09/08-temporal-stop-levels/main.md) |
| 08 | PAPER/Toss 외 broker 표면 제거 | [08-broker-surface-cleanup](history/2026/09/08-broker-surface-cleanup/main.md) |
| 07 | PAPER 자동 손절 -3% 바닥 | [07-stop-loss-floor](history/2026/09/07-stop-loss-floor/main.md) |
| 07 | 장중 보호 청산과 손절 이후 후보 검토 | [07-intraday-exit](history/2026/09/07-intraday-exit/main.md) |

2026-09-07~09-22 완료 이력이 모두 이 폴더에 있다. 루트 `HANDOFF.md`에는 현재 상태·다음 행동·남은 경계와 운영 배포 방식만 남는다. 이 폴더의 기록은 2026-09-07부터 시작한다.
