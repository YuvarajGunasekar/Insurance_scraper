"""
db/postgres_client.py
=====================
PostgreSQL client for the Insurance scraper.

- Auto-creates the database and table on first run (via db.init_db)
- Upserts scraped CoverageDetails records into vehicle_coverage
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Generator

import psycopg2
from psycopg2.extensions import connection as PgConnection

from pages.vehicle_details_page import CoverageDetails

logger = logging.getLogger("scrape_api")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _pg_config() -> dict:
    """Read DB connection settings from environment / .env."""
    return {
        "host":     os.getenv("PG_HOST",     "localhost"),
        "port":     os.getenv("PG_PORT",     "5432"),
        "dbname":   os.getenv("PG_DB",       "insurance"),
        "user":     os.getenv("PG_USER",     "postgres"),
        "password": os.getenv("PG_PASSWORD", ""),
    }

TABLE_NAME = "vehicle_coverage"

# ---------------------------------------------------------------------------
# Upsert SQL
# ---------------------------------------------------------------------------

UPSERT_SQL = f"""
INSERT INTO {TABLE_NAME} (
    registration_number, rto_location, manufacturing_year, manufacturing_month,
    policy_start_date, fuel_type, idv, own_damage_premium, third_party_premium,
    total_premium, gst, net_premium, base_od_premium, tp_premium,
    cover_type, vehicle_make_model,
    cc_watt, vehicle_segment, zone, seating_capacity, lcc,
    ex_showroom_price, proposal_number, trace_id,
    od_covers, liability_covers, addon_covers,
    raw_coverage_text, extra_fields
) VALUES (
    %(registration_number)s, %(rto_location)s, %(manufacturing_year)s, %(manufacturing_month)s,
    %(policy_start_date)s, %(fuel_type)s, %(idv)s, %(own_damage_premium)s, %(third_party_premium)s,
    %(total_premium)s, %(gst)s, %(net_premium)s, %(base_od_premium)s, %(tp_premium)s,
    %(cover_type)s, %(vehicle_make_model)s,
    %(cc_watt)s, %(vehicle_segment)s, %(zone)s, %(seating_capacity)s, %(lcc)s,
    %(ex_showroom_price)s, %(proposal_number)s, %(trace_id)s,
    %(od_covers)s, %(liability_covers)s, %(addon_covers)s,
    %(raw_coverage_text)s, %(extra_fields)s
)
ON CONFLICT (registration_number, policy_start_date)
DO UPDATE SET
    rto_location           = EXCLUDED.rto_location,
    manufacturing_year     = EXCLUDED.manufacturing_year,
    manufacturing_month    = EXCLUDED.manufacturing_month,
    fuel_type              = EXCLUDED.fuel_type,
    idv                    = EXCLUDED.idv,
    own_damage_premium     = EXCLUDED.own_damage_premium,
    third_party_premium    = EXCLUDED.third_party_premium,
    total_premium          = EXCLUDED.total_premium,
    gst                    = EXCLUDED.gst,
    net_premium            = EXCLUDED.net_premium,
    base_od_premium        = EXCLUDED.base_od_premium,
    tp_premium             = EXCLUDED.tp_premium,
    cover_type             = EXCLUDED.cover_type,
    vehicle_make_model     = EXCLUDED.vehicle_make_model,
    cc_watt                = EXCLUDED.cc_watt,
    vehicle_segment        = EXCLUDED.vehicle_segment,
    zone                   = EXCLUDED.zone,
    seating_capacity       = EXCLUDED.seating_capacity,
    lcc                    = EXCLUDED.lcc,
    ex_showroom_price      = EXCLUDED.ex_showroom_price,
    proposal_number        = EXCLUDED.proposal_number,
    trace_id               = EXCLUDED.trace_id,
    od_covers              = EXCLUDED.od_covers,
    liability_covers       = EXCLUDED.liability_covers,
    addon_covers           = EXCLUDED.addon_covers,
    raw_coverage_text      = EXCLUDED.raw_coverage_text,
    extra_fields           = EXCLUDED.extra_fields,
    scraped_at             = NOW()
RETURNING id;
"""

# ---------------------------------------------------------------------------
# Connection context manager
# ---------------------------------------------------------------------------

@contextmanager
def _get_conn() -> Generator[PgConnection, None, None]:
    cfg = _pg_config()
    conn = psycopg2.connect(**cfg)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Public: ensure database + table exist (delegates to init_db)
# ---------------------------------------------------------------------------

def ensure_table() -> None:
    """
    Auto-create the 'insurance' database and 'vehicle_coverage' table.
    Safe to call multiple times — uses CREATE IF NOT EXISTS.
    Also adds any missing columns (ALTER TABLE ADD COLUMN IF NOT EXISTS)
    so old tables are automatically migrated.
    """
    from db.init_db import setup_database
    setup_database()

    # Migrate existing tables that are missing newer columns
    _migrate_table()
    logger.info("[OK] Database and table are ready.")


# Columns added after initial release — added automatically via ALTER TABLE
_MIGRATION_COLUMNS: list[tuple[str, str]] = [
    ("base_od_premium",  "TEXT"),
    ("tp_premium",       "TEXT"),
    ("cover_type",       "TEXT"),
    ("vehicle_make_model","TEXT"),
    ("cc_watt",          "TEXT"),
    ("vehicle_segment",  "TEXT"),
    ("zone",             "TEXT"),
    ("seating_capacity", "TEXT"),
    ("lcc",              "TEXT"),
    ("ex_showroom_price","TEXT"),
    ("proposal_number",  "TEXT"),
    ("trace_id",         "TEXT"),
    ("od_covers",        "JSONB DEFAULT '[]'"),
    ("liability_covers", "JSONB DEFAULT '[]'"),
    ("addon_covers",     "JSONB DEFAULT '[]'"),
    ("raw_coverage_text","TEXT"),
    ("extra_fields",     "JSONB DEFAULT '{}'"),
]


def _migrate_table() -> None:
    """Add any columns that may be missing from an older table schema."""
    from db.init_db import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
    try:
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
            user=DB_USER, password=DB_PASSWORD,
        )
        cur = conn.cursor()
        for col_name, col_type in _MIGRATION_COLUMNS:
            # ADD COLUMN IF NOT EXISTS is safe to run multiple times
            cur.execute(
                f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {col_name} {col_type};"
            )
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Table migration complete.")
    except Exception as exc:
        logger.warning("Table migration warning (non-fatal): %s", exc)

# ---------------------------------------------------------------------------
# Public: save scraped coverage record
# ---------------------------------------------------------------------------

def save_coverage(record: CoverageDetails) -> int:
    """Upsert a CoverageDetails record into vehicle_coverage. Returns row id."""
    row = {
        "registration_number":  record.registration_number or "NEW",
        "rto_location":         record.rto_location,
        "manufacturing_year":   record.manufacturing_year,
        "manufacturing_month":  record.manufacturing_month,
        "policy_start_date":    record.policy_start_date,
        "fuel_type":            record.fuel_type,
        "idv":                  record.idv,
        "own_damage_premium":   record.own_damage_premium,
        "third_party_premium":  record.third_party_premium,
        "total_premium":        record.total_premium,
        "gst":                  record.gst,
        "net_premium":          record.net_premium,
        "base_od_premium":      record.base_od_premium,
        "tp_premium":           record.tp_premium,
        "cover_type":           record.cover_type,
        "vehicle_make_model":   record.vehicle_make_model or record.make_model_variant,
        "cc_watt":              record.cc_watt,
        "vehicle_segment":      record.vehicle_segment,
        "zone":                 record.zone,
        "seating_capacity":     record.seating_capacity,
        "lcc":                  record.lcc,
        "ex_showroom_price":    record.ex_showroom_price,
        "proposal_number":      record.proposal_number,
        "trace_id":             record.trace_id,
        "od_covers":            record.od_covers or "[]",
        "liability_covers":     record.liability_covers or "[]",
        "addon_covers":         record.addon_covers or "[]",
        "raw_coverage_text":    record.raw_coverage_text,
        "extra_fields":         json.dumps(record.extra_fields) if record.extra_fields else "{}",
    }
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(UPSERT_SQL, row)
            row_id = cur.fetchone()[0]

    logger.info(
        "[OK] Coverage saved | table=%s | registration=%s | row_id=%d",
        TABLE_NAME, record.registration_number or "NEW", row_id,
    )
    return row_id
