# Server pytest runner

Use a disposable container on the production host to run Linux-only and KAsset
API tests. The container uses the deployed application image, mounts `tests/`
from the matching checkout, shares the database container's Compose network
namespace, and has one CPU. It does not inherit `.env.kasset` or the API
container environment.

Do not run this procedure on KRX business days from 08:50 through 16:20 KST.
The test container can saturate its one-CPU quota while the live API, worker,
scheduler, MCP, PostgreSQL, Redis, and Caddy containers share a 2-vCPU host.
During market hours that contention can delay paper automation and market-event
jobs.

## Database safety contract

The only permitted target is PostgreSQL database `test_db`. Never substitute
`kasset` in any command below.

The test harness enforces the target in four steps:

1. `tests/conftest.py` loads defaults, then calls
   `configure_test_database_environment()` before importing
   `app.core.config.settings`.
2. `tests/_run_owned_database.py` rejects
   `AUTO_TRADER_TEST_DATABASE_URL` unless its database name is exactly
   `test_db`. `AUTO_TRADER_PYTEST_USE_SHARED_DB=1` then forces `DATABASE_URL`
   to that validated URL instead of creating `test_db_pytest_*`.
3. `app/core/config.py` gives process environment variables precedence over
   `.env`, so the forced `DATABASE_URL` reaches the application engine.
4. `tests/_schema_bootstrap.py` receives the already configured application
   engine. It applies test DDL through that engine and does not choose another
   database.

The shell preflight below derives the test URL without displaying its
credentials, checks both URL variables inside the disposable container, and
aborts unless both target `test_db` over loopback.

## 1. Connect and check the deployed inputs

Run on the server:

```bash
ssh root@100.73.186.78
set -eu

export KASSET_TEST_IMAGE='kasset-trader-core:4e6329d1'
export KASSET_TEST_COMMIT='2ed7ef40'
export KASSET_TEST_DEPS_VOLUME='kasset-pytest-deps-4e6329d1'

[ "$(git -C /opt/kasset-trader-core rev-parse --short=8 HEAD)" = "$KASSET_TEST_COMMIT" ]
docker image inspect "$KASSET_TEST_IMAGE" >/dev/null
[ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' kasset-trader-db-1)" = 'kasset-trader_default' ]

docker run --rm --entrypoint /bin/sh "$KASSET_TEST_IMAGE" -lc \
  'if [ -d /app/tests ]; then echo tests=present; else echo tests=absent; fi'
[ -d /opt/kasset-trader-core/tests ]
```

Image `4e6329d1` prints `tests=absent`, so every test command below mounts the
matching checkout's `tests/` directory read-only at `/app/tests`. Stop if the
checkout commit assertion fails. Update the image, commit, and dependency-volume
names together after a deployment.

## 2. Install the locked test-only dependencies

The API image contains `/app/.venv/bin/pytest`, but it omits the rest of the
`test` dependency group. Install the versions recorded by this checkout's
`uv.lock` into a versioned Docker volume. The application image remains
unchanged.

```bash
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

## 3. Force and verify `test_db`

The test socket guard permits loopback and blocks the Compose hostname `db`.
This block reads the API container's `DATABASE_URL`, rejects a source database
other than `kasset`, changes the database to `test_db`, and changes the host to
`127.0.0.1`. The test container shares the PostgreSQL container's Compose
network namespace, so that loopback address reaches PostgreSQL without
weakening the socket guard. The command substitution never displays the URL.

```bash
export KASSET_TEST_DATABASE_URL="$(
  docker exec kasset-trader-api-1 /app/.venv/bin/python -c \
    'import os; from sqlalchemy.engine import make_url; u=make_url(os.environ["DATABASE_URL"]); assert u.get_backend_name() == "postgresql" and u.database == "kasset" and u.host; print(u.set(host="127.0.0.1", database="test_db").render_as_string(hide_password=False))'
)"

docker run --rm \
  --network container:kasset-trader-db-1 \
  --mount type=bind,src=/opt/kasset-trader-core/tests,dst=/app/tests,readonly \
  --mount type=volume,src="$KASSET_TEST_DEPS_VOLUME",dst=/test-deps,readonly \
  -e AUTO_TRADER_TEST_DATABASE_URL="$KASSET_TEST_DATABASE_URL" \
  -e AUTO_TRADER_PYTEST_USE_SHARED_DB=1 \
  -e DATABASE_URL="$KASSET_TEST_DATABASE_URL" \
  -e PYTHONPATH=/test-deps:/app \
  --entrypoint /app/.venv/bin/python \
  "$KASSET_TEST_IMAGE" \
  -c 'import os; from sqlalchemy.engine import make_url; a=make_url(os.environ["AUTO_TRADER_TEST_DATABASE_URL"]); d=make_url(os.environ["DATABASE_URL"]); assert a.database == d.database == "test_db"; assert a.host == d.host == "127.0.0.1"; assert (a.port, a.username) == (d.port, d.username); print("validated database target: test_db via loopback")'

docker exec kasset-trader-db-1 \
  psql -U kasset -d test_db -tAc 'SELECT current_database()'
```

Expected lines:

```text
validated database target: test_db via loopback
test_db
```

Do not proceed if either line differs. Do not pass `--env-file
/opt/kasset-trader-core/.env.kasset`; that would inject live provider and broker
credentials into the test process.

The first database-backed run creates the test schema in shared `test_db`; it
does not return that database to zero tables. This is expected. Judge isolation
with the `kasset` table-count check in section 7, not with a zero-table check on
`test_db`.

## 4. Define the disposable runner

Keep this serial. Do not add `-n auto`, `-n 2`, or another xdist option. Shared
`test_db` and the 2-vCPU production host require one pytest process.

```bash
run_server_pytest() {
  container_name="$1"
  shift
  docker run --rm --name "$container_name" \
    --cpus=1.0 \
    --network container:kasset-trader-db-1 \
    --mount type=bind,src=/opt/kasset-trader-core/tests,dst=/app/tests,readonly \
    --mount type=volume,src="$KASSET_TEST_DEPS_VOLUME",dst=/test-deps,readonly \
    -e AUTO_TRADER_TEST_DATABASE_URL="$KASSET_TEST_DATABASE_URL" \
    -e AUTO_TRADER_PYTEST_USE_SHARED_DB=1 \
    -e DATABASE_URL="$KASSET_TEST_DATABASE_URL" \
    -e PYTHONPATH=/test-deps:/app \
    -e PYTHONDONTWRITEBYTECODE=1 \
    --entrypoint /app/.venv/bin/pytest \
    "$KASSET_TEST_IMAGE" \
    -q -p no:cacheprovider "$@"
}
```

## 5. Run the KAsset API slice and observe load

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

Save the final pytest summary and the `uptime`/`docker stats` lines with the test
report. A CPU reading near 100% is expected because `--cpus=1.0` caps the
container at one core.

## 6. Run a Linux-only B0-X lock file

```bash
run_server_pytest \
  kasset-pytest-b0x \
  tests/scripts/b0x/test_envelope_and_locks.py
```

This file imports `scripts.b0x.ledger`, which imports `fcntl` and uses
`fcntl.flock` for the writer lock. A successful summary proves Linux collected
and executed the file rather than applying the Windows collection exclusion.

## 7. Prove production isolation and service health

Run these immediately after the tests. The first command is the production DB
misrouting check and must still print `103` for this deployed schema.

```bash
docker exec kasset-trader-db-1 \
  psql -U kasset -d kasset -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"

docker ps --filter 'name=kasset-trader-' \
  --format 'table {{.Names}}\t{{.Status}}'
```

Expect seven running containers: `db`, `redis`, `api`, `worker`, `scheduler`,
`mcp`, and `caddy`. The test procedure must not stop, restart, or exec test code
inside any of those containers.

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
