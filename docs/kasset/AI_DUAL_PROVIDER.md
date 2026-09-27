# KAsset AI Provider Routing

갱신: 2026-09-27

## 실행 경로

복잡한 후보·거래 검토는 다음 availability fallback 순서를 사용한다.

```text
API / worker
  → internal ai-mcp sidecar (`run_skill`)
  → direct OpenAI-compatible API
  → OpenRouter
```

뉴스·공시 요약도 같은 MCP → direct API → OpenRouter 순서를 사용한다. 429, timeout,
연결 실패처럼 모델 응답을 얻지 못한 경우만 다음 provider로 넘어간다. malformed
output, schema 위반, refusal, safety 오류는 fail-closed한다. 일반 뉴스는 호출당
최대 5건을 묶고 `KASSET_NEWS_SUMMARY_DAILY_CALL_LIMIT`(기본 100)로 UTC 일일
provider attempt 수를 제한하며, 공시 요약에는 이 상한을 적용하지 않는다.

## 내부 AI MCP sidecar

`app.extensions.kasset.ai_mcp_sidecar.server`는 Streamable HTTP MCP의 `run_skill`
도구 하나만 노출한다. broker/account/order 도구와 DB·Redis 자격이 없다.
`docker-compose.kasset.yml`의 `ai-mcp` 서비스는 `profiles: ["ai-mcp"]`이고
`ports`가 없어 기본 compose network 안에서만 접근된다. 기존
`mcp:8768`은 analysis-readonly 거래 도구 서버이며 AI provider로 사용하지 않는다.

sidecar는 요청마다 운영자가 지정한 구독 CLI 프로세스를 하나 실행한다. 입력 크기,
timeout, 동시 실행 수와 stdout 크기를 제한하고, 반환 JSON을 요청의 JSON Schema로
다시 검증한다. 로그와 오류에는 prompt, context, stdout/stderr, token을 남기지 않는다.

## 활성화

배포는 이 문서의 자동 동작이 아니다. 승인된 배포 창에서만 다음을 수행한다.

1. 서버 `.env.kasset`에 충분히 긴 임의값의 `KASSET_AI_SIDECAR_TOKEN`과
   구독 CLI 명령 `KASSET_AI_SIDECAR_CMD`를 설정한다.
2. 예: `KASSET_AI_SIDECAR_CMD=codex exec --skip-git-repo-check --sandbox read-only -`.
   CLI 바이너리와 `/opt/kasset-codex` 인증은 호스트에서 별도로 준비한다.
3. `docker compose --env-file .env.kasset -f docker-compose.kasset.yml --profile ai-mcp up -d ai-mcp`
   로 sidecar만 기동한다.
4. `/health`와 bearer 없는 `/mcp`의 401을 확인한다.
5. API/worker 환경에 `KASSET_AI_MCP_URL=http://ai-mcp:8770/mcp`,
   `KASSET_AI_MCP_TOKEN=<동일 token>`, `KASSET_AI_MCP_TOOL_NAME=run_skill`을
   설정하고 해당 서비스만 재기동한다.
6. MCP 성공과 MCP unavailable 시 direct/OpenRouter fallback을 각각 확인한다.

`KASSET_AI_MCP_TIMEOUT_SECONDS` 기본 30초는 sidecar timeout 기본 90초보다 짧다.
호출자가 먼저 availability failure로 분류해야 fallback이 지연되지 않는다. 운영 서버는
`KASSET_AI_MCP_TIMEOUT_SECONDS=120`(검증 상한)과 `KASSET_AI_SIDECAR_TIMEOUT_SECONDS=150`을
쓴다. 뉴스 요약 5건 배치는 2026-09-27 기준 평균 67초·최대 86초였다.

## 구독 CLI 인증 만료

sidecar의 `codex exec`는 `/opt/kasset-codex`(컨테이너 `CODEX_HOME=/var/lib/kasset-codex`)의
ChatGPT 로그인을 쓴다. 토큰 갱신이 실패하면 모든 sidecar 호출이
`provider_unavailable: subscription CLI exited non-zero (exit_code=1)`로 실패한다. sidecar는
stderr를 남기지 않으므로 원인은 단건 실행으로 본다. 실패한 호출도 뉴스 요약 일일 한도를 쓴다.

1. 원인 확인: `echo 'Reply with the single word ok.' | docker exec -i kasset-trader-ai-mcp-1 codex exec -m gpt-6-luna --skip-git-repo-check --sandbox read-only -`
   출력에 `Failed to refresh token` 또는 `unauthorized (401)`가 있으면 재로그인이 필요하다.
2. `/opt/kasset-codex/auth.json`을 백업한다.
3. `ssh -tt <server> "docker exec -it kasset-trader-ai-mcp-1 codex login --device-auth"`로 코드를 받고,
   계정 소유자가 `https://auth.openai.com/codex/device`에서 승인한다. ChatGPT 보안 설정에서 기기
   코드 로그인이 꺼져 있으면 거부되므로 켠 뒤 새 코드로 다시 한다.
4. `codex login status`가 `Logged in using ChatGPT`인지, 1번 명령이 `ok`를 내는지 확인한다.
   `review.ai_call_events`의 `kasset_news_summary`가 다음 5분 주기에 success로 돌아오는지 본다.

## 롤백

1. API/worker의 `KASSET_AI_MCP_URL`과 `KASSET_AI_MCP_TOKEN`을 비우고 재기동한다.
   direct API → OpenRouter 경로는 그대로 남는다.
2. `ai-mcp` profile 서비스만 중지한다.
3. 노출 가능성이 있으면 sidecar token을 폐기한다. direct/OpenRouter key와는 별도다.

롤백은 Kill Switch, Hard Risk, PAPER/LIVE 설정, promotion bypass를 변경하지 않는다.

## Jev 판정 (OpenRouter Decisions API)

`KASSET_JEV_API_KEY`에 OpenRouter 키(`sk-or-…`)를 넣으면 코어가
`typesafe/jev-1.13`을 두 곳에서 부른다. 전송은 `app/extensions/kasset/ai/jev_client.py`의
`POST https://openrouter.ai/api/alpha/decisions` 호출 하나이며, 키가 비어 있으면 어떤
호출도 하지 않고 아래 두 동작 모두 기존과 같다. 이 키는 `KASSET_AI_OPENROUTER_API_KEY`와
별도라 요약·검토의 OpenRouter fallback을 켜지 않는다. timeout은
`KASSET_JEV_TIMEOUT_SECONDS`(기본 5초, 최대 30초), 재시도는 없다. 모델 버전은 뉴스 배제
임계값이 흔들리지 않도록 고정했다.

- **뉴스 선별:** 결정론 gate를 통과해 요약 입력이 만들어진 기사마다 yes/no(`noul`)
  `market_relevant`를 묻는다. P(true) < 0.2면 codex 요약에서 빼고 기존 6시간 backoff
  행(`error_type=jev_not_relevant`, `raw_response.jev`에 확률)을 남긴다. 배제
  기사는 codex 일일 호출 상한을 쓰지 않는다. 판정 실패는 그 기사를 요약 대상에 둔다.
- **후보 AI 가산점:** codex 후보 검토 verdict를 얻은 후보마다 choice `stance`
  (`AGREE`/`DISAGREE`/`INSUFFICIENT`)를 묻고, 점수의 AI 가산 항(비중 0.05)을
  P(AGREE)로 바꾼다. 판정 실패·verdict 없음이면 기존 규칙(codex 동의 시 방향 점수)이다.
  `AiReviewStatus`, 후보 채택, 주문·수량·손절·Hard Risk는 바뀌지 않는다. 판정은
  추천 evidence에 `kind=jev_stance` 항목으로 남는다.

호출은 `review.ai_call_events`에 provider `openrouter-jev`(2026-09-27 이전은
`vercel-jev`), feature `kasset_jev_news_relevance`/`kasset_jev_candidate_stance`로
기록되고, 응답의 USD `cost`가 `cost_amount`에 남는다. 롤백은 키를 비우고 api·worker를
재기동하는 것이다.

2026-09-27까지는 Vercel AI Gateway(`typesafe-ai/jev`)를 불렀다. 그날 HTTP 429·503이
분당 수건씩 나와 판정의 약 45%가 fail-open으로 빠졌고, 같은 모델을 제공하는 OpenRouter로
옮겼다. OpenRouter 응답은 choice 확률을 소수 둘째 자리로 반올림하므로 확률 합은 label당
0.005 허용오차로 검사한다.

## 공통 안전 계약

provider 결과는 설명·검토 evidence다. candidate factor, stop, 수량, Hard Risk,
Kill Switch, owner scope, PAPER 승인과 주문 제출은 결정론적 기존 경로가 담당한다.
AI 결과에 broker credential, account, quantity, approval hash, execution mode를
위임하지 않는다.
