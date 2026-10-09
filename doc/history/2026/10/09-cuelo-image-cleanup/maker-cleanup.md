# Maker 기록: CUELO 오래된 이미지 안전 조회·정리 경로

RECORD: maker-cleanup
DATE: 2026-10-09
SCOPE: cuelo-cloud, 이미지 inventory, 이미지 정리, docker image rm, 보호 이미지, fake docker
PATHS: deploy/cuelo/host.sh, .github/workflows/cuelo-cloud.yml, README.md(CUELO 클라우드 수동 교체 절), tests/test_cuelo_cloud_cleanup.py, doc/history/2026/10/09-cuelo-image-cleanup/maker-cleanup.md
STATUS: partial (fake Docker·실제 bash 경계 검증 완료. 실제 Docker·self-hosted runner·workflow 실행은 Main의 dispatch 대기)

## 변경

기존 수동 workflow `CUELO Cloud`와 `host.sh`에 모드 둘을 더했다. inspect·deploy·verify-image 함수와 출력은 건드리지 않았다(기존 줄 수정은 머리말 주석·usage 한 줄뿐이다).

- `host.sh images`: 읽기 전용 inventory. CUELO 이미지마다 전체 ID, 태그 전체, 생성 시각, size, 참조 컨테이너, `PROTECT`/`CANDIDATE` 이유를 출력한다. `docker system df` Images 행과 Docker 저장소 여유(df)도 출력한다.
- `host.sh cleanup-images`: `CLEANUP_IMAGE_IDS`(전체 `sha256:64hex`만, 최대 20개)로 명시한 후보만 지운다. `APPLY_CLEANUP=true`가 아니면 판정과 `PLAN`(이미지별 지울 태그 전체)만 출력하는 dry-run이다.
- workflow: `mode`에 `images`, `cleanup-images` 추가, 입력 `cleanup_image_ids`, `apply_cleanup`(기본 false) 추가, `cuelo_sha`를 선택 입력으로 바꿈(inspect·deploy는 `target`의 기존 40자 정규식이 빈 값을 그대로 거부). 두 새 모드는 `target`·`build`를 건너뛰고 host job만 돈다. 자동 trigger는 여전히 `workflow_dispatch` 하나다.

### CUELO 범위와 보호 규칙(`snapshot_images`)

범위는 repository 이름으로만 정한다: `cuelo-cloud:*`, `cuelo-cloud-rollback:*`, `cuelo:cloud-*`. 그 밖의 이미지·dangling은 나열도 삭제도 하지 않는다. 다음 중 하나라도 해당하면 `PROTECT`다.

| 이유 코드 | 뜻 |
|---|---|
| `current` | project `cuelo-cloud` / service `cuelo` 컨테이너(모든 상태)의 image ID |
| `rollback-latest:<tag>` | `cuelo-cloud-rollback:<run>-<attempt>` 중 run(다음 attempt) 번호가 최대인 태그의 ID |
| `rollback-unrecognized:<tag>` | rollback repo지만 `<run>-<attempt>` 형식이 아닌 태그 |
| `override-ref:<ref>` | `$ROOT/deploy/compose.image.yaml`이 지정한 image의 ID |
| `container-ref:<name(state)>` | 모든 상태(실행·중지·생성만 된)의 컨테이너가 참조하는 ID |
| `foreign-tag:<repo:tag>` | 다른 repository나 `<none>` 항목이 같은 ID를 가리킴 |
| `not-older-than-rollback` | 직전 복구 이미지보다 같거나 새로 만든 이미지(적재만 되고 아직 쓰이지 않은 새 이미지 포함) |
| `created-unknown`, `no-current-baseline`, `no-rollback-baseline` | 보호 기준을 세우지 못함 |

기준을 세우지 못하면(현재 컨테이너 없음, 복구 태그 없음, override 위치 접근 불가, 생성 시각 판독 불가) `problem`으로 쌓아 inventory와 cleanup 모두 실패시키고 모든 이미지를 PROTECT로 표시한다. 이 규칙으로 현재 0.10.4(`cuelo-cloud:0d135bf4fd8b`)와 직전 0.9.9(`cuelo-cloud-rollback:37904014962-1`)는 `current`/`rollback-latest`로 보호된다.

### 삭제 경로(`do_cleanup_images`, `remove_one`)

1. 입력 형식 검증(전체 ID·개수·`APPLY_CLEANUP` 값)은 Docker를 호출하기 전에 끝난다.
2. inventory를 읽고, 요청 ID마다 `CANDIDATE`인지 본다. 하나라도 아니면 아무것도 지우지 않는다(all-or-nothing). 후보의 태그 전체를 `PLAN`으로 출력하고 `PLAN_TAGS`에 저장한다.
3. `apply=true`면 `docker system df`·여유 KB를 찍은 뒤, 이미지마다 첫 태그를 지우기 직전에 Docker 상태를 전부 다시 읽는다. 참조·보호 판정이 바뀌었거나 태그 집합이 `PLAN`과 다르면 `RM-STOP`으로 그 이미지를 지우지 않고 중단한다.
4. 태그 하나씩 `docker image rm <tag>`(인자 정확히 하나, `-f`·prune·컨테이너/볼륨 명령 없음). 마지막 태그가 지워지면 이미지가 삭제되고 사후 `docker image inspect`로 존재를 확인한다.
5. 하나라도 실패하면 그 자리에서 멈추고 나머지는 `not-attempted`로 둔다. 끝에 `docker system df`·여유 KB를 다시 찍고 receipt(`RECEIPT removed|failed|not-attempted`)를 남긴 뒤 실패 종료한다. 용량은 합산하지 않고 df 전후 값과 측정 변화(KB)만 출력한다.

비밀(.env·환경 변수·인증 DB)은 읽거나 출력하지 않는다. 컨테이너 이름은 CUELO 이미지를 참조하는 것만 출력한다.

## 근거

- 이름 근거: 클라우드 이미지 이름 규칙은 `Tools/CUELO_Setup/HANDOFF.md:12`, `SERVER-MIGRATION.md:38,130`의 `cuelo:cloud-20261006-sticker`(`-final`)와 `host.sh`의 `cuelo-cloud:<sha12>`, `cuelo-cloud-rollback:<run>-<attempt>`다(공개 CUELO 저장소가 아니라 이 저장소 문서).
- 호출자 대조: `deploy/cuelo/host.sh`를 부르는 곳은 `.github/workflows/cuelo-cloud.yml`의 두 줄(`verify-image`, `"$MODE"`)뿐이다(grep). GitNexus는 이 환경에 CLI·`.gitnexus` 인덱스가 없어 impact를 돌릴 수 없었다. LSP도 설정된 서버가 없다. 대신 위 grep과 아래 before/after 동일성 실행으로 대조했다.
- Docker CLI 출력 형식은 `docker image ls --format`, `docker inspect -f`, `docker system df --format` 필드(`Type`·`TotalCount`·`Active`·`Size`·`Reclaimable`)를 구분자 `|`로 읽는다(`\t` 이스케이프에 기대지 않음).

## 검증(리비전 sha256: host.sh `382224f1…`, workflow `99f0b768…`, README `d8eb15f3…`, test `cee26eaa…`)

| 명령 | cwd | exit | 결과 |
|---|---|---|---|
| `bash -n deploy/cuelo/host.sh` | `.omp/cloud-cleanup` | 0 | 구문 OK |
| `python3 -m unittest tests.test_cuelo_cloud_cleanup -v` | `.omp/cloud-cleanup` | 0 | 11개 통과(약 17초). fake `docker`를 PATH에 주입해 실제 `bash host.sh images|cleanup-images` 실행 |
| 변이 검증(host.sh 복사본에서 규칙 하나씩 제거 후 같은 suite 실행) | `.omp/cloud-cleanup` | 0 | 삭제 직전 재조회 제거, 태그 계획 대조 제거, foreign-tag 무시, 컨테이너 참조 무시, newer-than-rollback 무시, 실패 뒤 계속 진행, dry-run이 삭제하도록 변경: 7개 모두 suite가 실패(CAUGHT) |
| 기존 모드 동일성: `git show HEAD:deploy/cuelo/host.sh`와 현재 파일을 이전 Maker의 fake docker(`doc/history/2026/10/07-cuelo-cloud/evidence/`, override 경로만 `deploy/`로 교정)로 11개 시나리오(inspect ok/badbind, deploy ok/multi/noack/unsettled/idmismatch/healthfail/badversion, verify-image, 알 수 없는 모드) 실행 | `/tmp/cuelo-parity`(임시, 정리함) | 0 | exit 코드(0,1,0,1,1,1,1,1,1,0,1)·출력·docker 호출 로그가 df 여유 숫자만 정규화하면 전부 동일(`diff` 빈 출력). deploy ok 시나리오가 exit 0으로 교체·검증까지 간다 |
| workflow YAML 조건 평가(bun + yaml 패키지로 `if` 식을 모드별로 계산) | `.omp/cloud-cleanup` | 0 | 아래 표 |

workflow 조건 평가(`target` → `build` → `host`, GitHub 의미: 상태 함수 없는 `if`는 선행이 success일 때만 평가):

| mode | target | build | host |
|---|---|---|---|
| inspect | 실행 | 건너뜀 | 열림(기존과 동일) |
| deploy | 실행 | 실행 | 열림(build 실패 시 닫힘, 기존과 동일) |
| images | 건너뜀 | 건너뜀 | 열림 |
| cleanup-images | 건너뜀 | 건너뜀 | 열림 |

trigger는 `workflow_dispatch` 하나, `permissions: contents: read`, host concurrency `cuelo-cloud-deploy`/`cancel-in-progress: false`, host env에 `CLEANUP_IMAGE_IDS`·`APPLY_CLEANUP`이 `inputs`에서 환경 변수로만 전달됨을 확인했다.

fake 시나리오가 확인한 동작(테스트 이름): inventory 사유 코드와 다중 태그·컨테이너 참조·compose 기본 image 안내(`test_images_inventory_reports_reasons_without_changes`), dry-run 기본(`..._defaults_to_dry_run`), 태그 단위 삭제와 receipt·전후 df(`..._apply_removes_only_explicit_candidates_tag_by_tag`), 현재·최신 복구·컨테이너 참조·공유 태그·새 이미지·형식 불명 복구 태그·비CUELO·dangling·없는 ID 거부(all-or-nothing), override 보호, 입력 오류는 Docker 호출 0번, 현재 컨테이너/복구 태그/override 위치가 없으면 전부 거부, preflight 뒤 컨테이너나 태그가 생기면 `RM-STOP`, 삭제 실패 시 중단·부분 receipt. 모든 시나리오에서 `-f`/`--force`/prune/volume/`rm`/`exec`/`run`/`tag`/`load`/compose 변경 호출이 없고 `.env` 값·KAsset 컨테이너 이름이 출력에 없다.

## 실행 환경 주의(재사용 가능)

- 이 저장소 pytest는 `tests/_socket_guard.py`가 자식 프로세스에 `PYTHONPATH` 시작 훅과 정책 fd를 주입한다. bash가 띄우는 fake `docker`가 일반 `python3`이면 시작 훅이 깨져 interpreter가 뜨지 않는다(실측: `Failed to import the site module`). fake를 `python -I`(격리 모드)로 실행하면 훅을 타지 않는다(실측 통과).
- 이 환경에는 pytest·PyYAML이 없어 pytest 자체는 돌리지 못했다. 테스트는 stdlib `unittest.TestCase`라 pytest가 그대로 수집한다. 위 가드 환경 주입을 수동으로 흉내 내(`AUTO_TRADER_TEST_SOCKET_GUARD=1`, `PYTHONPATH`) 2개 테스트가 통과함은 확인했다.

## 미확인

- 실제 Docker: `--no-trunc` ID 형식, `docker image rm <tag>`의 충돌 메시지, `docker system df --format` 필드, compose `config --images` 출력은 fake가 문서 기준으로 흉내 낸 것이다. 실제 host 첫 `images` 실행 결과로 확인해야 한다.
- 운영 host의 bash 버전: 연관 배열·`mapfile` 등 bash 4.4 기능을 쓴다(el8 기본 4.4). 이 환경은 5.2에서만 실행했다.
- workflow: `actionlint`·실제 Actions 파서가 없어 YAML은 위 평가로만 확인했다. host job `if`는 기존 `inspect`가 이미 같은 방식(`!cancelled()`와 건너뛴 build)으로 운영에서 통과한 패턴이다.
- 과거 이름 `cuelo:cloud-*`가 실제로 어떤 ID에 붙어 있는지, `compose.yaml` 기본 image가 그중 하나인지는 `images`의 `note: compose.yaml 기본 image가 …` 줄로 Main이 확인해야 한다(이 줄이 나오는 후보를 지우면 override 없이 `compose.yaml`만 쓰는 수동 실행이 그 이름을 찾지 못한다. README에도 적었다).

## Main이 실행할 입력과 안전 조건

KAsset-Trader-Core의 workflow `CUELO Cloud`(`cuelo-cloud.yml`)를, host.sh와 workflow가 같은 커밋인 ref에서 실행한다(머지 뒤 `main` 권장).

1. `mode=images`(다른 입력은 기본값, `cuelo_sha` 비움). 확인: 현재 `sha256:2d953d9f…`가 `PROTECT reasons=current,…`, `cuelo-cloud-rollback:37904014962-1`의 ID가 `rollback-latest`, `CANDIDATE`로 나온 ID의 태그에 `cuelo-cloud:0d135bf4fd8b`·`cuelo-cloud-rollback:37904014962-1`이 없음, `note: compose.yaml 기본 image` 줄의 영향 결정.
2. `mode=cleanup-images`, `cleanup_image_ids=<CANDIDATE 전체 ID 공백 구분>`, `apply_cleanup=false`: `PLAN` 줄의 태그 전체와 `DRY-RUN 완료` 확인.
3. 같은 입력에 `apply_cleanup=true`: `RM-OK`, `RECEIPT removed`, `[삭제 전]`/`[삭제 후]` df, 종료 코드 확인. 회수량은 df 전후 값으로만 말하고 size를 합산하지 않는다.

각 실행은 같은 concurrency 그룹이라 deploy와 겹치지 않는다. 컨테이너는 재시작하지 않는다. 어느 단계든 `::error::`나 `RM-STOP`·`RM-FAIL`이 나오면 중단하고 로그를 보고한다(자동 재시도·강제 삭제 없음).

## 재작업 r2: CI lint B007 (CLEANUP-CI-01)

- 원인(제품 결함 아님, 내 테스트 파일의 lint 위반): PR128 lint job이 `uv run ruff check app/ tests/ research/ scripts/`에서 `B007 Loop control variable 'iid' not used`(`tests/test_cuelo_cloud_cleanup.py:406`)로 실패했다(원문 로그 `local://cleanup-ci-lint.log` 418~432행). 같은 job의 다음 단계는 `uv run ruff format --check app/ tests/ research/ scripts/`(`.github/workflows/test.yml:48,51`)라서 B007만 고치면 format 단계가 다음 실패 지점일 수 있었다. 내 파일에는 88자 초과 코드 줄이 많았다.
- 변경: `tests/test_cuelo_cloud_cleanup.py`만 수정했다. `for iid, image in ...items()`를 `for image in ....values()`로 바꿔 B007을 없앴고, 같은 파일을 기존 `pyproject.toml`의 ruff 설정(line-length 88, 규칙 E·W·F·I·B·C4·UP, py313)에 맞게 직접 정리했다: 긴 호출은 한 줄에 하나씩 넣고 trailing comma를 달아 펼침(magic trailing comma로 포매터가 다시 접지 않음), f-string 안의 중첩 따옴표 제거, `IDS`·이미지 행·컨테이너를 표·헬퍼(`IMAGE_ROWS`, `container()`)로 분리, `'''`를 `"""`로(FAKE_DOCKER 본문은 바이트 동일). 검증 의미(시나리오·단언)는 그대로다. `host.sh`·workflow·README는 바꾸지 않았다.
- ruff 검증: 처음에는 환경에 `ruff`가 없어 수동 점검만 했으나, Main이 lockfile의 Ruff 0.15.9 공식 wheel 실행파일을 `.omp/ruff-check/ruff`에 준비해 줘서 실제로 돌렸다(cwd `.omp/cloud-cleanup`, `--no-cache`, 원문 `local://cleanup-ci-r3-ruff.log`). `ruff check` 통과, `ruff format --check` 통과(`1 file already formatted`), `ruff format`은 `1 file left unchanged`(전후 diff 0줄, sha256 `0276a732f0a22c48…` 그대로), 적용 후 `ruff check`·`ruff format --check` 재통과. 수동 정리가 포매터 출력과 같았다.
- 재검증(cwd `.omp/cloud-cleanup`, 리비전 test sha256 `0276a732f0a22c48…`, host.sh·workflow·README 불변): `python3 -m unittest tests.test_cuelo_cloud_cleanup -v` exit 0, 11개 통과(약 21초, 원문 `local://cleanup-ci-r2-unittest.log`). 정리한 파일이 여전히 안전 회귀를 잡는지 규칙 변이 5개로 다시 확인했다(원문 `local://cleanup-ci-r2-mutation.log`). 테스트 diff는 `local://cleanup-ci-r2-test.diff`.
- 교훈 후보: 조건: CI가 `ruff check`와 `ruff format --check`를 순서대로 돌리는 저장소에 새 `.py`를 넣을 때. 원인: 첫 lint가 한 단계에서 끝나 다음 단계(format)가 가려진다. 바꾼 행동: ruff가 없으면 88칸(전각 2칸) 초과 줄과 AST 근사 점검으로 사전 확인하고 magic trailing comma 형태로 펼쳐 쓴다.

## 교훈 후보(Main이 learn 여부 결정)

- 조건: pytest 아래에서 bash 스크립트를 fake 바이너리(Python)로 검증할 때. 원인: socket guard가 자식 Python 시작 훅을 주입한다. 바꾼 행동: fake를 `python -I`로 실행하는 bash 래퍼로 둔다. 근거: 이 문서 「실행 환경 주의」의 두 실측.
- 조건: `edit`로 같은 파일을 여러 번 고칠 때 줄 번호 앵커. 원인: 앞선 편집으로 줄 번호가 밀려 `PUT >345`가 다른 메서드 중간에 들어갔다(경고로만 알려 줌). 바꾼 행동: 경고가 나오면 즉시 해당 범위를 다시 읽고 `CUT`/`PUT @name`으로 옮겼다.
