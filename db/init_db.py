"""
db/init_db.py
=============
Fully automatic PostgreSQL setup for the Insurance scraper project.

What this script does (all automatic, no manual SQL needed):
  1. Reads connection settings from .env
  2. Connects to PostgreSQL server
  3. Creates the 'insurance' DATABASE if it does not exist
  4. Creates the 'vehicle_coverage' TABLE with all columns if it does not exist
  5. Prints a confirmation summary

Run this once before starting the scraper, OR just let the scraper
call it automatically on every start.

Usage:
    python -m db.init_db
"""

from __future__ import annotations

import logging
import os
import sys
from dotenv import load_dotenv

load_dotenv()   # read .env from project root

import psycopg2
from psycopg2 import sql as pg_sql

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("db.init")

# ---------------------------------------------------------------------------
# Settings (all from .env)
# ---------------------------------------------------------------------------
DB_HOST     = os.getenv("PG_HOST",     "localhost")
DB_PORT     = os.getenv("PG_PORT",     "5432")
DB_NAME     = os.getenv("PG_DB",       "insurance")        # <-- database name
DB_USER     = os.getenv("PG_USER",     "postgres")
DB_PASSWORD = os.getenv("PG_PASSWORD", "")

TABLE_NAME  = "vehicle_coverage"                            # <-- table name

# ---------------------------------------------------------------------------
# Full table schema  (all columns auto-created)
# ---------------------------------------------------------------------------
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS vehicle_coverage (

    -- Primary key (auto-increment)
    id                      SERIAL          PRIMARY KEY,

    -- Vehicle / registration info
    registration_number     TEXT            NOT NULL,
    rto_location            TEXT,
    manufacturing_year      TEXT,
    manufacturing_month     TEXT,
    fuel_type               TEXT,
    vehicle_make_model      TEXT,
    vehicle_segment         TEXT,
    cc_watt                 TEXT,
    zone                    TEXT,
    seating_capacity        TEXT,
    lcc                     TEXT,
    ex_showroom_price       TEXT,

    -- Policy info
    policy_start_date       TEXT,
    purchase_date           TEXT,
    business_type           TEXT,
    customer_type           TEXT,
    ncb_eligibility         TEXT,
    cover_type              TEXT,

    -- Coverage / premium details (scraped from real website)
    idv                     TEXT,
    own_damage_premium      TEXT,
    third_party_premium     TEXT,
    total_premium           TEXT,
    gst                     TEXT,
    net_premium             TEXT,
    base_od_premium         TEXT,
    tp_premium              TEXT,
    proposal_number         TEXT,
    trace_id                TEXT,

    -- Cover details scraped from website (JSON arrays)
    od_covers               JSONB           DEFAULT '[]',
    liability_covers        JSONB           DEFAULT '[]',
    addon_covers            JSONB           DEFAULT '[]',

    -- Raw scraped data (full text + JSON key-value pairs)
    raw_coverage_text       TEXT,
    extra_fields            JSONB           DEFAULT '{}',

    -- Audit
    scraped_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- Prevent duplicate rows for same vehicle + policy date
    UNIQUE (registration_number, policy_start_date)
);
"""

# ---------------------------------------------------------------------------
# Step 1: Create the database (connects via 'postgres' admin db)
# ---------------------------------------------------------------------------
def create_database() -> None:
    log.info("Connecting to PostgreSQL server at %s:%s ...", DB_HOST, DB_PORT)
    try:
        # Must connect to the 'postgres' default DB to create a new one
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT,
            dbname="postgres",
            user=DB_USER, password=DB_PASSWORD,
        )
        conn.autocommit = True   # required — CREATE DATABASE cannot run in a transaction
        cur = conn.cursor()

        # Check if our target database already exists
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s;",
            (DB_NAME,)
        )
        already_exists = cur.fetchone() is not None

        if already_exists:
            log.info("Database '%s' already exists — skipping creation.", DB_NAME)
            print(f"  [SKIP] Database '{DB_NAME}' already exists.")
        else:
            cur.execute(
                pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(DB_NAME))
            )
            log.info("Database '%s' created successfully.", DB_NAME)
            print(f"  [CREATED] Database '{DB_NAME}' created.")

        cur.close()
        conn.close()

    except psycopg2.OperationalError as exc:
        log.error("Cannot connect to PostgreSQL: %s", exc)
        print(f"\n  [ERROR] Cannot connect to PostgreSQL at {DB_HOST}:{DB_PORT}")
        print(f"  Check PG_HOST, PG_PORT, PG_USER, PG_PASSWORD in your .env file.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Step 2: Create the table (connects to the 'insurance' database)
# ---------------------------------------------------------------------------
def create_table() -> None:
    log.info("Connecting to database '%s' to create table '%s' ...", DB_NAME, TABLE_NAME)
    try:
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER, password=DB_PASSWORD,
        )
        cur = conn.cursor()

        # Create table (IF NOT EXISTS = safe to run multiple times)
        cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        log.info("Table '%s' created / verified.", TABLE_NAME)

        # Verify — read back all columns
        cur.execute(
            """
            SELECT
                ordinal_position,
                column_name,
                data_type,
                is_nullable,
                column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name   = %s
            ORDER BY ordinal_position;
            """,
            (TABLE_NAME,),
        )
        columns = cur.fetchall()

        cur.close()
        conn.close()
        return columns

    except psycopg2.OperationalError as exc:
        log.error("Cannot connect to database '%s': %s", DB_NAME, exc)
        sys.exit(1)
    except Exception as exc:
        log.error("Failed to create table: %s", exc)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Step 3: Print confirmation summary
# ---------------------------------------------------------------------------
def print_summary(columns: list) -> None:
    sep = "-" * 70
    print(f"\n{sep}")
    print(f"  DATABASE : {DB_NAME}")
    print(f"  HOST     : {DB_HOST}:{DB_PORT}")
    print(f"  USER     : {DB_USER}")
    print(f"  TABLE    : public.{TABLE_NAME}  ({len(columns)} columns)")
    print(sep)
    print(f"  {'#':<4}  {'Column Name':<30}  {'Type':<30}  Nullable")
    print(f"  {'-'*4}  {'-'*30}  {'-'*30}  --------")
    for pos, col_name, col_type, nullable, default in columns:
        null_str = "YES" if nullable == "YES" else "NO "
        print(f"  {pos:<4}  {col_name:<30}  {col_type:<30}  {null_str}")
    print(sep)
    print(f"\n  [OK] Setup complete!")
    print(f"  Refresh pgAdmin -> Databases -> {DB_NAME} -> Schemas -> public -> Tables")
    print(f"  You should see '{TABLE_NAME}' listed there.\n")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def setup_database() -> None:
    """
    Public function called by the scraper on every startup.
    Safe to call multiple times — uses IF NOT EXISTS.
    """
    create_database()
    columns = create_table()
    return columns


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("  Insurance Scraper -- PostgreSQL Auto-Setup")
    print("=" * 70)
    print(f"\n  Reading config from .env ...")
    print(f"  PG_HOST     = {DB_HOST}")
    print(f"  PG_PORT     = {DB_PORT}")
    print(f"  PG_DB       = {DB_NAME}")
    print(f"  PG_USER     = {DB_USER}")
    print(f"  PG_PASSWORD = {'*' * len(DB_PASSWORD)}")
    print()

    columns = setup_database()
    if columns:
        print_summary(columns)
