# KAsset Trader 서버 구조와 VPS 이전 런북

갱신: 2026-08-28. 현재 서버는 Naver Cloud(모두의 AI 실험실) Rocky Linux 8.8,
`root@100.73.186.78`(Tailscale) / 공인 `175.45.201.51`이다. 3개월 뒤 일반 VPS(Ubuntu)로
이전을 전제로 정리한다. 이 문서와 저장소의 `docker-compose.kasset.yml`이 기준이며,
Naver 전용 서비스 종속은 없다.

## 1. 현재 구조

```text
인터넷 ──> Cloudflare Tunnel(api.hsps-portal.xyz) ──> cloudflared ──> api:8000

docker compose (project: kasset-trader, /opt/kasset-trader-core)
├─ db          timescale/timescaledb-ha:pg17  (volume postgres_data)
├─ redis       redis:7-alpine appendonly      (volume redis_data)
├─ api         FastAPI :8000 (127.0.0.1 바인딩) — 자동매매 Engine 포함
├─ worker      taskiq worker  app.tasks.kasset_market_events_tasks
├─ scheduler   taskiq scheduler (캔들 수집 매시 :05, 스캔 매시 :10, KST 평일 9-16시)
├─ mcp         analysis_readonly MCP :8768 (127.0.0.1, 토큰 인증)
├─ cloudflared Cloudflare Tunnel kasset-trader (http2 강제 — UDP 7844 차단 환경)
└─ migration   alembic upgrade head (profile: migration, 수동 1회성)

환경변수 항목은 `.env.kasset.example`이 기준이다(키 이름 전수 + 주석).
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

## 6. Cloudflare Tunnel 최초 설정 (새 계정·존에서 다시 만들 때)

1. Cloudflare One > Networks > Tunnels > **Create tunnel** (Remote-managed, 이름 `kasset-trader`).
2. 발급된 커넥터 토큰을 `.env.kasset`의 `TUNNEL_TOKEN`에 넣는다(커밋 금지).
3. Public hostname 추가: `api.hsps-portal.xyz` → Service `http://api:8000`
   (cloudflared가 compose 내부 네트워크에서 서비스명 `api`로 접근).
4. `docker compose ... up -d cloudflared` → 존에 CNAME `api → <tunnel-id>.cfargotunnel.com` 자동 생성.
5. FastAPI 포트는 호스트에 `127.0.0.1` 바인딩뿐이므로 인터넷 직접 노출이 없다.

## 7. 롤백 (이전 실패 시)

구 서버를 지우기 전에는 언제든 5분 안에 되돌릴 수 있다:

```bash
new$ docker compose --env-file .env.kasset -f docker-compose.kasset.yml stop cloudflared
old$ docker compose --env-file .env.kasset -f docker-compose.kasset.yml up -d   # 전 서비스 재기동
curl -sS https://api.hsps-portal.xyz/health   # 구 서버 커넥터로 다시 200
```

- 같은 `TUNNEL_TOKEN`을 쓰는 커넥터가 살아 있는 쪽으로 Cloudflare가 라우팅한다.
  이전 검증이 끝날 때까지 구 서버 compose를 지우지 않는 것이 롤백의 전부다.
- 이전 후 새 서버에서 문제가 발견되면: 새 서버 cloudflared 정지 → 구 서버 up → 원인 수정.
- DB는 이전 시점 덤프가 남아 있으므로(`/root/backups/kasset-daily/` + 이전용 최종 덤프)
  잘못 복원했어도 `kasset-db-restore.sh <덤프>`로 재복원한다.

## 8. 정상 동작 검증 체크리스트

| # | 확인 | 명령/방법 | 기대 |
|---|---|---|---|
| 1 | 터널 경유 API | `curl -sS https://api.hsps-portal.xyz/health` | 200 |
| 2 | 컨테이너 상태 | `docker compose ... ps` | api/db/redis/mcp healthy, worker/scheduler Up |
| 3 | 커넥터 등록 | `docker logs kasset-trader-cloudflared-1 \| grep Registered` | 커넥션 4개 |
| 4 | DB 복원 무결성 | `psql -tAc "SELECT count(*) FROM users; SELECT max(time) FROM kr_candles_1d"` | 이전 전과 동일 |
| 5 | 스케줄 동작 | 다음 정시 +10분에 worker 로그 `market-scan` 실행 | 에러 없음 |
| 6 | 캔들 수집 | Toss 허용 IP 갱신 후 장중 :05 로그 | rows_upserted > 0 |
| 7 | 폰 스모크 | 앱 로그인 → 홈 시세 → 추천 목록 | 정상 표시 |
| 8 | 안전 스위치 | `TRADING_ENABLED=false`, `LIVE_TRADING_ENABLED=false` 유지 | PAPER 전용 |
| 9 | 포트 비노출 | 외부에서 `nc -zv <새IP> 8000 5432 6379` | 전부 실패 |
| 10 | 백업 cron | 다음날 `/root/backups/kasset-daily/` 신규 덤프 | 생성됨 |

## 9. 관리자 경로 네트워크 경계

### 9.1 정책과 설정

Caddy는 `/admin`, `/admin/*`, `/web-auth`, `/web-auth/*`를 일반 catch-all보다 먼저
검사한다. 현재 Google Identity Services callback은 같은 origin의 `POST /web-auth/google`이고,
향후 redirect 방식의 callback이 추가돼도 `/web-auth/*` 아래이면 같은 allowlist에 포함된다.
따라서 로그인 시작과 callback 요청 모두 허용된 네트워크에서 수행해야 한다.
애플리케이션의 기존 admin 세션·role 검사는 이 네트워크 경계 뒤에서 별도로 계속 적용된다.
웹 Google 버튼은 `.env.kasset`의 `WEB_GOOGLE_OAUTH_CLIENT_ID`가 비어 있지 않을 때만
렌더된다. 이 client ID는 브라우저에 노출되는 공개 식별자이며 client secret이 아니다.
미설정 시 버튼은 숨고 `POST /web-auth/google`은 `503`으로 fail-closed한다.

`KASSET_ADMIN_ALLOWED_IPS`는 공백으로 구분한 IP 또는 CIDR 목록이다. 기본값과 샘플값은
`100.64.0.0/10` 하나뿐이며, 환경변수가 없거나 빈 값이어도 Compose와 Caddyfile 양쪽의
기본값 때문에 tailnet 전용으로 동작한다. 사무실 공인 IP는 확인하기 전까지 추가하지 않는다.

```dotenv
KASSET_ADMIN_ALLOWED_IPS=100.64.0.0/10
```

Caddy의 `{$ENV:default}` 치환은 Caddyfile 파싱 전에 일어나며 공백이 든 값을 여러 token으로
확장한다. 따라서 IP 목록을 한 변수로 전달할 수 있다. 근거:
<https://caddyserver.com/docs/caddyfile/concepts#environment-variables>.
`remote_ip`는 HTTP header가 아니라 Caddy에 연결한 immediate peer를 IP/CIDR과 비교한다.
`X-Forwarded-For` 기반 `client_ip`는 이 경계에 사용하지 않는다. 근거:
<https://caddyserver.com/docs/caddyfile/matchers#remote-ip>.

사무실 공인 IPv4가 확정되면 서버의 `.env.kasset`에서 기존 tailnet CIDR 뒤에 `/32`로
추가한다. 아래 명령은 값을 화면에 입력받아 기존 파일을 백업하고 해당 줄만 갱신한다.

```bash
cd /opt/kasset-trader-core
read -r -p "Office public IPv4: " OFFICE_IP
cp -p .env.kasset .env.kasset.before-admin-office
if grep -q '^KASSET_ADMIN_ALLOWED_IPS=' .env.kasset; then
  sed -i "s|^KASSET_ADMIN_ALLOWED_IPS=.*|KASSET_ADMIN_ALLOWED_IPS=100.64.0.0/10 ${OFFICE_IP}/32|" .env.kasset
else
  printf '\nKASSET_ADMIN_ALLOWED_IPS=100.64.0.0/10 %s/32\n' "$OFFICE_IP" >> .env.kasset
fi
unset OFFICE_IP
```

IPv6 사무실 주소를 허용할 때는 단일 주소면 `/128`, 사무실 대역이면 운영자가 확인한 CIDR을
같은 줄에 추가한다. 임의 대역이나 전달 header 값을 넣지 않는다.

### 9.2 Tailnet HTTPS 인증서와 실제 접속 URL

사용자가 집·사무실에서 접속할 기본 URL은 다음이다.

```text
https://vm-naver-kasset.tail624c43.ts.net/admin/ops
```

`175-45-201-51.sslip.io`는 공인 IP로 연결되므로 집에서 접속하면 Caddy의 immediate peer가
집 공인 IP이고 기본 tailnet allowlist를 통과하지 못한다. 반면 MagicDNS host는
`100.73.186.78`로 연결되어 peer가 tailnet IP가 된다. Google Cloud Console의 같은 web
client에서 **Authorized JavaScript origins**에 아래 origin을 등록한다. Google Identity
Services credential POST 방식이므로 redirect URI는 추가하지 않는다.

```text
https://vm-naver-kasset.tail624c43.ts.net
```

`.env.kasset`의 `KASSET_TAILNET_DOMAIN`은 기본적으로 빈 값이다. 비어 있으면 optional site
block 자체가 생성되지 않아 기존 sslip HTTPS가 인증서 파일 없이도 기동한다. Compose는
이 빈 값을 내부 `import kasset_tailnet_disabled` sentinel로 바꿔 Caddyfile의 optional
block을 제거한다. sentinel은 운영자가 직접 설정하는 값이 아니다. 활성화할 때는
반드시 **인증서 발급 → env 설정 → Caddy 검증·재생성** 순서로 진행한다. 정적 인증서 파일이
없는데 domain만 설정하면 `tls` 파일 로드 단계에서 Caddy 검증과 기동이 실패한다.

`*.ts.net` MagicDNS 이름은 일반 공개 A/AAAA가 아니라 tailnet 안에서 해석된다. Caddy의
HTTP-01/TLS-ALPN-01은 공개 DNS lookup과 각각 80/443 도달이 필요하므로 이 이름에 직접
사용하지 않는다. Tailscale의 `tailscale cert`가 tailnet용 DNS-01을 수행해 만든 파일을
Caddy에 read-only로 제공한다.

```bash
install -d -m 700 /opt/kasset-tailnet-certs
tailscale cert \
  --cert-file=/opt/kasset-tailnet-certs/cert.pem \
  --key-file=/opt/kasset-tailnet-certs/key.pem \
  vm-naver-kasset.tail624c43.ts.net
chmod 600 /opt/kasset-tailnet-certs/cert.pem /opt/kasset-tailnet-certs/key.pem

cd /opt/kasset-trader-core
cp -p .env.kasset .env.kasset.before-tailnet-domain
if grep -q '^KASSET_TAILNET_DOMAIN=' .env.kasset; then
  sed -i 's|^KASSET_TAILNET_DOMAIN=.*|KASSET_TAILNET_DOMAIN=vm-naver-kasset.tail624c43.ts.net|' .env.kasset
else
  printf '\nKASSET_TAILNET_DOMAIN=vm-naver-kasset.tail624c43.ts.net\n' >> .env.kasset
fi
docker compose --env-file .env.kasset -f docker-compose.kasset.yml run --rm --no-deps caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose --env-file .env.kasset -f docker-compose.kasset.yml up -d --force-recreate caddy
```

Tailscale 공식 문서상 파일로 받은 인증서는 90일 만료이며 `tailscaled`가 설치 위치를 몰라
자동 갱신하지 않는다. 아래 cron은 매주 최소 유효기간 30일을 요구해 필요할 때 재발급하고,
새 파일로 `caddy validate`가 성공한 경우에만 Caddy를 재시작한다.

```bash
cat >/etc/cron.d/kasset-tailnet-cert <<'EOF'
17 4 * * 1 root /usr/bin/tailscale cert --min-validity=720h --cert-file=/opt/kasset-tailnet-certs/cert.pem --key-file=/opt/kasset-tailnet-certs/key.pem vm-naver-kasset.tail624c43.ts.net && cd /opt/kasset-trader-core && /usr/bin/docker compose --env-file .env.kasset -f docker-compose.kasset.yml run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && /usr/bin/docker compose --env-file .env.kasset -f docker-compose.kasset.yml restart caddy
EOF
chmod 644 /etc/cron.d/kasset-tailnet-cert
```

갱신 상태는 다음 명령으로 확인한다.

```bash
openssl x509 -in /opt/kasset-tailnet-certs/cert.pem -noout -subject -issuer -dates
journalctl -u cron --since '8 days ago' | grep kasset-tailnet-cert
```

근거:
- Tailscale HTTPS와 file certificate 갱신 책임:
  <https://tailscale.com/docs/how-to/set-up-https-certificates>
- `tailscale cert --cert-file --key-file --min-validity`:
  <https://tailscale.com/docs/reference/tailscale-cli#cert>
- Caddy static `tls <cert_file> <key_file>`:
  <https://caddyserver.com/docs/caddyfile/directives/tls>

사무실 공인 IP가 나중에 `KASSET_ADMIN_ALLOWED_IPS`에 `/32`로 추가되면 사무실에서는
`https://175-45-201-51.sslip.io/admin/ops`도 사용할 수 있다. 값이 확인되기 전에는
tailnet URL만 사용한다.

### 9.3 적용, 검증, 복구

적용 전에 Caddyfile과 실제 env를 백업한다. 첫 번째 `caddy validate`는
`KASSET_ADMIN_ALLOWED_IPS`를 전혀 전달하지 않아도 Caddyfile 기본값으로 유효한지를 확인한다.
두 번째 검증은 실제 Compose 치환 결과를 검사한다.

```bash
cd /opt/kasset-trader-core
cp -p deploy/kasset/Caddyfile deploy/kasset/Caddyfile.before-admin-guard
cp -p .env.kasset .env.kasset.before-admin-guard

docker run --rm \
  -e KASSET_DOMAIN=admin-guard.example.invalid \
  -e ACME_EMAIL=ops@example.invalid \
  -v "$PWD/deploy/kasset/Caddyfile:/etc/caddy/Caddyfile:ro" \
  caddy:2.11.4-alpine \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

docker compose --env-file .env.kasset -f docker-compose.kasset.yml config >/dev/null
docker compose --env-file .env.kasset -f docker-compose.kasset.yml run --rm --no-deps caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose --env-file .env.kasset -f docker-compose.kasset.yml up -d --force-recreate caddy
docker compose --env-file .env.kasset -f docker-compose.kasset.yml exec caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

미등록 ACME HTTP-01 probe는 `404`여야 한다. `308`이면 명시적 redirect가 challenge
경로를 잡은 것이므로 적용을 중단한다.

```bash
curl -H 'Host: 175-45-201-51.sslip.io' -sS -o /dev/null \
  -w "unclaimed ACME HTTP-01 probe => %{http_code}\n" \
  http://127.0.0.1/.well-known/acme-challenge/kasset-unclaimed-probe
```

`Valid configuration`을 확인한 뒤 smoke를 수행한다. 적용에 실패하면 마지막 정상 파일을
복구하고 Caddy만 다시 만든다.

```bash
cd /opt/kasset-trader-core
cp -p deploy/kasset/Caddyfile.before-admin-guard deploy/kasset/Caddyfile
cp -p .env.kasset.before-admin-guard .env.kasset
docker compose --env-file .env.kasset -f docker-compose.kasset.yml up -d --force-recreate caddy
docker compose --env-file .env.kasset -f docker-compose.kasset.yml exec caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

### 9.4 실제 진입점, 우회 방지, Cloudflare Tunnel 주의

운영 `KASSET_DOMAIN`은 `175-45-201-51.sslip.io` 하나다. 최근 Caddy 로그의 실사용
HTTPS Host도 이 값이었고 Android 앱을 포함한 API 호출은 이 origin으로 들어온다.
HTTPS site block은 관리자 guard를 health/web-terminal/catch-all보다 먼저 실행한다.
관리자 접두어가 아닌 `/health`, `/api/v1/*`, WebSocket 등은 기존 catch-all로 계속
전달되므로 Android/public API 계약은 바뀌지 않는다.

평문 80에는 `175.45.201.51:80`, `175.45.201.51`, `175.045.201.051` 같은 Host도
들어온다. 이 값들은 `KASSET_DOMAIN` host matcher와 일치하지 않는다. 그래서 Caddyfile은
host-agnostic `http://` site block에서 같은 관리자 guard를 먼저 실행하고, 허용된 요청만
명시적 `308` HTTPS redirect로 보낸다. zero-padded 변형도 이 catch-all에 흡수된다.
자동 redirect가 guard보다 먼저 실행되지 않도록 `auto_https disable_redirects`로 redirect만
끄고 동일한 `308`을 guard 뒤에 명시한다. Caddy 공식 문서상 `disable_redirects`는 인증서
자동화를 끄지 않는다. Caddy는 HTTP-01 challenge를 user route보다 먼저 별도 처리하며,
미등록 token만 명시적 `404` route로 떨어진다. HTTP-01과 TLS-ALPN-01은 모두 기본 활성화되고
한 방식이 실패하면 다른 방식으로 fallback한다.

근거:
- <https://caddyserver.com/docs/caddyfile/options#auto-https>
- <https://caddyserver.com/docs/automatic-https#effects>
- <https://caddyserver.com/docs/automatic-https#acme-challenges>
- Caddy 2.11.4 `Server.ServeHTTP`의 challenge 선처리:
  <https://github.com/caddyserver/caddy/blob/v2.11.4/modules/caddyhttp/server.go#L390-L394>

현재 `docker-compose.kasset.yml`에는 `cloudflared` 서비스가 없고 Caddy에도
`api.hsps-portal.xyz` site가 없다. 따라서 이 공개 도메인은 실사용 경로가 아니며
`https://api.hsps-portal.xyz/health`는 Cloudflare 530(error 1033), 직접 origin의
`https://175-45-201-51.sslip.io/health`는 200이다. Tunnel 복구는 이 변경의 범위 밖이고,
530은 관리자 경계가 차단했다는 증거가 아니다.

Tunnel을 나중에 복구할 때 connector를 `api:8000`으로 직접 보내면 Caddy를 완전히 우회하므로
금지한다. Caddy를 경유하도록 별도 설계하더라도 `remote_ip`가 보는 peer는 최종 사용자가 아니라
connector다. connector 주소를 `KASSET_ADMIN_ALLOWED_IPS`에 넣으면 모든 Tunnel 사용자가
같은 허용 peer로 보여 네트워크 경계가 무력화된다. 관리자 경로는 connector를 allowlist에
넣지 않은 채 차단하고, tailnet/확정된 사무실 공인 IP에서
`175-45-201-51.sslip.io` origin Caddy로 직접 접속한다. Tunnel 재구성 시에는 공개 API만
통과하고 관리자 두 접두어는 Caddy를 우회하지 않는다는 별도 검증이 필요하다.

### 9.5 Smoke: tailnet 허용, 외부 차단, public API 유지

tailnet에 연결된 PC에서 MagicDNS HTTPS origin을 직접 검사한다. 로그인 화면 `200`과
존재하지 않는 admin 경로 `404`는 둘 다 Caddy guard를 통과해 애플리케이션에 도달했다는
증거다.

```bash
TAILNET_HOST=vm-naver-kasset.tail624c43.ts.net
curl -sS -o /dev/null -w "tailnet web-auth login => %{http_code}\n" \
  "https://${TAILNET_HOST}/web-auth/login"
curl -sS -o /dev/null -w "tailnet admin smoke => %{http_code}\n" \
  "https://${TAILNET_HOST}/admin/__network_smoke_not_found__"
```

기대값은 차례로 `200`, `404`다. 실제 Google credential callback인
`POST /web-auth/google`과 로그인 성공 뒤 `/admin/ops`도 같은 두 접두어로 보호된다.

같은 tailnet PC에서 raw/zero-padded 평문 Host가 host-agnostic guard에 흡수되고 허용 요청은
HTTPS redirect 단계까지 가는지 확인한다.

```bash
TAILNET_ORIGIN=100.73.186.78
for HOST in '175.45.201.51:80' '175.45.201.51' '175.045.201.051'; do
  curl -H "Host: ${HOST}" -sS -o /dev/null \
    -w "${HOST} tailnet admin => %{http_code}\n" \
    "http://${TAILNET_ORIGIN}/admin/__network_smoke_not_found__"
done
```

세 요청 모두 guard를 통과한 뒤 `308`이어야 한다.

tailnet을 끈 외부 회선에서 직접 origin을 검사한다. 위조한 `X-Forwarded-For`를 붙여도
결과가 바뀌면 안 된다. 동시에 `/health`가 200인지 확인해 Android/public API 비영향을
확인한다.

```bash
DIRECT_HOST=175-45-201-51.sslip.io
curl -sS -o /dev/null -w "direct admin => %{http_code}\n" \
  "https://${DIRECT_HOST}/admin/__network_smoke_not_found__"
curl -H 'X-Forwarded-For: 100.64.0.1' -sS -o /dev/null \
  -w "direct spoofed-XFF web-auth => %{http_code}\n" \
  "https://${DIRECT_HOST}/web-auth/login"
curl -sS -o /dev/null -w "direct health => %{http_code}\n" \
  "https://${DIRECT_HOST}/health"
```

기대값은 차례로 `403`, `403`, `200`이다. 평문 direct IP와 zero-padded Host도 외부에서는
redirect 전에 차단되고, 비관리자 경로만 기존 `308`을 유지해야 한다.

```bash
for HOST in '175.45.201.51:80' '175.45.201.51' '175.045.201.051'; do
  curl -H "Host: ${HOST}" -sS -o /dev/null \
    -w "${HOST} external admin => %{http_code}\n" \
    "http://175.45.201.51/admin/__network_smoke_not_found__"
done
curl -sS -o /dev/null -w "direct-IP health redirect => %{http_code}\n" \
  "http://175.45.201.51/health"
```

기대값은 admin 3건 모두 `403`, health는 `308`이다.

공개 도메인은 현재 Tunnel 장애 때문에 아래 두 요청이 모두 `530`이다. 이는 현 상태 기록일
뿐 allowlist smoke가 아니다. Tunnel이 Caddy 경계를 우회하지 않도록 복구된 뒤에는 각각
`403`, `200`이어야 한다.

```bash
PUBLIC_HOST=api.hsps-portal.xyz
curl -sS -o /dev/null -w "public admin => %{http_code}\n" \
  "https://${PUBLIC_HOST}/admin/__network_smoke_not_found__"
curl -sS -o /dev/null -w "public health => %{http_code}\n" \
  "https://${PUBLIC_HOST}/health"
```
