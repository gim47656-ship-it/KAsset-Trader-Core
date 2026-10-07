#!/usr/bin/env bash
# throwaway fake docker: 모든 호출을 $FAKE_STATE/calls.log에 남기고 $SCEN에 따라 cuelo-cloud 상태를 흉내 낸다.
S="${FAKE_STATE:?}"; ROOT="${CUELO_ROOT:?}"
echo "docker $*" >> "$S/calls.log"
swapped() { [ -e "$S/swapped" ]; }
cid() { swapped && echo def456 || echo abc123; }
case "$1" in
  version) echo "26.1.0"; exit 0;;
  info) echo "$S"; exit 0;;
  ps) [ "$SCEN" = multi ] && printf 'abc123\nxyz999\n' || cid; exit 0;;
  load) exit 0;;
  tag) echo "tag $*" >> "$S/tags.log"; exit 0;;
  image) echo "sha256:new"; exit 0;;
  inspect)
    tpl="$3"
    case "$tpl" in
      *range\ .Mounts*)
        echo "bind $ROOT/home /home/omp rw=true"; echo "bind $ROOT/home/.omp /home/omp/.omp rw=true"; echo "bind $ROOT/workspace /workspace rw=true"
        [ "$SCEN" = badbind ] && echo "volume /var/lib/docker/volumes/anon /home/omp/.cache rw=true"; exit 0;;
      *working_dir*) swapped && echo "$ROOT/compose.yaml,$ROOT/compose.image.yaml|$ROOT" || echo "$ROOT/compose.yaml|$ROOT"; exit 0;;
      *config_files*) swapped && echo "$ROOT/compose.yaml,$ROOT/compose.image.yaml" || echo "$ROOT/compose.yaml"; exit 0;;
      *.Name*) echo "/cuelo-cloud-cuelo-1 state=running health=healthy started=2026-10-07T00:00:00Z"; exit 0;;
      *State.Health*) echo healthy; exit 0;;
      '{{.Config.Image}}') swapped && echo "cuelo-cloud:772554adc16e" || echo "cuelo:local"; exit 0;;
      '{{.Image}}') swapped && echo sha256:new || echo sha256:old; exit 0;;
    esac; echo "unhandled inspect $tpl" >&2; exit 9;;
  exec)
    shift; while [ "${1:0:1}" = - ]; do case "$1" in -e) shift 2;; *) shift;; esac; done
    shift  # container id
    case "$1 $2" in
      "id -u"|"id -g") echo 1000; exit 0;;
      "sh -c") exit 0;;
      "sh -s") cat >/dev/null; echo "config.yml=ok agent.db=ok APPEND_SYSTEM.md=ok modelRoles=abc123 lines=7"; exit 0;;
      "bun /app/install.mjs")
        if [ "$SCEN" = healthfail ] && swapped; then echo "FAIL web"; exit 1; fi
        for n in web usage btw subagent; do echo "OK   $n"; done; exit 0;;
      "bun -e")
        js="$3"
        case "$js" in
          *request.json*) echo request >> "$S/drain.log"; exit 0;;
          *ack.json*) [ "$SCEN" = noack ] && exit 3; [ "$SCEN" = unsettled ] && { echo "aborted=1 failed=0 unsettled=1"; exit 0; }; echo "aborted=1 failed=0 unsettled=0"; exit 0;;
          *pending-resume*) echo "pending=1 failures=0"; exit 0;;
          *cueloBuild*)
            if swapped; then
              [ "$SCEN" = badversion ] && { echo "cuelo 0.9.6 core 18.6.1"; echo "version 0.9.6; cueloBuild.coreVersion 18.6.1" >&2; exit 1; }
              echo "cuelo 0.9.9 core 18.7.0"
            else echo "cuelo 0.9.6 core 18.6.1"; fi; exit 0;;
        esac;;
    esac; echo "unhandled exec $*" >&2; exit 9;;
  run)
    shift; while [ "$1" != --entrypoint ]; do shift; done; shift; ep="$1"
    case "$ep" in id) echo 1000;; bun) echo "cuelo 0.9.9 core 18.7.0";; node) echo "CHECK OK";; esac; exit 0;;
  compose)
    shift; files=0; while [ $# -gt 0 ]; do case "$1" in -p|--project-directory) shift 2;; -f) files=$((files+1)); last="$2"; shift 2;; version) echo 2.30.0; exit 0;; config) img=cuelo:local; [ "$files" = 2 ] && img="$(sed -n 's/^ *image: //p' "$last")"; printf 'name: cuelo-cloud\nservices:\n  cuelo:\n    image: %s\n    environment:\n      X: secretvalue\n' "$img"; exit 0;; up) touch "$S/swapped"; echo up; exit 0;; *) shift;; esac; done;;
esac
echo "unhandled: $*" >&2; exit 9
