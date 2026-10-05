# KAsset-Trader-Core

**KAsset Trader Android 앱의 서버.** 국장(KRX) 중심의 **PAPER(모의) 자동매매**를 돌리고, 앱에 시세·관심종목·추천·주문·잔고 API를 제공합니다.

> 개인 운영 프로젝트이며 투자 조언이 아닙니다. 실계좌 주문 경로는 모두 기본 비활성(fail-closed)입니다.
> 이 저장소는 `mgh3326/auto_trader`에서 출발했지만 지금은 독립 운영됩니다. 원본의 MCP 에이전트 매매·`/invest` 대시보드·KIS·Upbit 경로는 운영에서 쓰지 않습니다.

## 무엇을 하나

- **PAPER 자동매매**: 장중 스캔 → 후보 선정 → AI 검토 → PAPER 주문 → 5단 청산 사다리(초기 손절 `진입가 − 2 ATR` 등). 현재 규칙과 근거는 [`HANDOFF.md`](HANDOFF.md).
- **Android 앱 API**: `app/extensions/kasset/api/` — 로그인(Google), 관심종목·종목 검색, 시세·차트·호가 스트림, 추천 승인/거절, PAPER 주문·체결, 푸시(FCM).
- **데이터 적재**: KR/US 일봉, 투자자 수급(네이버 모바일 API), DART 재무, 종목 마스터, 뉴스·공시.

앱 소스는 [HANSE의 `KAsset-Trader/android`](https://github.com/gim47656-ship-it/HANSE/tree/main/KAsset-Trader/android)에 있습니다. 앱 APK 빌드는 HANSE에서, 서버 테스트·배포는 이 저장소에서 관리합니다.

## 운영 구성

서버 한 대(`kasset-prod`, Tailscale)에서 `docker-compose.kasset.yml`로 돌립니다.

```mermaid
flowchart LR
    APP["KAsset Trader<br/>Android 앱"] -->|HTTPS| CADDY["caddy"]
    CADDY --> API["api<br/>앱 API · 시세 스트림"]

    subgraph Server["kasset-prod (docker compose)"]
        API
        SCHED["scheduler"] -->|작업 등록| REDIS[("Redis 작업 큐 · 캐시")]
        REDIS -->|TaskIQ| WORKER["worker<br/>자동매매 사이클 · 데이터 수집"]
        WORKER -->|MCP| AIMCP["ai-mcp<br/>codex exec · luna / sol"]
        API -->|MCP| AIMCP
        API --> DB[("PostgreSQL / TimescaleDB<br/>PAPER 원장 · 시세 · 재무 · 수급")]
        WORKER --> DB
        API --> REDIS
        MCP["mcp · analysis_readonly"] --> DB
        CRON["root cron"] --> JOB["일회성 worker 컨테이너<br/>DART 재무 · 종목 마스터 보충"]
        JOB --> DB
    end

    WORKER -->|시세·종목| TOSS["Toss Open API<br/>(조회 전용)"]
    API -->|시세·호가| TOSS
    WORKER --> EXT["DART · 네이버 수급 · 뉴스"]
    JOB -->|재무 공시 수집| DART["OpenDART"]
    WORKER -->|보조 판정| JEV["Jev<br/>(OpenRouter Decisions)"]
    API -->|푸시| FCM["FCM"]
```

| 서비스 | 역할 |
|---|---|
| `api` | 앱용 FastAPI (Caddy 뒤) |
| `worker` / `scheduler` | TaskIQ 작업자와 주기 트리거 (PAPER 자동매매 사이클, 시세·데이터 수집) |
| `ai-mcp` | AI sidecar. 구독형 `codex exec`를 MCP로 감싸 앱 밖에서 LLM을 호출 |
| `mcp` | 운영 조회용 MCP 서버 |
| `db` / `redis` / `caddy` | PostgreSQL(TimescaleDB) · Redis · HTTPS 프록시 |
| `migration` | 수동 배포 때만 쓰는 Alembic 실행 프로필 |

### 브로커

| 시장 | 시세 | 주문 |
|---|---|---|
| 국장 (KRX) | Toss | KAsset PAPER (모의 원장) |
| 미국 | Toss | KAsset PAPER |

Toss 실계좌는 조회 전용입니다. 앱 주문은 PAPER로만 나갑니다. KIS는 제거됐습니다.

### AI 경로

- Codex 기반 뉴스·후보·매매 분석과 별칭 생성은 `McpStructuredJsonClient` → `ai-mcp` → `codex exec` 순서로 호출합니다. 앱 런타임에 LLM provider SDK를 직접 넣지 않는 경계를 정적 테스트로 검사합니다.
- 추론 강도별 모델: `low`(뉴스·시장·스캔) = `gpt-6-luna`, `medium`/`high`(후보 검토·매매·크리티컬) = `gpt-6-sol` (`KASSET_AI_SIDECAR_EFFORT_MODELS`).
- 뉴스 관련성·후보 가산점의 보조 판정은 별도 Jev HTTP 클라이언트가 OpenRouter Decisions API(`typesafe/jev-1.13`)를 호출합니다. Codex sidecar와 다른 경로입니다. 상세는 [`docs/kasset/AI_DUAL_PROVIDER.md`](docs/kasset/AI_DUAL_PROVIDER.md).

## 배포

```
feature branch → PR → GitHub Actions Test 통과 → squash merge → Deploy(self-hosted runner, kasset-prod) 자동
```

- `main` 직접 push는 금지입니다.
- `alembic/versions` 변경이 있으면 자동 배포가 멈춥니다(`exit 2`). 이때는 Actions → Deploy → `workflow_dispatch`에서 `allow_migration=true`로 수동 배포합니다(DB 백업 후 migration).
- 롤백과 재배포도 `workflow_dispatch`(`sha`)로 합니다.

## 수동·예약 데이터 작업

서버에서 일회성 컨테이너로 실행합니다.

```bash
cd /opt/kasset-trader-core
docker compose --env-file .env.kasset -f docker-compose.kasset.yml run --rm -T \
  -w /app -e PYTHONPATH=/app worker /app/.venv/bin/python -m scripts.<script> [...]
```

| 스크립트 | 용도 | 실행 |
|---|---|---|
| `build_financial_fundamentals_snapshots` | DART 재무 지속 갱신 (최악 요청 수로 일일 18,000건 안에서 대상 선정) | root cron 매일 18:30 KST (`kasset-dart-daily.sh` 1단계) |
| `app.jobs.dart_disclosure_ingestion` (`-m`으로 실행) | DART 공시 목록 → `news_articles`(`feed_source=dart`), 최근 N일 upsert | root cron 매일 18:30 KST (`kasset-dart-daily.sh` 2단계, `--recent-days 2`) · 백필은 `--from-date`/`--to-date` |
| `sync_symbol_master` | 원본 KR/US universe에 있지만 검색 마스터에는 없는 보통주·ETF·미국 ADR 추가 | root cron 매일 22:00 KST, 수동 실행은 `--commit` |
| `build_investor_flow_snapshots` | 국장 전체 종목 최근 20일 수급 보충 | 승인 후 TaskIQ 월~토 08:30 KST (`investor_flow_snapshots.kr_scheduled`), 수동 백필도 가능 |
| `backfill_daily_candles` | 일봉 백필 | 수동 |
| `generate_symbol_search_aliases` | AI 검색 별칭 (sidecar `low`) | 수동 (`--commit`) |
| `kasset_strategy_validation` | 전략 검증 보고서 (DB 읽기 전용) | 수동 (`--output`) |
| `kasset_exit_rule_comparison` | 기준 백테스트의 진입을 고정하고 사전 고정 청산 변형(`PRESET_EXIT_VARIANTS`)만 비교. KRX 매도세·하한가 잠김 이월·체결 봉 거래량 1% 상한 반영, 끝까지 청산되지 않은 진입은 모든 변형에서 제외 (DB 읽기 전용) | 수동 (`--variants`, `--signal-start-at`·`--end-at`) |
| `kasset_swing_shadow` | KRX 스윙 3후보의 주문 없는 관측·가상 성과 조회 | `observe`는 저장, `report --since YYYY-MM-DD --signals`는 읽기 전용. 승인·마이그레이션 후 활성화하면 기존 평일 16:30 일봉 수집 성공 뒤 관측 |

`sync_symbol_master`와 AI 별칭 CLI는 기본 dry-run이며 `--commit`일 때 저장합니다. 나머지 수집기는 각 `--help`의 쓰기 옵션을 확인하세요. 새 스케줄 등록은 운영자 승인이 필요합니다.

스윙 SHADOW의 판정 규칙·활성화 순서·2주 성적 조회 방법은 [스윙 SHADOW 런북](docs/runbooks/kasset-swing-shadow.md)에 있습니다. 기본 off이며 단기 매매·주문·승격에는 연결하지 않습니다. 성과는 다음 거래일 시가 가상 진입에 대한 거래일별 관찰값으로, 실제 체결·계좌 수익이 아닙니다.

장기 추세·재무성장 SHADOW의 규칙·코호트 성과·벤치마크 해석은 [장기 SHADOW 런북](docs/runbooks/kasset-longterm-shadow.md)에 있습니다. 기본 off이며 주문·추천·승격에 연결하지 않습니다.

종목 동기화는 기존 `kr_symbol_universe`·`us_symbol_universe`에서 `symbol_master`로 **누락 행만** 보충합니다. 기존 이름 수정·상장폐지 반영·우선주 추가는 하지 않습니다. 원본 universe 수집과 검색 마스터 보충은 별개의 단계입니다.

- 실행 파일: `/usr/local/bin/kasset-symbol-master-daily.sh`
- 예약: `0 22 * * * /usr/local/bin/kasset-symbol-master-daily.sh >> /var/log/kasset-symbol-master-daily.log 2>&1`
- 중복 실행 방지: `/run/kasset-symbol-master-daily.lock`에 `flock`
- 점검: `crontab -l`, `systemctl is-active crond`, `/var/log/kasset-symbol-master-daily.log`
- 복구: 원본 수집 상태를 확인한 뒤 위 일회성 컨테이너 명령으로 `scripts.sync_symbol_master --commit`을 실행합니다. 중복 키는 추가하지 않습니다.

DART 일일 작업은 재무 지속 갱신 뒤에 공시 목록 수집을 이어서 돌립니다. 두 단계는 같은 `OPENDART_API_KEY` 하루 한도(20,000건)를 나눠 쓰며, 재무 자체 상한 18,000건을 유지합니다.

- 실행 파일: `/usr/local/bin/kasset-dart-daily.sh` (이전 판 `/root/kasset-dart-daily.sh.bak-20260927`)
- 예약: `30 18 * * * /usr/local/bin/kasset-dart-daily.sh >> /var/log/kasset-dart-daily.log 2>&1`
- 중복 실행 방지: 스크립트 전체를 `/run/kasset-dart-daily.lock`에 `flock`. 재무 단계가 실패해도 공시 단계는 실행됩니다.
- 점검: 로그의 `fundamentals exit=`·`disclosures exit=` 줄, `news_ingestion_runs`의 `feed_set=dart` 행
- 공시 백필: 한 번에 여러 달을 넣지 말고 하루씩 나눠 실행합니다. 공시 목록만 받는 경우 약 100건당 1요청입니다.

재무 cron의 수집 명령은 승인 후 아래 지속 갱신 모드로 바꿉니다.
기존 `--skip-existing`은 최초 백필 전용이며 한 행이라도 저장된 종목을 다시 받지 않으므로 지속 갱신에 쓰지 않습니다.

```bash
python -m scripts.build_financial_fundamentals_snapshots \
  --with-quarterly --refresh-due --all --commit --allow-partial
```

- 과거 5개 완료 회계연도와 올해 이미 끝난 분기를 대상으로 합니다. 법정 제출기한 전 조기 공시도 받을 수 있으며, 미종료 기간은 요청하지 않고 미공시 기간의 가짜 행을 만들지 않습니다.
- 기존 종목의 최신 기간 누락·부분 자료는 7일 후 재확인 대상, 정정 확인은 30일 후 대상이 됩니다. 대상 수와 예산 때문에 실제 갱신은 여러 날에 나뉠 수 있습니다. 성공한 수집 근거 없이 갱신시각을 만들지 않습니다.
- 자동 대상은 종목 마스터의 보통주 구분을 사용해 ETF·우선주 등을 제외합니다. 명시적 `--symbol` 수동 조회는 별개입니다.
- `--all`도 요청 예산을 넘기지 않도록 나눠 선정하며, 결과의 `projected_requests`·선정 사유·이월 종목 수를 확인합니다.
- 먼저 같은 옵션에 `--estimate-only`를 사용하면 외부 DART 호출·DB 저장 없이 선정 대상을 확인할 수 있습니다(`--commit`과 함께 사용하지 않음). `--dry-run` 기본 동작은 DB에 저장하지 않을 뿐 실제 DART 요청 예산을 소비합니다.
- 요청 카운터는 수집 프로세스 단위입니다. 같은 날 전체 재무 작업을 수동으로 반복하면 다른 프로세스·공시 수집과 사용량이 합산되지 않으므로, 운영 일일 실행과 중복하지 않습니다. 배포 직후의 소수 종목 확인도 공시용 여유분 안에서 별도로 제한합니다.
- 종료 코드는 정상·무대상 `0`, 예산 소진으로 무저장 `3`, 선택한 모든 종목이 실패하거나 빈 응답으로 끝나 무저장 `4`입니다. 일부 종목 실패를 허용한 실행은 경고와 실제 적재 범위를 함께 확인합니다.

### 수급 지속 갱신

기존 TaskIQ 예약을 사용하며 별도 root cron을 추가하지 않습니다. `.env.kasset`의
`INVESTOR_FLOW_SCHEDULE_ENABLED=true`는 예약 등록,
`INVESTOR_FLOW_SNAPSHOTS_COMMIT_ENABLED=true`는 DB 저장을 각각 허용합니다.
둘 다 기본값은 `false`입니다. 승인 후 값을 변경하고 worker·scheduler를 재생성해야
import 시점의 예약 라벨과 실행 설정에 반영됩니다.

- 월~토 08:30 KST에 오늘 또는 전일이 KRX 거래일이면 전체 active KR universe의 최근 20일을 upsert합니다. 금요일 자료는 토요일에도 수집 기회를 얻습니다.
- 실제 Naver 공표가 늦으면 해당 run에 최신 행이 없을 수 있습니다. 이후 run의 20일 보충이 결손을 다시 채우므로, 성공 종료뿐 아니라 `snapshot_date`별 종목 수를 확인합니다.
- 작업 결과의 `symbolsWithRows`와 `status=ok/partial/failed`를 확인합니다. 일부·전체 누락은 경고·오류 로그로 남기며, 이 결과로 자동 재시도를 추가하지 않습니다.
- 기존 16:40 `kasset.market_snapshots.kr.sync`는 보유·관심·추천 종목용이며, 전체 수급 갱신을 대신하지 않습니다.
- 초기 누락 보충은 위 일회성 컨테이너에서 `scripts.build_investor_flow_snapshots --market kr --all --days 20 --batch-size 100 --commit`을 실행합니다. 1년 백필은 `--days 250 --batch-size 25`로 DB 바인드 인자 한도를 피합니다.

## 개발 규칙 (요약)

정본은 [`CLAUDE.md`](CLAUDE.md)이고, 에이전트용 요약은 [`AGENTS.md`](AGENTS.md)입니다.

- **테스트·lint는 로컬에서 돌리지 않습니다.** 기본 검증 경로는 GitHub Actions입니다(`ruff` + `ty` + pytest 4 shard, TaskIQ smoke, migration round-trip). 새 테스트 파일은 `ci_shards/shard-N.txt` 한 곳에 정렬 위치로 추가합니다.
- 새 테이블이나 제약을 바꾸는 마이그레이션은 migration round-trip 테스트의 post-boundary 목록과 `tests/_schema_bootstrap.py` 버전도 함께 갱신합니다.
- 주문 레저 직접 쓰기, 게이트 완화, 스케줄 무단 등록은 금지입니다.
- 심볼 변환은 `app/core/symbol.py`만 사용합니다.

## 문서

- 현재 운영 상태와 다음 행동: [`HANDOFF.md`](HANDOFF.md)
- 작업 기록: [`doc/history/`](doc/history/)
- KAsset 설계: [`docs/kasset/`](docs/kasset/) — AI 경로, 자동매매 돌파 계약, Core 통합 지도
- 런북: [`docs/runbooks/`](docs/runbooks/)
