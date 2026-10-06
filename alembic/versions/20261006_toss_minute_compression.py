"""Compress old Toss 1-minute candle chunks (TimescaleDB compression policy).

Revision ID: 20261006_toss_minute_compression
Revises: 20261005_kasset_longterm_shadow
Create Date: 2026-10-06

``research.kr_candles_1m_toss`` grows ~0.65 GB per trading day (heap + five
indexes) and the production disk is moving to 80 GB.  Compressed with
``segmentby=symbol, orderby=time_utc DESC`` two independent 1/8-symbol samples
of weekly chunks shrank 22.4x and 22.7x (indexes all but vanish).

What this migration does -- and deliberately does not do:

* It only enables compression and registers one background policy.  It never
  compresses an existing chunk itself, so the deploy does not wait on it.  The
  first chunks are compressed by the policy's first run, at the next 02:30 KST.
* The policy compresses chunks whose end is older than 7 days.  The 1-minute
  collector upserts only the newest ~800 minutes, so it never touches a
  compressed chunk.  The manual repair CLI can upsert any past session; that is
  supported (``ON CONFLICT`` on a compressed chunk decompresses only the
  conflicting batches) -- see ``docs/runbooks/toss-minute-compression.md``.
* The job runs once a day at 02:30 KST (``timezone => 'Asia/Seoul'`` pins it to
  the wall clock): after the 20:00 KST NXT close and the 18:30 KST DB backup,
  well before the 08:50 KST open.  ``max_retries=3, retry_period=30 minutes``
  bound a failing run: measured on 2.29.2, retries back off exponentially
  (+33.5 min, then +67 min) and the third consecutive failure leaves the job
  Paused (``scheduled = false``), so the last attempt starts ~1h50 after the
  first, long before the 08:50 KST open, instead of retrying into market
  hours.  A paused job stays paused until an operator resumes it; the runbook
  has the status query and the ``alter_job`` that resumes it.
* No continuous-aggregate or retention policy is touched.

Plain PostgreSQL (the CI ``migration (PostgreSQL 15)`` job) has no
``timescaledb`` extension, and a table that is not a hypertable cannot be
compressed, so both cases are a NOTICE and a no-op, like the corpus migration.

``downgrade`` removes the policy first (so no new compression starts), then
decompresses every compressed chunk, then turns compression off -- PostgreSQL
refuses to disable compression while compressed chunks exist.  Decompression
rewrites all rows and needs the uncompressed size free (~15 GB for the
current 6 chunks); measured ~59k rows/s.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261006_toss_minute_compression"
down_revision: str | Sequence[str] | None = "20261005_kasset_longterm_shadow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "kr_candles_1m_toss"
COMPRESS_AFTER = "7 days"
SCHEDULE_INTERVAL = "1 day"
# 02:30 KST: after NXT closes (20:00) and the daily DB backup (18:30), before
# the NXT pre-market opens (08:00) and the KRX open (09:00).
RUN_AT_KST = "2 hours 30 minutes"
MAX_RETRIES = 3
RETRY_PERIOD = "30 minutes"
# Minimum for INSERT ... ON CONFLICT DO UPDATE on compressed chunks with
# segmentby-aware batch filtering (the repair CLI's upsert).
MIN_TIMESCALEDB_VERSION = "2, 16, 0"


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        DECLARE
            v_job_id INTEGER;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'
            ) THEN
                RAISE NOTICE
                    'timescaledb absent: research.{TABLE_NAME} compression skipped';
                RETURN;
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM timescaledb_information.hypertables
                WHERE hypertable_schema = 'research'
                  AND hypertable_name = '{TABLE_NAME}'
            ) THEN
                RAISE NOTICE
                    'research.{TABLE_NAME} is not a hypertable: compression skipped';
                RETURN;
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM pg_extension
                WHERE extname = 'timescaledb'
                  AND string_to_array(
                      regexp_replace(extversion, '[^0-9.]', '', 'g'), '.'
                  )::INTEGER[] >= ARRAY[{MIN_TIMESCALEDB_VERSION}]
            ) THEN
                RAISE EXCEPTION
                    'TimescaleDB 2.16.0 or newer is required for upserts into '
                    'compressed chunks';
            END IF;

            ALTER TABLE research.{TABLE_NAME} SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'symbol',
                timescaledb.compress_orderby = 'time_utc DESC'
            );

            PERFORM add_compression_policy(
                'research.{TABLE_NAME}',
                compress_after => INTERVAL '{COMPRESS_AFTER}',
                if_not_exists => TRUE,
                schedule_interval => INTERVAL '{SCHEDULE_INTERVAL}',
                initial_start => (
                    (
                        date_trunc('day', now() AT TIME ZONE 'Asia/Seoul')
                        + INTERVAL '1 day {RUN_AT_KST}'
                    ) AT TIME ZONE 'Asia/Seoul'
                ),
                timezone => 'Asia/Seoul'
            );

            SELECT job_id INTO v_job_id
            FROM timescaledb_information.jobs
            WHERE proc_name = 'policy_compression'
              AND hypertable_schema = 'research'
              AND hypertable_name = '{TABLE_NAME}';
            PERFORM alter_job(
                v_job_id,
                max_retries => {MAX_RETRIES},
                retry_period => INTERVAL '{RETRY_PERIOD}'
            );
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        DECLARE
            v_chunk REGCLASS;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'
            ) THEN
                RETURN;
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM timescaledb_information.hypertables
                WHERE hypertable_schema = 'research'
                  AND hypertable_name = '{TABLE_NAME}'
                  AND compression_enabled
            ) THEN
                RETURN;
            END IF;

            PERFORM remove_compression_policy(
                'research.{TABLE_NAME}', if_exists => TRUE
            );
            FOR v_chunk IN
                SELECT format('%I.%I', chunk_schema, chunk_name)::REGCLASS
                FROM timescaledb_information.chunks
                WHERE hypertable_schema = 'research'
                  AND hypertable_name = '{TABLE_NAME}'
                  AND is_compressed
                ORDER BY range_start
            LOOP
                PERFORM decompress_chunk(v_chunk, if_compressed => TRUE);
            END LOOP;
            ALTER TABLE research.{TABLE_NAME} SET (timescaledb.compress = FALSE);
        END
        $$
        """
    )
