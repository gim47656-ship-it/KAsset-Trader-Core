#!/usr/bin/env bash
# CUELO 클라우드(순수 Linux Docker) 수동 점검·교체·이미지 정리. KAsset 배포(deploy/kasset)와 무관하다.
#
#   host.sh inspect         읽기 전용 점검. 아무것도 바꾸지 않는다.
#   host.sh deploy          미리 build한 이미지(IMAGE_TAR)를 load하고 cuelo 서비스 하나만 교체한다.
#   host.sh verify-image    이미지 하나만 검사한다(ubuntu build job과 host 양쪽이 같은 검사를 쓴다).
#   host.sh images          읽기 전용 이미지 inventory. CUELO 이미지별 ID·태그·컨테이너 참조·size와 보호/후보 이유를 출력한다.
#   host.sh cleanup-images  CLEANUP_IMAGE_IDS로 명시한 CUELO 후보만 지운다. APPLY_CLEANUP=true가 아니면 dry-run이다.
#
# 환경(workflow가 정규식 검증 뒤 넘긴다): CUELO_SHA EXPECTED_VERSION EXPECTED_CORE IMAGE IMAGE_ID IMAGE_TAR RUN_TAG
#                                         CLEANUP_IMAGE_IDS APPLY_CLEANUP(cleanup-images 전용)
#
# 지키는 것: compose.yaml·.env·프로필(config.yml, auth DB, APPEND_SYSTEM.md)을 쓰지 않는다. 새로 쓰는 파일은
# $ROOT/compose.image.yaml(image 한 줄) 뿐이다. inspect/deploy에는 down/rm/prune/볼륨 삭제와 자동 rollback이 없다.
# 이미지 삭제는 cleanup-images만 하며, 명시한 전체 image ID의 CUELO 태그를 `docker image rm`(-f·prune 없음)으로 지운다.
# 권한이 모자라면 바꾸지 않고 교체 전에 실패한다. 비밀 값(.env, 환경 변수, 인증 DB)은 읽거나 출력하지 않는다.
set -euo pipefail
umask 022

MODE="${1:?usage: host.sh inspect|deploy|verify-image|images|cleanup-images}"
ROOT="${CUELO_ROOT:-/opt/cuelo}"
PROJECT="cuelo-cloud"
SERVICE="cuelo"
BASE_FILE="$ROOT/compose.yaml"
OVERRIDE_FILE="$ROOT/deploy/compose.image.yaml"
APP_UID="1000"
APP_GID="1000"
DRAIN_TIMEOUT_SEC="${DRAIN_TIMEOUT_SEC:-40}"
HEALTH_TIMEOUT_SEC="${HEALTH_TIMEOUT_SEC:-180}"
MIN_FREE_KB=$((6 * 1024 * 1024))
REQUIRED_DESTS=(/home/omp /home/omp/.omp /workspace)

PROBLEMS=0
SWAPPED=0
WORK=""
NEW_OVERRIDE=""

log() { printf '[cuelo-cloud %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
problem() { PROBLEMS=$((PROBLEMS + 1)); printf '::error::%s\n' "$*"; }

recovery_hint() {
  log "자동 rollback은 하지 않는다. 복구 근거: 이전 이미지 태그 cuelo-cloud-rollback:${RUN_TAG:-<run>}, 이전 override ${OVERRIDE_FILE}.prev-${RUN_TAG:-<run>}(있을 때)."
  log "수동 복구: override의 image를 rollback 태그로 바꾼 뒤 docker compose -p $PROJECT -f $BASE_FILE -f $OVERRIDE_FILE up -d --no-deps --no-build $SERVICE"
}
fail() {
  printf '::error::%s\n' "$*" >&2
  if [ "$SWAPPED" = 1 ]; then recovery_hint >&2; fi
  exit 1
}
cleanup() {
  [ -z "$WORK" ] || rm -rf "$WORK"
  [ -z "$NEW_OVERRIDE" ] || rm -f "$NEW_OVERRIDE"
}
trap cleanup EXIT

need_env() { local name; for name in "$@"; do [ -n "${!name:-}" ] || fail "환경 변수 $name 이 비어 있다"; done; }
compose() { docker compose -p "$PROJECT" --project-directory "$ROOT" -f "$BASE_FILE" "$@"; }
compose_with() { local extra="$1"; shift; docker compose -p "$PROJECT" --project-directory "$ROOT" -f "$BASE_FILE" -f "$extra" "$@"; }

# 컨테이너 안에서 도는 JS. 서버(lib/update-interrupt.ts)와 같은 위치 규칙으로 drain 파일 계약만 쓴다.
ROOT_JS='const fs=require("fs"),os=require("os"),path=require("path");
const root=path.join(path.resolve((process.env.CUELO_EXTERNAL_UPDATE_ROOT||"").trim()||path.join(os.homedir(),".omp","external-update")),"interrupts");
'
REQUEST_JS="$ROOT_JS"'fs.mkdirSync(root,{recursive:true});
const f=path.join(root,"request.json"),t=f+"."+process.pid+".tmp";
fs.writeFileSync(t,JSON.stringify({id:process.env.DRAIN_ID,reason:process.env.DRAIN_REASON,atUtc:new Date().toISOString()},null,2)+"\n");
fs.renameSync(t,f);'
ACK_JS="$ROOT_JS"'let a;try{a=JSON.parse(fs.readFileSync(path.join(root,process.env.DRAIN_ID+".ack.json"),"utf8"))}catch{process.exit(3)}
const n=x=>Array.isArray(x)?x.length:0;
console.log("aborted="+n(a.aborted)+" failed="+n(a.failed)+" unsettled="+n(a.unsettled));'
PENDING_JS="$ROOT_JS"'let p={};try{p=JSON.parse(fs.readFileSync(path.join(root,"pending-resume.json"),"utf8"))}catch{}
const n=x=>Array.isArray(x)?x.length:0;
console.log("pending="+n(p.sessionIds)+" failures="+n(p.failures));'
# 패키지·core 버전 검사. 기대값은 환경 변수로 받고, 어긋나면 종료 코드 1이다.
PKG_JS='const p=require("/app/package.json"),c=p.cueloBuild||{},d=p.dependencies||{};
const core=process.env.EXPECTED_CORE,errs=[];
if(p.name!=="cuelo")errs.push("name "+p.name);
if(p.version!==process.env.EXPECTED_VERSION)errs.push("version "+p.version);
if(c.coreVersion!==core)errs.push("cueloBuild.coreVersion "+c.coreVersion);
for(const n of ["pi-agent-core","pi-ai","pi-coding-agent","pi-tui","pi-utils"]){
  if(d["@oh-my-pi/"+n]!==core)errs.push("dependency "+n+" "+d["@oh-my-pi/"+n]);
  let v="missing";try{v=require("/app/node_modules/@oh-my-pi/"+n+"/package.json").version}catch{}
  if(v!==core)errs.push("installed "+n+" "+v);
}
console.log("cuelo "+p.version+" core "+c.coreVersion);
if(errs.length){console.error(errs.join("; "));process.exit(1)}'

# ── 이미지 검사 ────────────────────────────────────────────────────────────────
verify_image() {
  need_env IMAGE EXPECTED_VERSION EXPECTED_CORE
  local actual uid gid before="$PROBLEMS"
  if [ -n "${IMAGE_ID:-}" ]; then
    actual="$(docker image inspect -f '{{.Id}}' "$IMAGE")" || { problem "이미지 $IMAGE 가 없다"; return; }
    [ "$actual" = "$IMAGE_ID" ] || problem "이미지 ID 불일치(build job과 다르다)"
  fi
  docker run --rm --network none -e EXPECTED_VERSION -e EXPECTED_CORE --entrypoint bun "$IMAGE" -e "$PKG_JS" \
    || problem "이미지의 package/core 버전이 기대($EXPECTED_VERSION / $EXPECTED_CORE)와 다르다"
  docker run --rm --network none --entrypoint node "$IMAGE" /app/Tools/CUELO_Setup/files/native-runtime-patch.js --check --target /app \
    || problem "native runtime patch --check 실패"
  docker run --rm --network none --entrypoint bun "$IMAGE" /app/bin/prepare-runtime.js --check \
    || problem "prepare-runtime --check 실패"
  uid="$(docker run --rm --network none --entrypoint id "$IMAGE" -u)" || uid="?"
  gid="$(docker run --rm --network none --entrypoint id "$IMAGE" -g)" || gid="?"
  [ "$uid:$gid" = "$APP_UID:$APP_GID" ] || problem "이미지 사용자 $uid:$gid 가 $APP_UID:$APP_GID 가 아니다"
  if [ "$PROBLEMS" = "$before" ]; then log "이미지 검사 완료: $IMAGE"; fi
}

# ── 호스트 접근권한(내용은 읽지 않는다) ──────────────────────────────────────────────
access_report() {
  local p flags
  log "runner: $(id -un) uid=$(id -u) groups=$(id -Gn | tr ' ' ',')"
  docker version -f 'server={{.Server.Version}}' >/dev/null 2>&1 || problem "docker 데몬에 접근할 수 없다"
  docker compose version --short >/dev/null 2>&1 || problem "docker compose를 쓸 수 없다"
  log "docker server $(docker version -f '{{.Server.Version}}' 2>/dev/null || echo ?), compose $(docker compose version --short 2>/dev/null || echo ?)"
  for p in "$ROOT" "$BASE_FILE" "$ROOT/.env" "$ROOT/home" "$ROOT/workspace" "$OVERRIDE_FILE"; do
    flags=""
    [ -r "$p" ] && flags+="r" || flags+="-"
    [ -w "$p" ] && flags+="w" || flags+="-"
    [ -x "$p" ] && flags+="x" || flags+="-"
    log "경로 $p: $(stat -c 'owner=%U:%G mode=%a' "$p" 2>/dev/null || echo 'stat 불가/없음') runner=$flags"
  done
  [ -d "$ROOT" ] && [ -x "$ROOT" ] || problem "$ROOT 에 접근할 수 없다"
  [ -r "$BASE_FILE" ] || problem "runner가 $BASE_FILE 을 읽을 수 없다"
  if [ -e "$ROOT/.env" ] && [ ! -r "$ROOT/.env" ]; then problem "runner가 $ROOT/.env 를 읽을 수 없다(compose 보간 불가). 권한은 자동으로 바꾸지 않는다"; fi
  [ -w "$ROOT/deploy" ] || problem "runner가 $ROOT/deploy 에 이미지 지정 파일을 쓸 수 없다. 승인된 권한 설정이 필요하다"
  if [ -e "$OVERRIDE_FILE" ] && [ ! -w "$OVERRIDE_FILE" ]; then problem "기존 $OVERRIDE_FILE 을 쓸 수 없다"; fi
  local root_dir free
  root_dir="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)"
  if [ -n "$root_dir" ] && free="$(df -Pk "$root_dir" 2>/dev/null | awk 'NR==2{print $4}')" && [ -n "$free" ]; then
    log "Docker 저장소 여유 ${free}KB"
    [ "$free" -ge "$MIN_FREE_KB" ] || problem "Docker 저장소 여유가 ${MIN_FREE_KB}KB 미만이다"
  else
    log "Docker 저장소 여유 확인 불가(권한)"
  fi
}

# ── 현재 컨테이너 ─────────────────────────────────────────────────────────────────
FOUND_CID=""
find_container() {
  local ids count
  ids="$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$SERVICE")" || ids=""
  count="$(printf '%s' "$ids" | grep -c . || true)"
  if [ "$count" != 1 ]; then
    problem "project $PROJECT / service $SERVICE 실행 컨테이너가 정확히 1개가 아니다($count)"
    return 1
  fi
  FOUND_CID="$ids"
}

mounts_of() {
  docker inspect -f '{{range .Mounts}}{{.Type}} {{.Source}} {{.Destination}} rw={{.RW}}{{"\n"}}{{end}}' "$1" | sed '/^$/d' | LC_ALL=C sort
}

roles_of() {
  docker exec -i "$1" sh -s <<'SH'
d=/home/omp/.omp/agent
printf 'config.yml=%s agent.db=%s APPEND_SYSTEM.md=%s ' \
  "$([ -s $d/config.yml ] && echo ok || echo missing)" \
  "$([ -s $d/agent.db ] && echo ok || echo missing)" \
  "$([ -s $d/APPEND_SYSTEM.md ] && echo ok || echo missing)"
block="$(awk '/^modelRoles:/{p=1;print;next} p&&/^[^[:space:]#]/{p=0} p' $d/config.yml 2>/dev/null)"
if [ -z "$block" ]; then echo "modelRoles=none"; exit 0; fi
printf 'modelRoles=%s lines=%s\n' "$(printf '%s' "$block" | sha256sum | cut -d' ' -f1)" "$(printf '%s\n' "$block" | wc -l)"
SH
}

health_ok() {
  local out
  out="$(docker exec "$1" bun /app/install.mjs health 2>&1)" || true
  printf '%s\n' "$out"
  [ "$(printf '%s\n' "$out" | grep -c '^OK ')" = 4 ]
}

# 교체 전 불변 조건. 어긋나면 problem만 쌓고 호출부가 실패시킨다.
OLD_CID="" OLD_IMAGE_ID="" OLD_IMAGE_REF="" OLD_MOUNTS="" OLD_ROLES="" OLD_UID=""
check_current() {
  local cid labels files wdir type src dest dst seen cur out
  find_container || return 1
  cid="$FOUND_CID"
  OLD_CID="$cid"
  OLD_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$cid")"
  OLD_IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$cid")"
  labels="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$cid")"
  files="${labels%%|*}"; wdir="${labels#*|}"
  log "컨테이너 $(docker inspect -f '{{.Name}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} started={{.State.StartedAt}}' "$cid")"
  log "image ref=$OLD_IMAGE_REF id=$OLD_IMAGE_ID"
  log "compose config_files=$files working_dir=$wdir"
  case "$files" in
    "$BASE_FILE"|"$BASE_FILE,$OVERRIDE_FILE") ;;
    *) problem "compose config_files 라벨이 $BASE_FILE (+$OVERRIDE_FILE) 가 아니다" ;;
  esac
  [ "$wdir" = "$ROOT" ] || problem "compose working_dir 가 $ROOT 가 아니다"

  OLD_MOUNTS="$(mounts_of "$cid")"
  printf '%s\n' "$OLD_MOUNTS" | sed 's/^/  mount /'
  while read -r type src dest _; do
    [ -n "$type" ] || continue
    [ "$type" = bind ] || problem "bind가 아닌 마운트가 있다($type → $dest). 익명 볼륨/볼륨은 허용하지 않는다"
    case "$src" in "$ROOT"/*) ;; *) problem "마운트 $dest 의 원본이 $ROOT 하위가 아니다" ;; esac
  done <<< "$OLD_MOUNTS"
  for dst in "${REQUIRED_DESTS[@]}"; do
    seen="$(printf '%s\n' "$OLD_MOUNTS" | awk -v d="$dst" '$1=="bind" && $3==d' | grep -c . || true)"
    [ "$seen" = 1 ] || problem "필수 bind $dst 가 없다"
  done

  OLD_UID="$(docker exec "$cid" id -u 2>/dev/null || echo '?'):$(docker exec "$cid" id -g 2>/dev/null || echo '?')"
  log "컨테이너 사용자 $OLD_UID"
  [ "$OLD_UID" = "$APP_UID:$APP_GID" ] || problem "컨테이너 사용자 $OLD_UID 가 새 이미지의 $APP_UID:$APP_GID 와 다르다"
  docker exec "$cid" sh -c 'test -w /home/omp/.omp/agent && test -w /workspace' || problem "컨테이너 사용자가 프로필/workspace에 쓸 수 없다"
  cur="$(docker exec -e EXPECTED_VERSION=- -e EXPECTED_CORE=- "$cid" bun -e "$PKG_JS" 2>/dev/null || true)"
  log "현재 버전: $(printf '%s\n' "$cur" | head -n 1)"
  OLD_ROLES="$(roles_of "$cid")"
  log "프로필: $OLD_ROLES"
  case "$OLD_ROLES" in *config.yml=missing*|*agent.db=missing*|*modelRoles=none*) problem "프로필 구성이 기대와 다르다(config.yml/agent.db/modelRoles)" ;; esac
  if out="$(health_ok "$cid")"; then log "install.mjs health 4/4"; else log "경고: 현재 install.mjs health 4/4 아님"; fi
  printf '%s\n' "$out" | sed 's/^/  health /'
}

# ── inspect ────────────────────────────────────────────────────────────────────
do_inspect() {
  log "점검 대상 공개 CUELO ${CUELO_SHA:-?} (기대 ${EXPECTED_VERSION:-?} / core ${EXPECTED_CORE:-?})"
  access_report
  check_current || true
  if [ "$PROBLEMS" -gt 0 ]; then fail "점검에서 문제 $PROBLEMS 건. deploy는 이 문제가 풀리기 전에는 교체하지 않는다."; fi
  log "점검 통과: deploy 선행 조건을 모두 만족한다(교체는 하지 않았다)."
}

# ── deploy ─────────────────────────────────────────────────────────────────────
prepare_override() {
  WORK="$(mktemp -d)"
  NEW_OVERRIDE="$OVERRIDE_FILE.new-$RUN_TAG"
  printf 'services:\n  %s:\n    image: %s\n' "$SERVICE" "$IMAGE" > "$NEW_OVERRIDE" || { problem "override 임시 파일을 쓸 수 없다"; return; }
  local err
  if ! compose config > "$WORK/base.cfg" 2> "$WORK/err"; then
    err="$(head -n 1 "$WORK/err" | cut -c1-200)"; problem "compose config 실패: $err"; return
  fi
  if ! compose_with "$NEW_OVERRIDE" config > "$WORK/new.cfg" 2> "$WORK/err"; then
    err="$(head -n 1 "$WORK/err" | cut -c1-200)"; problem "override 포함 compose config 실패: $err"; return
  fi
  local other added
  other="$(diff "$WORK/base.cfg" "$WORK/new.cfg" | grep -E '^[<>]' | grep -vcE '^[<>] +image: ' || true)"
  added="$(grep -cE "^ +image: $IMAGE\$" "$WORK/new.cfg" || true)"
  [ "$other" = 0 ] || problem "override가 image 외 설정 $other 줄을 바꾼다"
  [ "$added" = 1 ] || problem "override 적용 후 서비스 image가 $IMAGE 하나가 아니다"
  log "override 검증: compose config 차이는 image 줄뿐이다"
}

drain_old() {
  local id="cuelo-cloud-$RUN_TAG" deadline out=""
  # 탭이 없어도 감시자가 돌도록 health를 한 번 친다(web 점검이 GET /api/update-maintenance).
  health_ok "$OLD_CID" >/dev/null || log "경고: 현재 health 4/4가 아니다(감시자 가동은 web만 필요)"
  docker exec -e "DRAIN_ID=$id" -e "DRAIN_REASON=CUELO cloud image replacement ($RUN_TAG)" "$OLD_CID" bun -e "$REQUEST_JS" \
    || fail "drain 요청을 쓸 수 없다. 교체하지 않았다."
  log "drain 요청 id=$id (excludeSessionIds 없음: 현재 세션도 pending-resume에 기록된다)"
  deadline=$((SECONDS + DRAIN_TIMEOUT_SEC))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if out="$(docker exec -e "DRAIN_ID=$id" "$OLD_CID" bun -e "$ACK_JS" 2>/dev/null)"; then break; fi
    out=""; sleep 2
  done
  [ -n "$out" ] || fail "drain ack가 ${DRAIN_TIMEOUT_SEC}초 안에 없다. 세션은 중단됐을 수 있고 교체하지 않았다. 다시 실행하면 새 요청 id로 재시도한다."
  log "drain ack: $out"
  case "$out" in
    "aborted="*" failed=0 unsettled=0") ;;
    *) fail "drain에서 정리되지 않은 작업이 있다($out). 데이터 보존을 위해 교체하지 않았다. 해당 세션은 중단된 상태일 수 있다." ;;
  esac
  log "drain 후 재개 대기열: $(docker exec "$OLD_CID" bun -e "$PENDING_JS" 2>/dev/null || echo 확인불가)"
}

verify_running() {
  local cid="$1" labels mounts roles newuid deadline health ver
  [ "$(docker inspect -f '{{.Image}}' "$cid")" = "$IMAGE_ID" ] || fail "실행 컨테이너의 image ID가 새 이미지와 다르다"
  [ "$(docker inspect -f '{{.Config.Image}}' "$cid")" = "$IMAGE" ] || fail "실행 컨테이너의 image 이름이 $IMAGE 가 아니다"
  deadline=$((SECONDS + HEALTH_TIMEOUT_SEC))
  until health_ok "$cid" >"$WORK/health.out" 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || { sed 's/^/  health /' "$WORK/health.out"; fail "새 컨테이너 install.mjs health 4개가 ${HEALTH_TIMEOUT_SEC}초 안에 OK가 되지 않았다"; }
    sleep 5
  done
  sed 's/^/  health /' "$WORK/health.out"
  deadline=$((SECONDS + 120))
  until health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")" && [ "$health" = healthy ]; do
    [ "$SECONDS" -lt "$deadline" ] || fail "Docker health 상태가 healthy가 아니다($health)"
    sleep 5
  done
  log "Docker health=healthy"
  ver="$(docker exec -e EXPECTED_VERSION -e EXPECTED_CORE "$cid" bun -e "$PKG_JS" 2>&1)" \
    || fail "실행 중 컨테이너의 package/core 버전이 기대($EXPECTED_VERSION / $EXPECTED_CORE)와 다르다: $(printf '%s' "$ver" | tr '\n' ' ' | cut -c1-300)"
  log "실행 버전: $ver"
  mounts="$(mounts_of "$cid")"
  [ "$mounts" = "$OLD_MOUNTS" ] || fail "마운트가 교체 전과 다르다"
  log "마운트 교체 전과 동일(bind $(printf '%s\n' "$mounts" | grep -c . || true)개, 익명 볼륨 0)"
  newuid="$(docker exec "$cid" id -u):$(docker exec "$cid" id -g)"
  [ "$newuid" = "$OLD_UID" ] || fail "컨테이너 사용자가 바뀌었다($newuid)"
  roles="$(roles_of "$cid")"
  [ "$roles" = "$OLD_ROLES" ] || fail "프로필 존재/modelRoles 해시가 교체 전과 다르다"
  log "프로필 보존 확인: $roles"
  labels="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$cid")"
  [ "$labels" = "$BASE_FILE,$OVERRIDE_FILE" ] || fail "compose config_files 라벨이 override를 포함하지 않는다($labels)"
  log "재개 대기열(관측값, 자동 재개는 보장하지 않는다): $(docker exec "$cid" bun -e "$PENDING_JS" 2>/dev/null || echo 확인불가)"
}

do_deploy() {
  need_env CUELO_SHA EXPECTED_VERSION EXPECTED_CORE IMAGE IMAGE_ID IMAGE_TAR RUN_TAG
  case "$RUN_TAG" in *[!A-Za-z0-9_.-]*|"") fail "RUN_TAG 형식이 올바르지 않다" ;; esac
  log "교체 대상 공개 CUELO $CUELO_SHA → $IMAGE (기대 $EXPECTED_VERSION / core $EXPECTED_CORE)"
  access_report
  check_current || true
  [ -f "$IMAGE_TAR" ] || problem "이미지 파일이 없다"
  [ "$PROBLEMS" = 0 ] || fail "교체 전 점검 실패 $PROBLEMS 건. 아무것도 바꾸지 않았다."

  docker load -i "$IMAGE_TAR" >/dev/null || fail "docker load 실패. 실행 중 컨테이너는 그대로다."
  verify_image
  prepare_override
  [ "$PROBLEMS" = 0 ] || fail "새 이미지·override 검증 실패 $PROBLEMS 건. 실행 중 컨테이너는 그대로다."

  if [ "$OLD_IMAGE_ID" = "$IMAGE_ID" ]; then
    log "이미 같은 이미지로 실행 중이다. 교체와 drain 없이 검증만 한다."
    verify_running "$OLD_CID"
    log "완료(변경 없음)"
    return
  fi

  drain_old

  docker tag "$OLD_IMAGE_ID" "cuelo-cloud-rollback:$RUN_TAG" || fail "롤백 태그를 만들 수 없다. 교체하지 않았다."
  if [ -e "$OVERRIDE_FILE" ]; then cp -p "$OVERRIDE_FILE" "$OVERRIDE_FILE.prev-$RUN_TAG" || fail "기존 override를 보존할 수 없다. 교체하지 않았다."; fi
  mv -f "$NEW_OVERRIDE" "$OVERRIDE_FILE"; NEW_OVERRIDE=""
  SWAPPED=1
  log "교체: $SERVICE 서비스만 --no-deps --no-build (이전 이미지 $OLD_IMAGE_REF → 태그 cuelo-cloud-rollback:$RUN_TAG 보존)"
  compose_with "$OVERRIDE_FILE" up -d --no-deps --no-build --pull never "$SERVICE" || fail "compose up 실패"
  local new
  new="$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$SERVICE")"
  [ "$(printf '%s' "$new" | grep -c . || true)" = 1 ] || fail "교체 뒤 cuelo 컨테이너가 1개가 아니다"
  [ "$new" != "$OLD_CID" ] || fail "컨테이너가 재생성되지 않았다"
  verify_running "$new"
  log "완료: $IMAGE 로 교체·검증했다."
}

# ── 이미지 inventory / cleanup (images, cleanup-images) ───────────────────────────────────
# CUELO 범위는 repository 이름으로만 정한다. 다른 repository나 <none> 항목이 같은 ID를 가리키면 그 이미지는 건드리지 않는다.
# 삭제는 cleanup-images + APPLY_CLEANUP=true + 전체 image ID 목록일 때만, 태그 단위 `docker image rm`(-f·prune 없음)이다.
ROLLBACK_REPO="cuelo-cloud-rollback"
MAX_CLEANUP_IDS=20
IMG_ID_RE='^sha256:[0-9a-f]{64}$'
declare -A IMG_TAGS IMG_FOREIGN IMG_CONT IMG_CREATED IMG_SIZE IMG_EPOCH IMG_VERDICT IMG_WHY PLAN_TAGS
IMG_IDS=()
REQ_IDS=()
CUR_IDS=" "
ROLLBACK_ID="" ROLLBACK_TAG="" ROLLBACK_EPOCH="" OVERRIDE_REF="" OVERRIDE_ID="" FREE_KB=""

is_cuelo_ref() {
  case "$1:$2" in
    cuelo-cloud:*|"$ROLLBACK_REPO":*|cuelo:cloud-*) return 0 ;;
  esac
  return 1
}
full_id() { printf 'sha256:%s' "${1#sha256:}"; }
tags_of() { printf '%s\n' ${IMG_TAGS[$1]:-} | LC_ALL=C sort | tr '\n' ' ' | sed 's/ $//'; }

# 지금 Docker 상태를 읽어 CUELO 이미지별 보호/후보 판정을 새로 만든다. 읽기 전용이다.
snapshot_images() {
  local out id repo tag cimg cname cstate ref info epoch ranked="" why
  local -a carr why_list
  IMG_IDS=(); IMG_TAGS=(); IMG_FOREIGN=(); IMG_CONT=(); IMG_CREATED=(); IMG_SIZE=(); IMG_EPOCH=(); IMG_VERDICT=(); IMG_WHY=()
  CUR_IDS=" "; ROLLBACK_ID=""; ROLLBACK_TAG=""; ROLLBACK_EPOCH=""; OVERRIDE_REF=""; OVERRIDE_ID=""

  out="$(docker image ls --no-trunc --format '{{.ID}}|{{.Repository}}|{{.Tag}}')" || fail "docker image ls 실패"
  while IFS='|' read -r id repo tag; do
    [ -n "$id" ] || continue
    id="$(full_id "$id")"
    if is_cuelo_ref "$repo" "$tag"; then
      [ -n "${IMG_TAGS[$id]:-}" ] || IMG_IDS+=("$id")
      IMG_TAGS[$id]+="$repo:$tag "
    else
      IMG_FOREIGN[$id]+="$repo:$tag "
    fi
  done <<< "$out"

  # 모든 상태(실행·중지·생성만 된)의 컨테이너가 참조하는 image ID
  out="$(docker ps -aq --no-trunc)" || fail "docker ps 실패"
  if [ -n "$out" ]; then
    mapfile -t carr <<< "$out"
    out="$(docker inspect -f '{{.Image}}|{{.Name}}|{{.State.Status}}' "${carr[@]}")" || fail "컨테이너 inspect 실패"
    while IFS='|' read -r cimg cname cstate; do
      [ -n "$cimg" ] || continue
      IMG_CONT[$(full_id "$cimg")]+="${cname#/}($cstate) "
    done <<< "$out"
  fi

  # 현재 cuelo 서비스 컨테이너의 image ID
  out="$(docker ps -aq --no-trunc --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$SERVICE")" || fail "docker ps 실패"
  if [ -z "$out" ]; then
    problem "현재 $PROJECT/$SERVICE 컨테이너가 없다(현재 이미지 보호 기준을 세울 수 없다)"
  else
    mapfile -t carr <<< "$out"
    out="$(docker inspect -f '{{.Image}}' "${carr[@]}")" || fail "현재 컨테이너 inspect 실패"
    while read -r cimg; do
      if [ -n "$cimg" ]; then CUR_IDS+="$(full_id "$cimg") "; fi
    done <<< "$out"
  fi

  for id in ${IMG_IDS[@]+"${IMG_IDS[@]}"}; do
    if info="$(docker image inspect -f '{{.Created}}|{{.Size}}' "$id")"; then
      IMG_CREATED[$id]="${info%%|*}"; IMG_SIZE[$id]="${info#*|}"
      IMG_EPOCH[$id]="$(date -u -d "${IMG_CREATED[$id]}" +%s 2>/dev/null || true)"
    else
      problem "이미지 $id 를 inspect할 수 없다"
    fi
  done

  # 직전 복구본 = 형식이 <run>-<attempt>인 rollback 태그 중 run 번호(그다음 attempt)가 가장 큰 것
  for id in ${IMG_IDS[@]+"${IMG_IDS[@]}"}; do
    for ref in ${IMG_TAGS[$id]}; do
      case "$ref" in
        "$ROLLBACK_REPO":*) if [[ "${ref#*:}" =~ ^([0-9]+)-([0-9]+)$ ]]; then ranked+="${BASH_REMATCH[1]} ${BASH_REMATCH[2]} $ref $id"$'\n'; fi ;;
      esac
    done
  done
  if [ -n "$ranked" ]; then
    read -r _ _ ROLLBACK_TAG ROLLBACK_ID <<< "$(printf '%s' "$ranked" | sort -k1,1n -k2,2n | tail -n 1)"
    ROLLBACK_EPOCH="${IMG_EPOCH[$ROLLBACK_ID]:-}"
    if [ -z "$ROLLBACK_EPOCH" ]; then
      problem "직전 복구 이미지 $ROLLBACK_TAG 의 생성 시각을 읽을 수 없다(보호 기준을 세울 수 없다)"
      ROLLBACK_ID=""; ROLLBACK_TAG=""
    fi
  else
    problem "$ROLLBACK_REPO:<run>-<attempt> 형식의 복구 태그가 없다(직전 복구본 보호 기준을 세울 수 없다)"
  fi

  # 다음 compose up이 쓸 override 이미지
  if [ ! -d "$ROOT/deploy" ] || [ ! -x "$ROOT/deploy" ]; then
    problem "$ROOT/deploy 에 접근할 수 없다(override 이미지 보호 기준을 세울 수 없다)"
  elif [ -e "$OVERRIDE_FILE" ]; then
    if [ -r "$OVERRIDE_FILE" ]; then OVERRIDE_REF="$(sed -n 's/^[[:space:]]*image:[[:space:]]*//p' "$OVERRIDE_FILE" | head -n 1)" || OVERRIDE_REF=""; fi
    if ! [[ "$OVERRIDE_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ ]]; then
      problem "$OVERRIDE_FILE 에서 image를 읽을 수 없다(override 이미지 보호 기준을 세울 수 없다)"
    elif info="$(docker image inspect -f '{{.Id}}' "$OVERRIDE_REF" 2>/dev/null)"; then
      OVERRIDE_ID="$(full_id "$info")"
    else
      problem "override가 지정한 image $OVERRIDE_REF 를 찾을 수 없다"
    fi
  fi

  for id in ${IMG_IDS[@]+"${IMG_IDS[@]}"}; do
    why_list=()
    case "$CUR_IDS" in *" $id "*) why_list+=("current") ;; esac
    if [ "$CUR_IDS" = " " ]; then why_list+=("no-current-baseline"); fi
    if [ -z "$ROLLBACK_ID" ]; then
      why_list+=("no-rollback-baseline")
    elif [ "$id" = "$ROLLBACK_ID" ]; then
      why_list+=("rollback-latest:$ROLLBACK_TAG")
    fi
    for ref in ${IMG_TAGS[$id]}; do
      case "$ref" in
        "$ROLLBACK_REPO":*) [[ "${ref#*:}" =~ ^[0-9]+-[0-9]+$ ]] || why_list+=("rollback-unrecognized:$ref") ;;
      esac
    done
    if [ -n "$OVERRIDE_ID" ] && [ "$id" = "$OVERRIDE_ID" ]; then why_list+=("override-ref:$OVERRIDE_REF"); fi
    if [ -n "${IMG_CONT[$id]:-}" ]; then
      why="${IMG_CONT[$id]% }"; why_list+=("container-ref:${why// /+}")
    fi
    if [ -n "${IMG_FOREIGN[$id]:-}" ]; then
      why="${IMG_FOREIGN[$id]% }"; why_list+=("foreign-tag:${why// /+}")
    fi
    epoch="${IMG_EPOCH[$id]:-}"
    if [ -z "$epoch" ]; then
      why_list+=("created-unknown")
    elif [ -n "$ROLLBACK_EPOCH" ] && [ "$id" != "$ROLLBACK_ID" ] && [ "$epoch" -ge "$ROLLBACK_EPOCH" ]; then
      why_list+=("not-older-than-rollback")
    fi
    if [ "${#why_list[@]}" = 0 ]; then
      IMG_VERDICT[$id]="CANDIDATE"; IMG_WHY[$id]="unreferenced,older-than-rollback"
    else
      IMG_VERDICT[$id]="PROTECT"; IMG_WHY[$id]="$(IFS=,; printf '%s' "${why_list[*]}")"
    fi
  done
}

print_inventory() {
  local id ref sorted sizemb base_refs=""
  log "CUELO 이미지 inventory: 범위 = cuelo-cloud:* · $ROLLBACK_REPO:* · cuelo:cloud-*, ${#IMG_IDS[@]}개 ID (다른 repo·dangling 이미지는 나열·삭제 대상이 아니다)"
  log "size는 이미지별 값이며 공유 레이어를 중복 포함하므로 합산해도 회수량이 아니다. 실제 회수량은 삭제 전후 docker system df와 저장소 여유로만 본다."
  base_refs="$(compose config --images 2>/dev/null)" || { base_refs=""; log "compose.yaml 기본 image 참조를 읽을 수 없다(정보용)"; }
  sorted="$(for id in ${IMG_IDS[@]+"${IMG_IDS[@]}"}; do printf '%s %s\n' "${IMG_EPOCH[$id]:-0}" "$id"; done | sort -rn | cut -d' ' -f2)"
  while read -r id; do
    [ -n "$id" ] || continue
    sizemb="?"
    case "${IMG_SIZE[$id]:-}" in ''|*[!0-9]*) ;; *) sizemb="$(( ${IMG_SIZE[$id]} / 1048576 ))MB" ;; esac
    log "IMAGE $id ${IMG_VERDICT[$id]} reasons=${IMG_WHY[$id]} created=${IMG_CREATED[$id]:-?} size=$sizemb"
    printf '    tags: %s\n' "$(tags_of "$id")"
    if [ -n "${IMG_CONT[$id]:-}" ]; then printf '    containers: %s\n' "${IMG_CONT[$id]% }"; fi
    for ref in ${IMG_TAGS[$id]}; do
      case $'\n'"$base_refs"$'\n' in
        *$'\n'"$ref"$'\n'*) printf '    note: compose.yaml 기본 image가 %s 를 가리킨다. 지우면 override 없이 compose만 쓰는 수동 실행은 이 이름을 찾지 못한다.\n' "$ref" ;;
      esac
    done
  done <<< "$sorted"
  if [ -n "$OVERRIDE_REF" ]; then log "override image: $OVERRIDE_REF → ${OVERRIDE_ID:-?}"; fi
  log "현재 컨테이너 image:${CUR_IDS% }"
  log "직전 복구본: ${ROLLBACK_TAG:-없음} → ${ROLLBACK_ID:-?}"
}

# Docker 저장소 상태. 삭제 전후 값을 그대로 보여 주기 위해 계산하지 않는다(여유 KB는 FREE_KB로 남긴다).
storage_report() {
  local label="$1" root_dir df_row=""
  df_row="$(docker system df --format '{{.Type}}|{{.TotalCount}}|{{.Active}}|{{.Size}}|{{.Reclaimable}}' 2>/dev/null | awk -F'|' '$1=="Images"')" || df_row=""
  log "[$label] docker system df Images(전체 이미지 기준, CUELO 한정 아님; Type|Total|Active|Size|Reclaimable): ${df_row:-확인 불가}"
  FREE_KB=""
  root_dir="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)"
  if [ -n "$root_dir" ]; then FREE_KB="$(df -Pk "$root_dir" 2>/dev/null | awk 'NR==2{print $4}')" || FREE_KB=""; fi
  if [ -n "$FREE_KB" ]; then log "[$label] Docker 저장소 여유 ${FREE_KB}KB ($root_dir)"; else log "[$label] Docker 저장소 여유 확인 불가(권한)"; fi
}

do_images() {
  log "읽기 전용 이미지 inventory. 아무것도 바꾸지 않는다."
  snapshot_images
  print_inventory
  storage_report "현재"
  [ "$PROBLEMS" = 0 ] || fail "inventory에서 문제 $PROBLEMS 건. 보호 기준을 세우지 못했으므로 cleanup-images는 모든 후보를 거부한다."
  log "inventory 완료(읽기 전용). 삭제하려면 mode=cleanup-images에 CANDIDATE의 전체 ID를 넣고, apply_cleanup=false(dry-run)로 먼저 확인한다."
}

# 요청 ID가 지금 삭제 가능하면 0, 아니면 이유를 출력하고 1.
removable_check() {
  local id="$1"
  if [ -z "${IMG_TAGS[$id]:-}" ]; then echo "CUELO 범위의 이미지가 아니거나 이미 없다"; return 1; fi
  if [ "${IMG_VERDICT[$id]:-}" != CANDIDATE ]; then echo "PROTECT reasons=${IMG_WHY[$id]:-?}"; return 1; fi
  return 0
}

parse_cleanup_ids() {
  local raw token seen=" "
  local -a toks=()
  raw="$(printf '%s' "${CLEANUP_IMAGE_IDS:-}" | tr ',\r\n\t' '    ')"
  read -r -a toks <<< "$raw" || true
  [ "${#toks[@]}" -ge 1 ] || fail "cleanup_image_ids가 비어 있다. 삭제할 전체 image ID(sha256:64hex)를 지정해야 한다"
  [ "${#toks[@]}" -le "$MAX_CLEANUP_IDS" ] || fail "한 번에 ${MAX_CLEANUP_IDS}개까지만 지정할 수 있다"
  REQ_IDS=()
  for token in "${toks[@]}"; do
    [[ "$token" =~ $IMG_ID_RE ]] || fail "cleanup_image_ids는 전체 image ID(sha256:64hex)만 허용한다(짧은 ID·태그·와일드카드 불가)"
    case "$seen" in *" $token "*) continue ;; esac
    seen+="$token "; REQ_IDS+=("$token")
  done
}

# 삭제 직전 재확인 뒤 한 이미지의 CUELO 태그를 모두 지운다. 하나라도 어긋나면 아무것도 지우지 않고 1을 돌려준다.
remove_one() {
  local id="$1" before="$PROBLEMS" why tag out tags
  snapshot_images
  if [ "$PROBLEMS" != "$before" ]; then log "RM-STOP $id 삭제 직전 재조회에서 보호 기준을 세우지 못했다"; return 1; fi
  if ! why="$(removable_check "$id")"; then log "RM-STOP $id 삭제 직전 재판정 실패: $why"; return 1; fi
  tags="$(tags_of "$id")"
  if [ "$tags" != "${PLAN_TAGS[$id]}" ]; then log "RM-STOP $id 태그가 계획과 달라졌다(계획: ${PLAN_TAGS[$id]} / 지금: $tags)"; return 1; fi
  log "RM-RUN $id 재확인 통과(참조 컨테이너 없음, 보호 ID 아님, 태그 계획과 동일)"
  for tag in $tags; do
    if out="$(docker image rm "$tag" 2>&1)"; then
      printf '%s\n' "$out" | sed 's/^/    docker: /'
    else
      printf '%s\n' "$out" | head -n 3 | sed 's/^/    docker: /'
      log "RM-FAIL $id 태그 $tag 삭제 실패. 이후 삭제를 중단한다"
      return 1
    fi
  done
  if docker image inspect -f '{{.Id}}' "$id" >/dev/null 2>&1; then log "RM-FAIL $id 태그는 모두 지웠지만 이미지가 남아 있다"; return 1; fi
  log "RM-OK $id 삭제됨(태그 $tags)"
}

do_cleanup_images() {
  local apply="${APPLY_CLEANUP:-false}" id why before_kb="" failed_id="" nfail=0
  local -a ok=() left=()
  case "$apply" in true|false) ;; *) fail "APPLY_CLEANUP은 true 또는 false여야 한다" ;; esac
  parse_cleanup_ids
  if [ "$apply" = true ]; then log "요청 ID ${#REQ_IDS[@]}개, apply=true: 재판정을 통과한 대상만 실제로 삭제한다"; else log "요청 ID ${#REQ_IDS[@]}개, apply=false: dry-run, 아무것도 삭제하지 않는다"; fi
  snapshot_images
  print_inventory
  [ "$PROBLEMS" = 0 ] || fail "inventory에서 문제 $PROBLEMS 건. 보호 기준을 세우지 못했으므로 아무것도 삭제하지 않았다."
  for id in "${REQ_IDS[@]}"; do
    if why="$(removable_check "$id")"; then
      PLAN_TAGS[$id]="$(tags_of "$id")"
      log "PLAN $id: 태그 전체 ${PLAN_TAGS[$id]} 를 각각 docker image rm (이미지 ID 단독 rm·-f·prune 없음)"
    else
      problem "요청 $id 는 삭제할 수 없다: $why"
    fi
  done
  [ "$PROBLEMS" = 0 ] || fail "요청한 ID 중 삭제할 수 없는 것이 있다. 아무것도 삭제하지 않았다."
  if [ "$apply" != true ]; then
    log "DRY-RUN 완료: 아무것도 삭제하지 않았다. 같은 입력에 apply_cleanup=true로 다시 실행해야 삭제하며, 삭제 직전에 대상마다 다시 판정한다."
    return
  fi

  storage_report "삭제 전"; before_kb="$FREE_KB"
  for id in "${REQ_IDS[@]}"; do
    if [ -n "$failed_id" ]; then left+=("$id"); continue; fi
    if remove_one "$id"; then ok+=("$id"); else failed_id="$id"; fi
  done
  storage_report "삭제 후"
  if [ -n "$before_kb" ] && [ -n "$FREE_KB" ]; then
    log "Docker 저장소 여유 변화: $((FREE_KB - before_kb))KB (df 측정값. 같은 시간의 다른 쓰기가 섞일 수 있고 CUELO 이미지 size 합이 아니다)"
  fi
  if [ -n "$failed_id" ]; then nfail=1; fi
  log "삭제 receipt: 삭제 완료 ${#ok[@]}개, 실패·중단 ${nfail}개, 미시도 ${#left[@]}개"
  for id in ${ok[@]+"${ok[@]}"}; do log "RECEIPT removed $id tags=${PLAN_TAGS[$id]}"; done
  if [ -n "$failed_id" ]; then log "RECEIPT failed $failed_id tags=${PLAN_TAGS[$failed_id]} (남은 상태는 위 RM-* 줄과 docker image ls로 확인)"; fi
  for id in ${left[@]+"${left[@]}"}; do log "RECEIPT not-attempted $id tags=${PLAN_TAGS[$id]}"; done
  if [ -n "$failed_id" ]; then fail "이미지 삭제가 중간에 멈췄다. 위 receipt 이후 상태를 확인하고 다시 판단한다. 자동 재시도·강제 삭제는 하지 않는다."; fi
  log "완료: 요청한 ${#ok[@]}개 이미지를 삭제했다."
}

case "$MODE" in
  inspect) do_inspect ;;
  deploy) do_deploy ;;
  images) do_images ;;
  cleanup-images) do_cleanup_images ;;
  verify-image)
    verify_image
    [ "$PROBLEMS" = 0 ] || fail "이미지 검사 실패 $PROBLEMS 건"
    ;;
  *) fail "알 수 없는 모드: $MODE" ;;
esac
