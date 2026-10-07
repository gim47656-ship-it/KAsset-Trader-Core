#!/usr/bin/env bash
# usage: run-mock.sh <host.sh 경로> <fake-docker.sh 경로> "SCEN MODE" ...   (각 시나리오 raw 출력을 stdout에 낸다)
HOST_SH="$1"; FAKE="$2"; shift 2
BIN=$(mktemp -d); cp "$FAKE" "$BIN/docker"; chmod +x "$BIN/docker"
for spec in "$@"; do
  set -- $spec; export SCEN="$1" MODE="$2"
  D=$(mktemp -d); export FAKE_STATE="$D/state" CUELO_ROOT="$D/opt"; mkdir -p "$FAKE_STATE" "$CUELO_ROOT"
  echo "services:" > "$CUELO_ROOT/compose.yaml"; echo "SECRET=x" > "$CUELO_ROOT/.env"; touch "$D/img.tar.gz"
  export CUELO_SHA=772554adc16ea7993d94e13c44e43cfa45c028f6 EXPECTED_VERSION=0.9.9 EXPECTED_CORE=18.7.0 \
    IMAGE=cuelo-cloud:772554adc16e IMAGE_ID=sha256:new IMAGE_TAR="$D/img.tar.gz" RUN_TAG=123-1 DRAIN_TIMEOUT_SEC=4 HEALTH_TIMEOUT_SEC=6
  [ "$SCEN" = idmismatch ] && export IMAGE_ID=sha256:other
  echo "################ SCEN=$SCEN MODE=$MODE"
  PATH="$BIN:$PATH" bash "$HOST_SH" "$MODE" > "$D/out.log" 2>&1; rc=$?
  sed "s#$D#<tmp>#g" "$D/out.log"
  echo "---- exit=$rc"
  echo "---- docker calls (<tmp> 치환):"; sed "s#$D#<tmp>#g" "$FAKE_STATE/calls.log"
  echo "---- summary: up=$(grep -c ' up ' "$FAKE_STATE/calls.log") tag=$(grep -c . "$FAKE_STATE/tags.log" 2>/dev/null || echo 0) drain=$(grep -c . "$FAKE_STATE/drain.log" 2>/dev/null || echo 0) forbidden_nonexec=$(grep -vE '^docker exec' "$FAKE_STATE/calls.log" | grep -cE 'docker (compose .* (down|rm|stop|kill|restart)|rm |rmi|volume|system|builder|image prune)| -v ' || true) secret_leaked=$(grep -c 'secretvalue\|SECRET=' "$D/out.log" || true)"
  echo "---- files in opt: $(ls -A "$CUELO_ROOT" | tr '\n' ' ')"
  rm -rf "$D"
done
rm -rf "$BIN"
