#!/bin/bash
# 검증 전용: kasset-test-db 안 이 Maker 고유 DB만 사용한다.
S=/tmp/kasset-swing-shadow-20261003-01a0fead
L=/tmp/kasset-swing-shadow-logs
IMG=kasset-trader-core:04d62828e06f72ae7a4c314c796529db7e744759
V=kasset-pytest-deps-04d62828-swing
run_py() {
  db="$1"; shift
  docker run --rm --cpus=1.0 --network container:kasset-test-db \
    --mount type=bind,src=$S,dst=/work,readonly \
    --mount type=volume,src=$V,dst=/test-deps,readonly \
    --env-file $S/env.example \
    -e DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/$db \
    -e SECRET_KEY=Test_Secret_Key_12345_Test_Secret_Key_12345 \
    -e PYTHONPATH=/work:/test-deps -e PYTHONDONTWRITEBYTECODE=1 \
    -w /work --entrypoint /app/.venv/bin/python $IMG "$@"
}
psql_db() { docker exec -i kasset-test-db psql -v ON_ERROR_STOP=1 -U postgres -d "$1" "${@:2}"; }

case "$1" in
alembic)
  DB=swing_alembic_20261003_01a0fead
  psql_db postgres -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"
  run_py $DB -c '
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.config import settings
import app.models  # noqa: F401
from app.models.base import Base
async def main():
    engine = create_async_engine(settings.DATABASE_URL)
    async with engine.begin() as conn:
        for schema in ("paper", "research", "review"):
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("CREATE_ALL_OK")
asyncio.run(main())'
  echo CREATE_ALL_EXIT=$?
  run_py $DB -m alembic stamp head; echo STAMP_EXIT=$?
  run_py $DB -m alembic current; echo CURRENT_EXIT=$?
  run_py $DB -m alembic downgrade -1; echo DOWNGRADE_EXIT=$?
  psql_db $DB -tAc "SELECT version_num FROM alembic_version" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='review' AND table_name LIKE 'kasset_swing_shadow%'"
  run_py $DB -m alembic upgrade head; echo UPGRADE_EXIT=$?
  psql_db $DB -tAc "SELECT version_num FROM alembic_version" -tAc "SELECT table_name FROM information_schema.tables WHERE table_schema='review' AND table_name LIKE 'kasset_swing_shadow%' ORDER BY 1" -tAc "SELECT conname FROM pg_constraint WHERE conrelid IN ('review.kasset_swing_shadow_runs'::regclass,'review.kasset_swing_shadow_signals'::regclass) ORDER BY 1"
  psql_db postgres -c "DROP DATABASE $DB"; echo DROP_EXIT=$?
  ;;
smoke-schema)
  DB=swing_smoke_20261003_01a0fead
  psql_db $DB -c "CREATE TABLE IF NOT EXISTS public.alembic_version (version_num varchar(32) PRIMARY KEY)" -c "DELETE FROM public.alembic_version" -c "INSERT INTO public.alembic_version VALUES ('20260926_symbol_master_adr')"
  run_py $DB -m alembic upgrade 20260926_symbol_master_adr:20261003_kasset_swing_shadow --sql > $L/smoke-migration.sql; echo SQL_EXIT=$?
  psql_db $DB < $L/smoke-migration.sql; echo APPLY_EXIT=$?
  psql_db $DB -tAc "SELECT version_num FROM alembic_version"
  ;;
observe)
  run_py swing_smoke_20261003_01a0fead -m scripts.kasset_swing_shadow observe; echo OBSERVE_EXIT=$?
  ;;
report)
  run_py swing_smoke_20261003_01a0fead -m scripts.kasset_swing_shadow report --since "$2" --signals; echo REPORT_EXIT=$?
  ;;
esac
