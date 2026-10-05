#!/bin/bash
# 검증 전용: kasset-test-db 안 이 Maker 고유 DB만 쓰고 운영 DB는 읽기 전용 \copy로만 읽는다.
H=5e7c1a3d
S=/tmp/kasset-longterm-shadow-20261005-$H
L=/tmp/kasset-longterm-shadow-logs-$H
IMG=kasset-trader-core:b22c1eac0c02197306281c229e7968d0c2b6760b
V=kasset-pytest-deps-04d62828-swing
SMOKE=longterm_smoke_20261005_$H
ALEMBIC=longterm_alembic_20261005_$H
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
prod_copy() { docker exec -e PGOPTIONS='-c default_transaction_read_only=on' kasset-trader-db-1 psql -v ON_ERROR_STOP=1 -U kasset -d kasset -c "$1"; }
CREATE_ALL='
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.config import settings
import app.models  # noqa: F401
from app.models.base import Base
async def main():
    engine = create_async_engine(settings.DATABASE_URL)
    tables = [t for t in Base.metadata.sorted_tables if "longterm" not in t.name]
    async with engine.begin() as conn:
        for schema in ("paper", "research", "review"):
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    await engine.dispose()
    print("CREATE_ALL_OK", len(tables))
asyncio.run(main())'

case "$1" in
alembic)
  DB=$ALEMBIC
  psql_db postgres -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"
  run_py $DB -c "$CREATE_ALL"; echo CREATE_ALL_EXIT=$?
  # 직전 head(스윙)까지 만든 DB에 새 head를 stamp 없이 올리고 내렸다 올린다.
  run_py $DB -m alembic stamp 20261003_kasset_swing_shadow; echo STAMP_EXIT=$?
  run_py $DB -m alembic upgrade head; echo UPGRADE1_EXIT=$?
  psql_db $DB -tAc "SELECT version_num FROM alembic_version" -tAc "SELECT table_name FROM information_schema.tables WHERE table_schema='review' AND table_name LIKE 'kasset_longterm_shadow%' ORDER BY 1" -tAc "SELECT count(*) FROM pg_constraint WHERE conrelid IN ('review.kasset_longterm_shadow_runs'::regclass,'review.kasset_longterm_shadow_signals'::regclass)"
  run_py $DB -m alembic downgrade -1; echo DOWNGRADE_EXIT=$?
  psql_db $DB -tAc "SELECT version_num FROM alembic_version" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='review' AND table_name LIKE 'kasset_longterm_shadow%'" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='review' AND table_name LIKE 'kasset_swing_shadow%'"
  run_py $DB -m alembic upgrade head; echo UPGRADE2_EXIT=$?
  psql_db $DB -tAc "SELECT version_num FROM alembic_version" -tAc "SELECT table_name FROM information_schema.tables WHERE table_schema='review' AND table_name LIKE 'kasset_longterm_shadow%' ORDER BY 1" -tAc "SELECT conname FROM pg_constraint WHERE conrelid IN ('review.kasset_longterm_shadow_runs'::regclass,'review.kasset_longterm_shadow_signals'::regclass) ORDER BY 1"
  run_py $DB -m alembic heads; echo HEADS_EXIT=$?
  psql_db postgres -c "DROP DATABASE $DB"; echo DROP_EXIT=$?
  ;;
smoke-prep)
  DB=$SMOKE
  psql_db postgres -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"
  run_py $DB -c "$CREATE_ALL"; echo CREATE_ALL_EXIT=$?
  psql_db $DB -c "CREATE TABLE IF NOT EXISTS public.kr_candles_1d (time timestamptz NOT NULL, symbol text NOT NULL, venue text NOT NULL, open numeric NOT NULL, high numeric NOT NULL, low numeric NOT NULL, close numeric NOT NULL, volume numeric NOT NULL, value numeric, source text, ingested_at timestamptz DEFAULT now(), CONSTRAINT uq_kr_candles_1d_time_symbol_venue UNIQUE (time, symbol, venue))"
  UCOLS="symbol,name,exchange,nxt_eligible,is_active,security_type,is_common_share,listing_status,delist_date,krx_trading_suspended"
  prod_copy "\\copy (SELECT $UCOLS FROM public.kr_symbol_universe) TO STDOUT" | psql_db $DB -c "\\copy public.kr_symbol_universe ($UCOLS) FROM STDIN"
  CCOLS="time,symbol,venue,open,high,low,close,volume,value,source,ingested_at"
  prod_copy "\\copy (SELECT $CCOLS FROM public.kr_candles_1d WHERE venue='KRX' AND time >= '2025-08-01') TO STDOUT" | psql_db $DB -c "\\copy public.kr_candles_1d ($CCOLS) FROM STDIN"
  FCOLS="market,symbol,fiscal_period,period_type,period_end_date,filing_date,effective_at,source,source_collected_at,currency,revenue,net_income,discrete_revenue,discrete_net_income,data_state,schema_version"
  prod_copy "\\copy (SELECT $FCOLS FROM public.financial_fundamentals_snapshots WHERE market='kr' AND period_type='quarterly') TO STDOUT" | psql_db $DB -c "\\copy public.financial_fundamentals_snapshots ($FCOLS) FROM STDIN"
  run_py $DB -m alembic stamp 20261003_kasset_swing_shadow; echo STAMP_EXIT=$?
  run_py $DB -m alembic upgrade head; echo UPGRADE_EXIT=$?
  psql_db $DB -tAc "SELECT version_num FROM alembic_version" -tAc "SELECT count(*), min(time), max(time) FROM public.kr_candles_1d" -tAc "SELECT count(*) FROM public.kr_symbol_universe" -tAc "SELECT count(*) FROM public.financial_fundamentals_snapshots"
  ;;
observe)
  date -Is
  run_py $SMOKE -m scripts.kasset_longterm_shadow observe; echo OBSERVE_EXIT=$?
  ;;
report)
  shift
  run_py $SMOKE -m scripts.kasset_longterm_shadow report "$@"; echo REPORT_EXIT=$?
  ;;
sql)
  shift
  psql_db $SMOKE "$@"
  ;;
isolation)
  echo "prod longterm tables:"; docker exec kasset-trader-db-1 psql -U kasset -d kasset -tAc "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE '%longterm%'"
  echo "prod swing tables (unchanged expected 2):"; docker exec kasset-trader-db-1 psql -U kasset -d kasset -tAc "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'kasset_swing_shadow%'"
  echo "prod public tables:"; docker exec kasset-trader-db-1 psql -U kasset -d kasset -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
  echo "test-db longterm dbs:"; docker exec kasset-test-db psql -U postgres -tAc "SELECT datname FROM pg_database WHERE datname LIKE 'longterm_%' OR datname LIKE 'test_db_pytest%'"
  ;;
swing-compare)
  # 같은 smoke DB에서 기준 커밋(b22c1eac) 코드와 수정 코드로 스윙 SHADOW를 각각 실행해 지문·결과를 비교한다.
  B=$S-base
  FP='
from app.extensions.kasset.automation.swing_shadow import DEFAULT_SWING_SHADOW_CONFIG as c
print("SWING_FINGERPRINT", c.fingerprint)'
  for dir in $B $S; do
    echo "== source: $dir"
    docker run --rm --network none --mount type=bind,src=$dir,dst=/work,readonly --mount type=volume,src=$V,dst=/test-deps,readonly \
      --env-file $S/deploy/kasset/env.example -e SECRET_KEY=Test_Secret_Key_12345_Test_Secret_Key_12345 \
      -e PYTHONPATH=/work:/test-deps -e PYTHONDONTWRITEBYTECODE=1 -w /work --entrypoint /app/.venv/bin/python $IMG -c "$FP"
  done
  for dir in $B $S; do
    echo "== swing observe with source: $dir"
    docker run --rm --cpus=1.0 --network container:kasset-test-db --mount type=bind,src=$dir,dst=/work,readonly --mount type=volume,src=$V,dst=/test-deps,readonly \
      --env-file $S/deploy/kasset/env.example -e DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/$SMOKE \
      -e SECRET_KEY=Test_Secret_Key_12345_Test_Secret_Key_12345 -e PYTHONPATH=/work:/test-deps -e PYTHONDONTWRITEBYTECODE=1 \
      -w /work --entrypoint /app/.venv/bin/python $IMG -m scripts.kasset_swing_shadow observe; echo SWING_OBSERVE_EXIT=$?
  done
  ;;
cleanup)
  psql_db postgres -c "DROP DATABASE IF EXISTS $SMOKE" -c "DROP DATABASE IF EXISTS $ALEMBIC"
  rm -rf $S $L; echo CLEANUP_EXIT=$?
  ;;
esac
