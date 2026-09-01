# KIS circuit breaker — 비활성 역사 구현

KIS cutover 뒤 production wiring은 KIS provider를 생성하거나 호출하지 않는다. 이 circuit-breaker 구현과 설정은 dormant adapter 호환을 위한 역사 정의일 뿐 운영 복구·fallback 절차가 아니다.

KIS 장애 시 Toss로 자동 fallback하거나 circuit breaker를 토글해 KIS를 재활성화하지 않는다. 현재 equity provider는 Toss이며 NH PLUG는 KR mock read-only다.
