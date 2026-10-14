# 2026-10-14 — Dependabot 보안 알림 정리(lockfile 패치·미사용 pnpm lock 제거)

RECORD:
DATE: 2026-10-14
SCOPE: dependabot, security alerts, npm lockfile, pnpm-lock, uv.lock, multidict, nltk
PATHS: frontend/invest/package-lock.json, frontend/invest/pnpm-lock.yaml, uv.lock
STATUS: partial

## 요청
사용자: GitHub "Security and quality"에 쌓인 알림이 무엇인지 → 일괄 dismiss 대신 "업데이트해서 자동으로 닫히게"(2번) 선택.

## 관측(2026-10-14 09:50 KST, `gh api repos/gim47656-ship-it/KAsset-Trader-Core/...`)
- 열린 Dependabot 알림 54건(high 16·medium 27·low 11). code scanning은 분석 없음(미설정), secret scanning 0건.
- 52건이 `frontend/invest`(npm), 2건이 `uv.lock`(`multidict`, `nltk`).
- `frontend/invest`에 `package-lock.json`과 `pnpm-lock.yaml`이 함께 있어 같은 취약점이 대부분 두 번 잡혔다. CI(`.github/workflows/test.yml` `npm ci`)와 `Dockerfile.api`는 `package-lock.json`만 쓰고, `pnpm-lock.yaml`은 빌드·CI 어디에서도 쓰이지 않는다(마지막 갱신은 Renovate 시절 커밋).

## 변경(브랜치 `chore/dependabot-security-updates`, 기준 `origin/main` `498920bc9`)
- `frontend/invest/package-lock.json`: `npm update ... --package-lock-only`로 `package.json` 범위 안에서만 올렸다. `react-router(-dom)` 7.18.1→7.18.4, `undici` 7.25.0→7.30.0, `postcss` 8.5.22→8.5.30, `source-map-js` 1.2.1→1.2.2, `vitest`·`@vitest/*` 4.1.10→4.1.11, `vite` 8.1.5→8.3.4(따라서 `rolldown` 1.1.5→1.2.13, `lightningcss` 1.32→1.33 등 vite 하위 의존성). `package.json`은 바꾸지 않았다. `npm audit --package-lock-only`: 0 vulnerabilities.
- `frontend/invest/pnpm-lock.yaml`: 삭제(사용자 선택 2번에 포함한 lockfile 정리).
- `uv.lock`: `uv tool run --from uv==0.9.0 uv lock --upgrade-package multidict`로 `multidict` 6.7.1→6.9.1만 바꿨다. 로컬 기본 uv 0.13.0은 lock 헤더를 `revision = 5`로 올리고 `secretstorage` 의존 marker까지 다시 써서, 교차 리뷰(`xr-20261014005511-9d2739` R1-OAI-1) 지적대로 기존 형식(`revision = 3`)을 유지하는 0.9.0으로 다시 만들었다. 2차 리뷰 지적 없음, resolve pass(`xr-20261014005555-b66678`).

## 남긴 것
- `nltk` 3.10.3(high, "Model-artifact APIs bypass pathsec")은 패치 버전이 없다. 우리 코드·`pyproject.toml`은 `nltk`를 쓰지 않으며, dev 그룹의 `safety`(CI 보안 점검 단계에서 실행)가 끌고 온다. 운영 이미지는 `--no-dev`라 들어가지 않는다. Dependabot은 scope를 runtime으로 표시했지만 실제 경로는 dev 도구다. `safety` 교체는 CI 보안 단계 변경이라 이번 범위에서 뺐다.

## 검증
- 로컬 Python test·lint·build는 저장소 규칙상 실행하지 않는다. 프론트엔드 빌드·테스트(`frontend`)와 Python CI(`ci-required`, `migration`)는 PR의 GitHub Actions 결과로 판정한다.
