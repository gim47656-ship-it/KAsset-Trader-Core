"""AI provider 전용 MCP sidecar.

`run_skill` 도구 하나만 노출하는 별도 프로세스다. 거래 도구 서버인
`app/mcp_server`(profile `analysis_readonly`)와는 아무 것도 공유하지 않으며,
broker·account·order 도구를 등록하지 않는다.

import 부작용을 두지 않는다. sidecar 실행은 `server` 모듈이 담당하고,
`app.core.config` 전체 Settings를 읽지 않으므로 DB·broker·JWT 비밀이 없는
컨테이너에서도 기동한다.
"""
