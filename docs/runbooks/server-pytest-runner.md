# Server pytest runner

Run Linux-only and KAsset tests in a disposable container on the production
host. The container uses the deployed application image for its interpreter and
`/app/.venv`, mounts the checkout under test read-only, installs the test-only
dependency group into a Docker volume, and has one CPU.

It never touches the production database. It targets the separate
`kasset-test-db` container and lets the harness create its own run-owned
database there.

Do not run this procedure on KRX business days from 08:50 through 16:20 KST.
The test container can saturate its one-CPU quota while the live API, worker,
scheduler, MCP, ai-MCP, PostgreSQL, Redis, and Caddy containers share a 2-vCPU
host. During market hours that contention can delay paper automation and
market-event jobs.

## Database safety contract

The only permitted PostgreSQL instance is the `kasset-test-db` container. Never
point this procedure at `kasset-trader-db-1`, which holds the production
`kasset` database.

Two independent layers keep production out of reach:

1. **Different instance.** The runner joins `--network container:kasset-test-db`,
   so `127.0.0.1:5432` inside the container resolves to the test PostgreSQL
   process. The production database is not reachable on that loopback address.
2. **Run-owned database.** `AUTO_TRADER_TEST_DATABASE_URL` must name database
   `test_db` — `tests/_run_owned_database.py` rejects anything else. Leave
   `AUTO_TRADER_PYTEST_USE_SHARED_DB` unset so the harness creates
   `test_db_pytest_<uid>_<worker>` inside `kasset-test-db` and drops it at the
   end.

Do not pass `--env-file /opt/kasset-trader-core/.env.kasset`; that would inject
live provider and broker credentials into the test process.

### Do not use the shared `test_db` path

An earlier revision of this runbook used `kasset-trader-db-1` with
`AUTO_TRADER_PYTEST_USE_SHARED_DB=1`. That path is broken: the shared `test_db`
on the production instance carries schema from an older revision, the bootstrap
reports `applied=0` because the database is not empty, and fixtures then fail on
column mismatches. On 2026-09-23 the same `tests/extensions/kasset/api`
selection produced 12 failed / 28 errors that way and 489 passed through the
run-owned path below.

## 1. Resolve the deployed inputs

Every tag below is derived, not hardcoded. The deployed image tag is the full
commit SHA that `deploy.sh` built.

```bash
ssh kasset-server   # root@100.73.186.78
set -eu

export KASSET_TEST_COMMIT="$(git -C /opt/kasset-trader-core rev-parse HEAD)"
export KASSET_TEST_IMAGE="kasset-trader-core:${KASSET_TEST_COMMIT}"
export KASSET_TEST_DEPS_VOLUME='kasset-pytest-deps-4e6329d1'

docker image inspect "$KASSET_TEST_IMAGE" >/dev/null
docker inspect kasset-test-db >/dev/null
[ "$(docker inspect -f '{{.State.Status}}' kasset-test-db)" = 'running' ]
```

The application image ships no `tests/` directory, so section 3 mounts a
checkout instead.

## 2. Install the locked test-only dependencies

`/app/.venv` contains `pytest` itself but omits the rest of the `test`
dependency group. Install the versions this checkout's `uv.lock` records into a
Docker volume. The application image stays unchanged.

The volume name carries the image SHA it was first built against, but its
contract is the `uv.lock` pin set, not that image. Reuse it as long as the
versions still match; create a new volume when a lock bump changes them.

```bash
# Current pins (verified against uv.lock on 2026-09-23)
docker volume inspect "$KASSET_TEST_DEPS_VOLUME" >/dev/null 2>&1 || \
  docker volume create "$KASSET_TEST_DEPS_VOLUME"

docker run --rm --name kasset-pytest-deps-init \
  --user 0:0 \
  --cpus=1.0 \
  --mount type=volume,src="$KASSET_TEST_DEPS_VOLUME",dst=/test-deps \
  --entrypoint /usr/local/bin/pip \
  "$KASSET_TEST_IMAGE" \
  install --disable-pip-version-check --no-cache-dir --upgrade \
  --target /test-deps \
  pytest==9.1.1 \
  pytest-asyncio==1.3.0 \
  pytest-cov==7.1.0 \
  pytest-mock==3.15.1 \
  pytest-xdist==3.8.0 \
  fakeredis==2.34.1 \
  aiosqlite==0.22.1 \
  pytest-split==0.11.0
```

`--user 0:0` is required because Docker creates the named volume with root
ownership. Test containers mount the completed volume read-only.

Confirm the volume still matches the lock before trusting an existing one:

```bash
awk '/^name = "/{n=$3; gsub(/"/,"",n)} /^version = "/{v=$3; gsub(/"/,"",v);
  if (n ~ /^(pytest|pytest-asyncio|pytest-cov|pytest-mock|pytest-xdist|fakeredis|aiosqlite|pytest-split)$/)
  print n"=="v}' /opt/kasset-trader-core/uv.lock | sort

docker run --rm \
  --mount type=volume,src="$KASSET_TEST_DEPS_VOLUME",dst=/test-deps,readonly \
  --entrypoint sh "$KASSET_TEST_IMAGE" -c \
  'ls /test-deps | grep dist-info | sort'
```

## 3. Prepare the checkout under test

Clone the deployed checkout, detach at the deployed commit, then overlay only
the files you are validating. Never mount `/opt/kasset-trader-core` itself into
a test container.

```bash
export KASSET_TEST_SRC=/tmp/kasset-pytest-$(date +%Y%m%d-%H%M%S)

git clone -q /opt/kasset-trader-core "$KASSET_TEST_SRC"
git -C "$KASSET_TEST_SRC" checkout -q "$KASSET_TEST_COMMIT"

# then copy in the changed files, e.g.
#   ssh kasset-server "cat > $KASSET_TEST_SRC/pnl.patch" < local.patch
#   git -C "$KASSET_TEST_SRC" apply pnl.patch
# or scp/tar the individual files.
```

Remove the directory when finished.

## 4. Define the disposable runner

Keep this serial. Do not add `-n auto`, `-n 2`, or another xdist option: the
2-vCPU production host requires one pytest process.

`PYTHONPATH` puts the checkout first, so `import app` resolves to the code under
test rather than the image's copy. Verify that once per session with
`python -c 'import app; print(app.__file__)'` if in doubt.

```bash
run_server_pytest() {
  container_name="$1"
  shift
  docker run --rm --name "$container_name" \
    --cpus=1.0 \
    --network container:kasset-test-db \
    --mount type=bind,src="$KASSET_TEST_SRC",dst=/work,readonly \
    --mount type=volume,src="$KASSET_TEST_DEPS_VOLUME",dst=/test-deps,readonly \
    -e AUTO_TRADER_TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/test_db' \
    -e PYTHONPATH=/work:/test-deps \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -w /work \
    --entrypoint /app/.venv/bin/pytest \
    "$KASSET_TEST_IMAGE" \
    -q --tb=short -p no:cacheprovider "$@"
}
```

`-p no:cacheprovider` is required: `/work` is read-only and pytest would
otherwise fail creating `.pytest_cache`.

## 5. Run a slice and observe load

```bash
run_server_pytest kasset-pytest-api tests/extensions/kasset/api &
KASSET_PYTEST_PID=$!
sleep 3
uptime
docker stats --no-stream \
  --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' \
  kasset-pytest-api
wait "$KASSET_PYTEST_PID"
unset KASSET_PYTEST_PID
```

Save the final pytest summary and the `uptime`/`docker stats` lines with the
test report. A CPU reading near 100% is expected because `--cpus=1.0` caps the
container at one core.

Every run prints the guard lines:

```text
ROB-1296 external HTTP boundary: 0 blocked requests
ROB-1880 socket guard: active=True blocked_attempts=0 workers=0
```

A run that actually touches the database adds a bootstrap line:

```text
test schema bootstrap: databases=1 applied=1 schema_seconds=2.17 database_seconds=0.19
```

`applied=1` means the harness built the schema in a fresh run-owned database.
`applied=0` means it found an existing database and skipped DDL — stop and check
that `AUTO_TRADER_PYTEST_USE_SHARED_DB` is unset.

Selections that never open a session print no bootstrap line at all; that is
normal, not a skipped setup.

## 6. Run a Linux-only B0-X lock file

```bash
run_server_pytest \
  kasset-pytest-b0x \
  tests/scripts/b0x/test_envelope_and_locks.py
```

This file imports `scripts.b0x.ledger`, which imports `fcntl` and uses
`fcntl.flock` for the writer lock. A successful summary proves Linux collected
and executed the file rather than applying the Windows collection exclusion.

## 7. Static checks on the same checkout

`ruff` needs `--no-cache` because `/work` is read-only.

```bash
docker run --rm \
  --mount type=bind,src="$KASSET_TEST_SRC",dst=/work,readonly \
  -w /work --entrypoint sh "$KASSET_TEST_IMAGE" -c \
  '/app/.venv/bin/ruff check --no-cache <files>;
   /app/.venv/bin/ruff format --no-cache --check <files>;
   /app/.venv/bin/ty check --error-on-warning <files>'
```

## 8. Prove production isolation and service health

Run these immediately after the tests. The first command is the production DB
misrouting check; it must print the deployed schema's table count, which was
`109` on 2026-09-23. A drop means test DDL reached production — stop and
investigate.

```bash
docker exec kasset-trader-db-1 \
  psql -U kasset -d kasset -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"

docker ps --filter 'name=kasset-trader-' \
  --format 'table {{.Names}}\t{{.Status}}'

rm -rf "$KASSET_TEST_SRC"
```

Expect eight running containers: `db`, `redis`, `api`, `worker`, `scheduler`,
`mcp`, `ai-mcp`, and `caddy`. The test procedure must not stop, restart, or exec
test code inside any of those containers.

## Adding a test file

CI enforces that every collected test file appears in exactly one
`ci_shards/shard-N.txt`. A new file makes the `taskiq-smoke` job fail with
`shard manifest exact-cover check failed`. Add the path to exactly one shard at
its `LC_ALL=C` sorted position:

```bash
LC_ALL=C sort -c ci_shards/shard-4.txt          # must stay sorted
cat ci_shards/shard-*.txt | sort | uniq -d      # must print nothing
```

Do not run `file_shard_plan generate` for an ordinary add or rename; it
recomputes and rewrites every shard and belongs to the duration-refresh
workflow.

## Windows collection exclusions

`tests/conftest.py::pytest_ignore_collect` skips these paths whenever
`sys.platform == "win32"`:

- all files under `tests/scripts/b0x/`
- `tests/research/toss_phase2/test_load.py`
- `tests/scripts/test_mock_session_mcp.py`
- `tests/scripts/test_r4_p0_manifest_cli.py`
- `tests/scripts/test_r4_p0_readiness.py`
- `tests/services/mock_integration/test_kiwoom_coordination_adapter.py`
- `tests/services/test_krb1_p0_journal.py`
- `tests/services/test_market_events_dart_helpers.py`
- `tests/test_binance_r4_p0_backfill.py`
- `tests/test_binance_r4_p0_collector.py`
- `tests/test_binance_r4_p0_hardening.py`
- `tests/test_binance_r4_p0_watchdog.py`
- `tests/test_services_dart.py`

The B0-X code uses POSIX `fcntl`/`flock` directly. Other listed tests exercise
POSIX process groups, signals, file locks, or Linux service behavior. The hook
excludes them before import on Windows, so a Windows run cannot provide
collection or execution evidence for these paths. Run them through this Linux
container path instead.
