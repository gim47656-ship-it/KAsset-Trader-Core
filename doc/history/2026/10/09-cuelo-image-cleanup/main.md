# CUELO 이미지 정리 검수와 실행

RECORD:
DATE: 2026-10-09
SCOPE: CUELO cloud image cleanup
PATHS: deploy/cuelo/host.sh, .github/workflows/cuelo-cloud.yml, ci_shards/shard-1.txt
STATUS: accepted

## 승인과 범위

사용자가 오래된 CUELO 이미지 정리를 요청했다. 현재 0.10.4 이미지와 직전 0.9.9 복구본, 컨테이너 참조 이미지, 타 repository 공유 이미지는 보호한다. 컨테이너·볼륨·빌드 캐시·다른 서비스의 이미지는 대상이 아니다.

Main은 Maker의 frozen diff와 실제 shell/fake Docker unittest 11개 출력, 기존 모드 11시나리오 대조를 검수했다. exact ID·기본 dry-run·삭제 직전 재조회·태그 변화와 삭제 실패 시 중단을 확인했다. 실제 운영 Docker 호환성은 아래 수동 실행으로 확인했다.

## 실제 실행

실행 소스: `6a73f5e6aa30dd392561c9ced3317651abc53e57`.

- [조회 37910755889](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/37910755889): success. CUELO 이미지 ID 4개, 현재·직전복구 보호와 오래된 미사용 후보 2개 확인.
- [dry-run 37910904577](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/37910904577): success. `sha256:9ff61fd2b32fe4094a207ce73540d4118d8f06a3f0c8b52e84d5ec4845d2a4d5`, 태그 `cuelo:cloud-20261006-final` 하나만 계획에 포함.
- [삭제 37911087292](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/37911087292): success. 같은 ID의 `RM-OK`와 `RECEIPT removed` 확인. Docker 저장소 여유 39,448,484KB → 43,539,192KB, 변화 +4,090,708KB(약 3.90GiB). 같은 시간의 다른 쓰기가 섞일 수 있어 이미지 표시 size 합계로 회수량을 계산하지 않았다.
- 삭제 후 `bun /app/install.mjs health`: exit 0, web/usage/btw/subagent 4개 OK. `df -h /workspace`: 99G 중 58G 사용, 42G 여유, 59%.

현재 `cuelo-cloud:0d135bf4fd8b`, 직전 복구 `cuelo-cloud-rollback:37904014962-1`은 유지했다. 옛 `cuelo:cloud-20261006-sticker`는 기본 compose 파일이 참조하므로 이번 삭제 대상에서 제외했다. 설정·인증·작업 폴더·서비스 재시작은 변경하지 않았다.

## CI 보완

PR #128 첫 Test에서 새 테스트의 B007(미사용 loop 변수)과 exact-cover 목록 누락을 관측했다. Maker가 테스트 파일 lint/format을 보완하고, Main은 `ci_shards/shard-1.txt`의 정렬 위치에 파일 하나를 추가했다. 전체 shard를 재생성하지 않았다.

CI가 만든 `authoritative-collected-nodes.txt`를 재사용해 다음 명령을 실행했다.

```sh
python3 -m scripts.ci.file_shard_plan check --collected .omp/ci-collected-37910763603/authoritative-collected-nodes.txt --manifest-dir ci_shards --shard-count 4
```

cwd는 이 저장소, exit 0. 1,454 files across 4 shards, 0 missing, 0 duplicate, 0 unexpected. `git diff --check`도 exit 0.

고정된 Ruff 0.15.9 wheel의 lockfile 해시를 확인하고 임시 실행 파일만 준비했다. Maker가 새 테스트 파일의 `ruff check --no-cache`, `ruff format --check --no-cache`를 실행해 모두 exit0을 확인했다. `ruff format`은 `1 file left unchanged`였고 r2 unittest 11개 통과 증거를 재사용했다. Main은 검사 뒤 임시 실행 파일을 삭제했다.

[PR Test 37912651082](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/37912651082)가 성공했다. PR #128을 검증한 head `cdb4e487`로 고정해 `[skip ci]` squash했고, main의 통합 커밋은 `2cc44af806881aac8fc8dbced565142e42a46984`다. KAsset 앱 자동 Deploy는 유발하지 않았다.

## 후속 라이브 배포와 추가 정리

사용자 승인으로 [CUELO Cloud 37913850332](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/37913850332)가 공개 `81f62016b5c6475ad61306d2b39a475981a39efe`를 새 이미지로 교체했다. 버전 표기는 0.10.4/core18.8.6 그대로이고, guard #18·#19 수정 커밋을 적용한 것이다. 현재 이미지 ID는 `sha256:a3d64ba741d9180a0817b850b5d6cba6ea71358f241dad9f76b34ef2a2e2af80`, 직전 복구본은 기존 0.10.4 이미지(`sha256:2d953d9fd012ee6dab51d495285c2745ba5e57ec8589e104a78761ba84ba0d2b`)의 `cuelo-cloud-rollback:37913850332-1`이다.

기존 보존 기준(현재·직전 복구본 보호)에 따라 더 오래된 0.9.9 이미지를 추가 정리했다. [dry-run 37917513470](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/37917513470)에서 두 보호 ID와 구형 후보를 확인한 뒤 [삭제 37917719521](https://github.com/gim47656-ship-it/KAsset-Trader-Core/actions/runs/37917719521)를 실행했다. `sha256:3fdeb1513eb9f1eb53c186e7814cf482bda228eced67b352444bb36d36346975`의 두 태그(`cuelo-cloud:772554adc16e`, `cuelo-cloud-rollback:37904014962-1`)만 제거했다. `RM-OK`·`RECEIPT removed`·success를 확인했다.

추가 삭제의 Docker 저장소 여유는 39,374,756KB → 42,697,372KB(+3,322,616KB, 약 3.17GiB)였다. 두 번의 df 변화 합계는 7,413,324KB(약 7.07GiB)이며 같은 시간의 다른 쓰기가 섞일 수 있다. 추가 정리 뒤 health 4/4, 디스크 99G 중 59G 사용·41G 여유를 확인했다. 기본 compose 참조 이미지는 이번에도 남겼다. 서비스 재시작·데이터/볼륨/캐시 삭제 없이 끝냈다.
