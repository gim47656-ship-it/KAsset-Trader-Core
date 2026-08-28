"""토스 실시간 스트림 → 앱 WebSocket 팬아웃 모듈.

폴링으로는 증권사앱 체감이 나오지 않는다는 실측(홈 2초 폴링, 주문 호가 1초
폴링 대비 토스 WS는 초당 여러 tick)에 대한 응답이다. 상향(토스 → 서버)은
계정당 연결 상한 때문에 전역 단일 소유자만 열고, 하향(서버 → 앱)은 프로세스마다
자기 클라이언트에게만 팬아웃한다. 기존 REST 시세·호가 경로는 그대로 남아 폴백
역할을 계속한다.
"""

from app.extensions.kasset.api.stream.route import stream_router

__all__ = ["stream_router"]
