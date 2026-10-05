#!/bin/bash
# 검증 전용: kasset-test-db 안 이 Maker 고유 DB(longterm_*_5e7c1a3d)와 /tmp 고유 디렉터리만 사용한다.
H=5e7c1a3d
S=/tmp/kasset-longterm-shadow-20261005-$H
L=/tmp/kasset-longterm-shadow-logs-$H
COMMIT=b22c1eac0c02197306281c229e7968d0c2b6760b
IMG=kasset-trader-core:$COMMIT
V=kasset-pytest-deps-04d62828-swing
run_py() {
  db="$1"; shift
  docker run --rm --cpus=1.0 --network container:kasset-test-db \
    --mount type=bind,src=$S,dst=/work,readonly \
    --mount type=volume,src=$V,dst=/test-deps,readonly \
    --env-file $S/deploy/kasset/env.example \
    -e DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/$db \
    -e SECRET_KEY=Test_Secret_Key_12345_Test_Secret_Key_12345 \
    -e PYTHONPATH=/work:/test-deps -e PYTHONDONTWRITEBYTECODE=1 \
    -w /work --entrypoint /app/.venv/bin/python $IMG "$@"
}
psql_db() { docker exec -i kasset-test-db psql -v ON_ERROR_STOP=1 -U postgres -d "$1" "${@:2}"; }
FILES_FILE=$S/.changed_files

case "$1" in
static)
  docker run --rm --cpus=1.0 --network none \
    --mount type=bind,src=$S,dst=/work,readonly \
    --mount type=volume,src=$V,dst=/test-deps,readonly \
    -e PYTHONPATH=/work:/test-deps -w /work --entrypoint sh $IMG -c \
    "F=\$(cat .changed_files | grep '\.py\$'); /test-deps/bin/ruff check --no-cache \$F; echo RUFF_CHECK_EXIT=\$?; /test-deps/bin/ruff format --no-cache --check \$F; echo RUFF_FORMAT_EXIT=\$?"
  ;;
format-diff)
  docker run --rm --cpus=1.0 --network none \
    --mount type=bind,src=$S,dst=/work,readonly \
    --mount type=volume,src=$V,dst=/test-deps,readonly \
    -e PYTHONPATH=/work:/test-deps -w /work --entrypoint sh $IMG -c \
    "F=\$(cat .changed_files | grep '\.py\$'); /test-deps/bin/ruff format --no-cache --diff \$F; /test-deps/bin/ruff check --no-cache --fix --diff \$F"
  ;;
ty)
  docker run --rm --cpus=1.0 --network none \
    --mount type=bind,src=$S,dst=/work,readonly \
    --mount type=volume,src=$V,dst=/test-deps,readonly \
    -e PYTHONPATH=/work:/test-deps -w /work --entrypoint sh $IMG -c \
    "/test-deps/bin/ty check --error-on-warning app/; echo TY_EXIT=\$?"
  ;;
pytest)
  shift
  docker run --rm --name kasset-longterm-pytest-$H \
    --cpus=1.0 --network container:kasset-test-db \
    --mount type=bind,src=$S,dst=/work,readonly \
    --mount type=volume,src=$V,dst=/test-deps,readonly \
    -e AUTO_TRADER_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/test_db' \
    -e PYTHONPATH=/work:/test-deps -e PYTHONDONTWRITEBYTECODE=1 \
    -w /work --entrypoint /app/.venv/bin/python $IMG \
    -m pytest -q --tb=short -p no:cacheprovider "$@"
  echo PYTEST_EXIT=$?
  ;;
esac
