# Raspberry Pi Docker 배포 가이드 (Retired)

> **상태:** 폐기됨 / 사용 금지  
> **이유:** ROB-263에서 Raspberry Pi Docker production deploy 경로를 제거했습니다.  
> **현재 production 경로:** MacBook native launchd 배포만 사용합니다.

## 현재 운영 원칙

- Auto Trader production은 MacBook native 서비스(`launchd`)가 단일 owner입니다.
- production 실행 체결 WebSocket은 Upbit 전용입니다. 주식 체결 증거는 MacBook
  worker의 Toss fill poller가 최대 2분 간격으로 수집합니다.
- KIS runtime/WebSocket은 배포하지 않으며, 남은 어댑터는 과거 레저 조회
  호환성을 위한 비활성 코드입니다.

## 사용해야 하는 배포 경로

```bash
# GitHub Actions
.github/workflows/deploy-macos-native.yml

# 원격 MacBook native 배포 스크립트
scripts/deploy-native.sh
```

수동 배포가 필요하면 `Deploy MacBook Native Production` workflow를 사용하거나, 운영 runbook에 따라 `scripts/deploy-native.sh`를 통해 MacBook native 서비스를 갱신하세요.

## 과거 Raspberry Pi stack 정리 명령

아래 명령은 **새 배포용이 아니라 기존 Raspberry Pi host 정리용**입니다.

```bash
cd /home/mgh3326/auto_trader

docker compose --env-file .env.prod -f docker-compose.prod.yml down --remove-orphans
```

정리 후에는 다음을 확인하세요.

```bash
docker ps -a | grep '^auto_trader_' || true
ps aux | grep -E 'websocket_monitor|upbit_websocket' | grep -v grep || true
```

## 참고

이 문서는 과거 운영 흔적을 남기기 위한 retired 문서입니다. 신규 Raspberry Pi Docker production 설정 절차를 추가하지 마세요.
