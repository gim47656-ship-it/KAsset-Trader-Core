# Maker 기록: 순수 Linux CUELO 수동 배포 경로

RECORD: maker-LinuxCloudDeploy
DATE: 2026-10-07
SCOPE: cuelo-cloud, 수동 workflow, Docker 이미지 교체, drain, inspect
PATHS: .github/workflows/cuelo-cloud.yml, deploy/cuelo/host.sh, README.md(배포 절), doc/history/2026/10/07-cuelo-cloud/maker-LinuxCloudDeploy.md
STATUS: partial (로컬 mock·구문 검증 완료, 실제 host·ubuntu build 실행은 Main의 inspect/deploy dispatch 대기)

## 변경

- `.github/workflows/cuelo-cloud.yml`(신규, 191줄): `workflow_dispatch` 전용. 입력 `mode`(inspect 기본|deploy), `cuelo_sha`(40hex), `expected_version`(0.9.9), `expected_core`(18.7.0). 정규식 검증 뒤 env로만 전달. `permissions: contents: read`.
  - `target`(ubuntu): 공개 CUELO 조상 여부(`compare/<sha>...main`), `package.json` name·version·cueloBuild.coreVersion·core 5개 의존성 일치(jq).
  - `build`(ubuntu, deploy만): 기존 Dockerfile, UID/GID 1000 → `host.sh verify-image`(package/core/설치 core, `native-runtime-patch --check`, `prepare-runtime --check`, 사용자 1000:1000) → `docker save | gzip` → artifact(1일).
  - `host`(`[self-hosted, kasset-prod]`, concurrency `cuelo-cloud-deploy` cancel-in-progress false — Main 지시로 `production-deploy` 공유 안 함): artifact 다운로드 → `host.sh <mode>` → 로그를 Actions 요약에 기록, 이미지 파일 삭제.
- `deploy/cuelo/host.sh`(신규, 340줄): `inspect` / `deploy` / `verify-image`.
  - 접근권한 보고(내용 미독), 컨테이너 1개·compose 라벨·bind 3개(`/home/omp`, `/home/omp/.omp`, `/workspace`가 `/opt/cuelo` 하위 bind)·익명 볼륨/docker.sock 불가·uid:gid 1000:1000·프로필 쓰기 가능·프로필 존재와 `modelRoles` 해시를 검사. 문제는 누적해 inspect는 전부 보고 후 실패, deploy는 교체 전 실패.
  - deploy: 이미지 `docker load` → 이미지 ID·버전 재검사 → `compose.image.yaml.new-<run>` 후보 생성 후 `compose config`(base 대비 diff가 image 줄뿐) → drain(기존 컨테이너에서 health로 감시자 가동, `request.json` 원자 작성, ack 40초 한도, `unsettled`/`failed` ≠ 0 또는 ack 없음이면 교체 전 실패) → 이전 이미지에 `cuelo-cloud-rollback:<run>` 태그, 기존 override `.prev-<run>` 보존 → `up -d --no-deps --no-build --pull never cuelo` → health 4/4, Docker healthy, 실행 버전, mount·사용자·프로필 동일, 라벨 override 포함 확인. 교체 후 실패는 자동 rollback 없이 복구 근거와 수동 명령 출력.
  - 같은 이미지가 이미 실행 중이면 drain·교체 없이 검증만.
- `README.md`: `## 배포` 아래 `### CUELO 클라우드 수동 교체`(19줄) 추가. 기존 문장·KAsset 파일 무변경.

## 근거

- drain 계약: `lib/update-interrupt.ts:4-23,77-80,151-215`(CUELO private 저장소). 새 protocol 없음. `request.json` {id, reason, atUtc}만 쓰고 `excludeSessionIds`는 넣지 않아 현재 세션도 `pending-resume.json`에 기록.
- 감시자는 `/api/update-maintenance` 첫 요청에서 켜지며 `install.mjs health`의 web 점검이 같은 경로다(`install.mjs:47`). 별도 curl 없이 health가 감시자를 깨운다.
- health 출력/종료코드: 4개 모두 `OK` 줄이고 종료 0(`install.mjs:393-397`).

## 검증 r1(리비전 workflow sha256 `e2b2a701…`, host.sh `2ebbc0b2…`; r2 증거는 아래 「재작업 r2」)

| 명령 | exit | 결과 |
|---|---|---|
| `bash -n deploy/cuelo/host.sh` | 0 | 구문 OK |
| `bun -e`(yaml 패키지)로 workflow 파싱 | 0 | triggers=[workflow_dispatch], inputs 4개, host job runs-on [self-hosted,kasset-prod], concurrency cuelo-cloud-deploy/false, permissions contents:read |
| `jq -e` package 필터를 0.9.6/18.6.1 대 0.9.9/18.7.0로 | 0 / 1 | 일치·불일치 모두 기대대로 |
| 컨테이너 내부 JS(PKG/REQUEST/ACK/PENDING)를 실제 bun 1.4.2로 실행 | 0 | 버전 불일치 시 rc=1, request 원자 작성, ack 없음 rc=3, `CUELO_EXTERNAL_UPDATE_ROOT` 적용 |
| throwaway mock docker(`/tmp/lcd`, 삭제 대상)로 `host.sh` 8개 시나리오 | 아래 | |

mock 결과: inspect ok exit0(변경 0) / inspect badbind exit1 / deploy ok exit0(up 1회, tag 1회, drain 1회, 명령 `up -d --no-deps --no-build --pull never cuelo`, 새 파일은 compose.image.yaml 하나, 출력에 비밀 0) / multi(컨테이너 2개) exit1 up0 / badbind exit1 up0 / noack exit1 up0 tag0 / unsettled exit1 up0 tag0 / idmismatch exit1 up0 / healthfail exit1(up 1회 후 복구 안내 출력). 모든 시나리오에서 `down|rm|rmi|prune|volume|-v` 호출 0.

## 미확인(남은 검증)

- 실제 host: runner 계정의 docker 접근, `/opt/cuelo/compose.yaml`·`.env` 읽기, `/opt/cuelo` 쓰기, 현재 compose 라벨·mount·`modelRoles` 구조는 inspect dispatch 전까지 미확인(Main이 `inspect` 실행 → 결과 수용 → `deploy`).
- ubuntu-latest에서 공개 SHA의 `docker build`(디스크·시간), 이미지 내 `--check` 통과, artifact 업로드·다운로드(약 1~2GB 추정)는 실행 전 미확인. `actionlint`/`shellcheck`/`docker`는 이 환경에 없어 실행하지 못했다.
- `/api/update-maintenance`가 컨테이너 내부 `127.0.0.1` Host로 허용되는지는 코드로 확인(`isApiRequestHostAllowed`: 루프백/IP 허용)했으나 실제 응답은 미관측. 새 서버의 세션 자동 재개는 보장하지 않는다(아래 PID 재사용 주의).
- `docker compose up --pull never` 지원 버전과 `compose config` 출력 형식(image 줄 한 줄)은 host Compose에서 inspect 시점에 확인되지 않는다(deploy의 후보 검증 단계에서 교체 전 실패로 드러남).

## Main 실행 인터페이스

1. Actions → `CUELO Cloud` → Run workflow: `mode=inspect`, `cuelo_sha=772554adc16ea7993d94e13c44e43cfa45c028f6`, 기본 버전 0.9.9 / core 18.7.0. 로그와 요약의 `::error::`가 없고 "점검 통과"가 나오면 수용.
2. 같은 입력으로 `mode=deploy`.
3. 결과는 `gh run view <id> --log`(또는 요약)로 새 세션에서 읽는다.

## 교훈 후보

- 컨테이너 안 PID1을 교체하는 외부 runner 배포는 drain 요청을 컨테이너 안에서 서버와 같은 경로 규칙으로 써야 한다(호스트에서 bind 경로에 쓰면 0700 소유 1000 디렉터리에 막힌다). 증거: `host.sh` `drain_old`, mock noack/unsettled.

## 재작업 r2 (Main finding F1 + 재개 문구 정정)

- **F1**: `verify_running`이 `log "실행 버전: $(docker exec … PKG_JS)"`로 `docker exec` 실패를 로그 성공으로 덮던 것을 `ver="$(docker exec … 2>&1)" || fail "실행 중 컨테이너의 package/core 버전이 기대(…)와 다르다: …"`로 분리했다. 불일치 사유는 메시지에 남고 비밀은 없다.
- **재개 문구 정정(Main 관측)**: `lib/update-interrupt.ts:274-276` `runResumePass`는 `snapshot.writerPid === process.pid`이면 재개하지 않는다. 컨테이너 PID 재사용 시 새 서버가 옛 `pending-resume.json`을 재개하지 않을 수 있다. 그래서 README(3번 항목 문장, 제한 절 97행)와 `host.sh` 로그(`재개 대기열(관측값, 자동 재개는 보장하지 않는다)`)에서 "자동 재개" 단정을 지웠다. `pending` 건수는 파일에서 읽은 관측값이다. 사용자는 새 컨테이너에서 대화를 직접 이어 가며 배포 결과를 이 Actions 실행의 요약·로그로 확인한다. 코어·기존 protocol은 건드리지 않았다. 앞선 r1 문장 중 "현재 세션도 새 서버에서 자동 재개"는 철회한다.
- **r2 리비전**: host.sh sha256 `68df0462bfdcd226…`, workflow `e2b2a70135e5c989…`(불변), README `cc2726d4e59eba4a…`.
- **증거(cwd=`.omp/cuelo-cloud-deploy-kasset`)**: `bash -n deploy/cuelo/host.sh` exit 0. `evidence/maker-LinuxCloudDeploy-run-mock.sh <host.sh> evidence/maker-LinuxCloudDeploy-fake-docker.sh "ok inspect" "badbind inspect" "ok deploy" "multi deploy" "badbind deploy" "noack deploy" "unsettled deploy" "idmismatch deploy" "healthfail deploy" "badversion deploy"` 실행 exit 0. 원문 전체(시나리오별 출력·exit·docker 호출 로그): [maker-LinuxCloudDeploy-mock-r2.txt](evidence/maker-LinuxCloudDeploy-mock-r2.txt). 시나리오별 exit: ok inspect 0, badbind inspect 1, ok deploy 0(up1·tag1·drain1), multi 1(up0), badbind 1(up0), noack 1(up0 tag0), unsettled 1(up0 tag0), idmismatch 1(up0), healthfail 1(up1), **badversion 1(up1, 메시지 `실행 중 컨테이너의 package/core 버전이 기대(0.9.9 / 18.7.0)와 다르다: cuelo 0.9.6 core 18.6.1 version 0.9.6; cueloBuild.coreVersion 18.6.1`)**. 전 시나리오 `forbidden_nonexec=0`(down/rm/rmi/prune/volume/-v 호출 없음), `secret_leaked=0`. 하네스는 [fake-docker](evidence/maker-LinuxCloudDeploy-fake-docker.sh)·[run-mock](evidence/maker-LinuxCloudDeploy-run-mock.sh).
- **frozen diff**: [host.sh 전체 추가](evidence/maker-LinuxCloudDeploy-r2-host.sh.diff), [workflow 전체 추가](evidence/maker-LinuxCloudDeploy-r2-workflow.diff), [README diff](evidence/maker-LinuxCloudDeploy-r2-README.diff).
- 기존 성공 경로 raw는 위 mock 로그의 `SCEN=ok MODE=deploy` 구간이 같은 파일에서 갱신됐다(r1 출력은 폐기, r2가 정본).
