# KAsset Trader 서버 구조와 VPS 이전 런북

갱신: 2026-08-28. 현재 서버는 Naver Cloud(모두의 AI 실험실) Rocky Linux 8.8,
`root@100.73.186.78`(Tailscale) / 공인 `175.45.201.51`이다. 3개월 뒤 일반 VPS(Ubuntu)로
이전을 전제로 정리한다. 이 문서와 저장소의 `docker-compose.kasset.yml`이 기준이며,
Naver 전용 서비스 종속은 없다.

## 1. 현재 구조

```text
인터넷 ──> Cloudflare Tunnel(api.hsps-portal.xyz) ──> cloudflared ──> api:8000
     └─(구, 폐기 예정) 175-45-201-51.sslip.io ──> caddy:443 ──> api:8000

docker compose (project: kasset-trader, /opt/kasset-trader-core)
├─ db          timescale/timescaledb-ha:pg17  (volume postgres_data)
├─ redis       redis:7-alpine appendonly      (volume redis_data)
├─ api         FastAPI :8000 (127.0.0.1 바인딩) — 자동매매 Engine 포함
├─ worker      taskiq worker  app.tasks.kasset_market_events_tasks
├─ scheduler   taskiq scheduler (캔들 수집 매시 :05, 스캔 매시 :10, KST 평일 9-16시)
├─ mcp         analysis_readonly MCP :8768 (127.0.0.1, 토큰 인증)
├─ cloudflared Cloudflare Tunnel kasset-trader (http2 강제 — UDP 7844 차단 환경)
├─ caddy       (구) sslip.io HTTPS — Android가 터널 도메인으로 전환되면 제거
└─ migration   alembic upgrade head (profile: migration, 수동 1회성)
```

- Risk Manager/주문 검증: `POST /api/v1/orders/preview`와 주문 제출 경로의 Risk Engine은
  api 컨테이너 내부 코드다. 인프라 이전으로 바뀌지 않으며 건드리지 않는다.
- Codex/Claude CLI(구독 AI 브리지)는 api 컨테이너에 바이너리·auth만 마운트되는
  분리 구성이다(아래 상태 인벤토리 참조). 자동매매 서비스 자체는 CLI 없이도
  API 티어(OpenAI/OpenRouter)로 동작한다(fail-through).

## 2. 서버 상태 인벤토리 (이전 시 복사 대상)

| 경로 | 내용 | 비밀 |
|---|---|---|
| `/opt/kasset-trader-core` | 이 저장소 clone + `.env.kasset`(모든 API키/비밀번호) + `.env.nhplug-mock.native` | env 2개만 비밀 |
| `/root/.nhplug/` | NH PLUG 토큰 캐시 | O |
| `/opt/kasset-codex/` | 컨테이너용 Codex CLI auth(ChatGPT 구독) | O |
| `/root/.codex/`, `/root/.codex/env.sh` | 호스트 Codex + MCP 토큰 | O |
| `/usr/local/bin/codex`, `codex-code-mode-host` | Codex 바이너리(재다운로드 가능) | X |
| `/usr/local/sbin/kasset-db-backup.sh`, `kasset-db-restore.sh` | 백업·복원(저장소 `deploy/`에 사본) | X |
| `/etc/cron.d/kasset-db-backup` | 매일 03:30 KST pg_dump, 7일 보존 | X |
| `/root/backups/kasset-daily/` | 일일 덤프 | O |
| `/etc/nftables/kasset-ssh-guard.nft` (+sysconfig include) | SSH 22 공인 차단(tailnet 전용) | X |

DB·Redis 데이터는 named volume(`postgres_data`, `redis_data`)로 영속화돼 있고,
논리 백업은 위 cron이 만든다.

## 3. 외부 종속(이전 시 반드시 갱신)

1. **Toss Open API 허용 IP** — 현재 `175.45.201.51` 등록. 새 VPS 공인 IP로 재등록해야
   캔들 수집이 동작한다.
2. **NH PLUG(모의)** — 발급 시 IP 제한을 걸었다면 동일하게 갱신.
3. **Cloudflare Tunnel** — 커넥터 위치와 무관하게 `api.hsps-portal.xyz`가 따라온다.
   DNS·인증서 작업 0. (`TUNNEL_TOKEN`은 `.env.kasset`에 있음. 토큰 회전 시
   Cloudflare One > 네트워크 > 커넥터 > kasset-trader에서 재발급.)
4. **Tailscale** — 새 VPS에 설치·로그인하면 SSH 경로 유지. 기존 노드는 tailnet에서 제거.
5. **기존 HANSE_ERP 터널은 별개다. 건드리지 않는다.**

## 4. VPS 이전 절차 (다운타임 ≈ 복원 시간 몇 분)

새 VPS(Ubuntu 22.04+) 기준:

```bash
# 1) 기반 설치
apt-get update && apt-get install -y docker.io docker-compose-plugin git
curl -fsSL https://tailscale.com/install.sh | sh && tailscale up

# 2) 코드와 상태 복사 (기존 서버에서)
git clone <repo> /opt/kasset-trader-core
scp old:/opt/kasset-trader-core/.env.kasset /opt/kasset-trader-core/
scp old:/opt/kasset-trader-core/.env.nhplug-mock.native /opt/kasset-trader-core/
scp -r old:/root/.nhplug /root/.nhplug
scp -r old:/opt/kasset-codex /opt/kasset-codex   # 구독 CLI 유지 시
scp -r old:/root/.codex /root/.codex             # 호스트 codex 유지 시
install -m700 deploy/kasset-db-backup.sh /usr/local/sbin/
install -m700 deploy/kasset-db-restore.sh /usr/local/sbin/
# cron: /etc/cron.d/kasset-db-backup (2절 표의 내용 그대로)

# 3) 기존 서버에서 최종 백업 뜨고 서비스 정지
old$ /usr/local/sbin/kasset-db-backup.sh && docker compose ... stop
scp old:/root/backups/kasset-daily/kasset-<최신>.dump.gz /root/

# 4) 새 서버 기동 + 복원
cd /opt/kasset-trader-core
docker compose --env-file .env.kasset -f docker-compose.kasset.yml build api
docker compose --env-file .env.kasset -f docker-compose.kasset.yml up -d db redis
/usr/local/sbin/kasset-db-restore.sh /root/kasset-<최신>.dump.gz
docker compose --env-file .env.kasset -f docker-compose.kasset.yml up -d api worker scheduler mcp cloudflared
# caddy는 Android가 터널 도메인으로 전환된 뒤에는 불필요 — 기동하지 않는다.

# 5) 검증
curl -sS https://api.hsps-portal.xyz/health          # 터널 경유 200
docker compose ... ps                                 # 전 서비스 healthy
docker logs kasset-trader-cloudflared-1 | grep Registered
# Toss 허용 IP 갱신 후: 캔들 수집 태스크 수동 1회 실행 확인
```

주의: 새 환경에서 UDP 7844가 열려 있으면 compose의 cloudflared `--protocol http2`
강제를 제거해 quic로 되돌려도 된다(성능상 이득 미미, 그대로 둬도 무방).

## 5. 복구(같은 서버에서 DB만)

```bash
/usr/local/sbin/kasset-db-restore.sh /root/backups/kasset-daily/kasset-<STAMP>.dump.gz
# RESTORE 입력으로 확인. api/worker/scheduler/mcp 자동 재기동.
```
