"""
db/init_db.py  (Unified Clean Schema — Exactly 7 Relational Tables)
==================================================================
Eliminates all duplicate tables and maintains ONLY the 7 canonical relational
tables corresponding to the IndusInd form sections:

The 7 Relational Tables:
------------------------
  1. insurance_quote   - Master quote/session table (proposal #, trace id, dates, flow flags)
  2. vehicle_details   - Vehicle Details section (FK: quote_id -> insurance_quote.id)
  3. cover_details     - Cover Details section (FK: quote_id -> insurance_quote.id)
  4. customer_details  - Customer Details section (FK: quote_id -> insurance_quote.id)
  5. nominee_details   - Nominee Details section (FK: quote_id -> insurance_quote.id, customer_id -> customer_details.id)
  6. address_details   - Communication & Permanent Address (FK: quote_id -> insurance_quote.id)
  7. premium_details   - Scraped Premium Summary (FK: quote_id -> insurance_quote.id)

Master View:
------------
  v_complete_insurance_quote - Joins all 7 tables together
  v_scrape_input_flat        - Alias view for backwards compatibility

Usage:
    python -m db.init_db
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import psycopg2
import psycopg2.extras
from psycopg2 import sql as pg_sql

# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("db.init")

DB_HOST     = os.getenv("PG_HOST",     "localhost")
DB_PORT     = os.getenv("PG_PORT",     "5432")
DB_NAME     = os.getenv("PG_DB",       "insurance")
DB_USER     = os.getenv("PG_USER",     "postgres")
DB_PASSWORD = os.getenv("PG_PASSWORD", "")

# ---------------------------------------------------------------------------
# 1. Master Table: insurance_quote
# ---------------------------------------------------------------------------
CREATE_INSURANCE_QUOTE_SQL = """
CREATE TABLE IF NOT EXISTS insurance_quote (
    id                      SERIAL          PRIMARY KEY,
    registration_number     TEXT            NOT NULL,
    policy_start_date       TEXT,
    purchase_date           TEXT,
    business_type           TEXT            DEFAULT 'New',
    customer_type           TEXT            DEFAULT 'Individual',
    cover_type              TEXT,
    proposal_number         TEXT,
    trace_id                TEXT,
    -- Automation flow flags
    continue_to_buy         BOOLEAN         NOT NULL DEFAULT TRUE,
    validate_customer       BOOLEAN         NOT NULL DEFAULT TRUE,
    upload_document         BOOLEAN         NOT NULL DEFAULT TRUE,
    upload_doc_type         TEXT            DEFAULT 'Invoice_Copy',
    upload_doc_path         TEXT            DEFAULT 'sample_documents/sample_document.pdf',
    save_proposal           BOOLEAN         NOT NULL DEFAULT TRUE,
    proceed_to_payment      BOOLEAN         NOT NULL DEFAULT TRUE,
    is_active               BOOLEAN         NOT NULL DEFAULT TRUE,
    notes                   TEXT,
    scraped_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (registration_number, policy_start_date)
);

CREATE INDEX IF NOT EXISTS idx_insurance_quote_scraped_at
    ON insurance_quote (scraped_at DESC);
"""

# ---------------------------------------------------------------------------
# 2. Vehicle Details Section Table: vehicle_details
# ---------------------------------------------------------------------------
CREATE_VEHICLE_DETAILS_SQL = """
CREATE TABLE IF NOT EXISTS vehicle_details (
    id                      SERIAL          PRIMARY KEY,
    quote_id                INTEGER         NOT NULL
                                            REFERENCES insurance_quote(id) ON DELETE CASCADE,
    registration_number     TEXT            NOT NULL,
    rto_location            TEXT,
    make_model_variant      TEXT,
    vehicle_make_model      TEXT,
    fuel_type               TEXT            DEFAULT 'Petrol',
    manufacturing_year      TEXT,
    manufacturing_month     TEXT,
    engine_number           TEXT,
    chassis_number          TEXT,
    seating_capacity        TEXT,
    cc_watt                 TEXT,
    vehicle_segment         TEXT,
    zone                    TEXT,
    lcc                     TEXT,
    ex_showroom_price       TEXT,
    is_new_vehicle          BOOLEAN         DEFAULT TRUE,
    ncb_eligibility         TEXT            DEFAULT 'None',
    discount_percent        INTEGER         DEFAULT 0,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (quote_id)
);

CREATE INDEX IF NOT EXISTS idx_vehicle_details_quote
    ON vehicle_details (quote_id);
"""

# ---------------------------------------------------------------------------
# 3. Cover Details Section Table: cover_details
# ---------------------------------------------------------------------------
CREATE_COVER_DETAILS_SQL = """
CREATE TABLE IF NOT EXISTS cover_details (
    id                                  SERIAL  PRIMARY KEY,
    quote_id                            INTEGER NOT NULL
                                                REFERENCES insurance_quote(id) ON DELETE CASCADE,

    -- OD Package Covers
    basic_od                            BOOLEAN DEFAULT FALSE,
    non_electrical_accessories          BOOLEAN DEFAULT FALSE,
    electrical_accessories              BOOLEAN DEFAULT FALSE,
    geo_extension                       BOOLEAN DEFAULT FALSE,
    driving_tuition                     BOOLEAN DEFAULT FALSE,
    fibre_glass_tank                    BOOLEAN DEFAULT FALSE,
    voluntary_deductible                BOOLEAN DEFAULT FALSE,
    anti_theft_device                   BOOLEAN DEFAULT FALSE,
    specially_designed                  BOOLEAN DEFAULT FALSE,
    automobile_association              BOOLEAN DEFAULT FALSE,

    -- Liability Covers
    tppd                                BOOLEAN DEFAULT TRUE,
    tppd_limit                          TEXT    DEFAULT '6000',
    basic_liability                     BOOLEAN DEFAULT TRUE,
    pa_to_owner_driver                  BOOLEAN DEFAULT TRUE,
    cpa_tenure                          TEXT    DEFAULT '1',
    pa_to_named_passenger               BOOLEAN DEFAULT FALSE,
    pa_to_unnamed_passenger             BOOLEAN DEFAULT FALSE,
    pa_to_paid_driver                   BOOLEAN DEFAULT FALSE,
    liability_to_paid_driver            BOOLEAN DEFAULT FALSE,
    liability_to_employees              BOOLEAN DEFAULT FALSE,

    -- AddOn Covers
    nil_depreciation                    BOOLEAN DEFAULT TRUE,
    electric_motor_protect              BOOLEAN DEFAULT FALSE,
    ev_battery_protection               BOOLEAN DEFAULT FALSE,
    ev_charger_cover                    BOOLEAN DEFAULT FALSE,
    hospi_cash_30                       BOOLEAN DEFAULT FALSE,
    hospi_cash_15                       BOOLEAN DEFAULT FALSE,
    emergency_medical                   BOOLEAN DEFAULT FALSE,
    assistance_cover_tw_shield          BOOLEAN DEFAULT FALSE,
    assistance_cover_tw                 BOOLEAN DEFAULT FALSE,
    assistance_cover_24x7               BOOLEAN DEFAULT FALSE,
    key_protect                         BOOLEAN DEFAULT FALSE,
    daily_allowance_7days               BOOLEAN DEFAULT FALSE,
    emi_protection                      BOOLEAN DEFAULT FALSE,
    is_side_car                         BOOLEAN DEFAULT FALSE,

    created_at                          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (quote_id)
);

CREATE INDEX IF NOT EXISTS idx_cover_details_quote
    ON cover_details (quote_id);
"""

# ---------------------------------------------------------------------------
# 4. Customer Details Section Table: customer_details
# ---------------------------------------------------------------------------
CREATE_CUSTOMER_DETAILS_SQL = """
CREATE TABLE IF NOT EXISTS customer_details (
    id                  SERIAL          PRIMARY KEY,
    quote_id            INTEGER         NOT NULL
                                        REFERENCES insurance_quote(id) ON DELETE CASCADE,
    first_name          TEXT,
    middle_name         TEXT,
    last_name           TEXT,
    dob                 TEXT,
    pan                 TEXT,
    gender              TEXT            DEFAULT 'Male',
    occupation          TEXT            DEFAULT 'Others',
    marital_status      TEXT            DEFAULT 'Single',
    mobile              TEXT,
    email               TEXT,
    aadhar              TEXT,
    nationality         TEXT            DEFAULT 'Indian',
    registered_gst      TEXT            DEFAULT 'No',
    related_party       TEXT            DEFAULT 'No',
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (quote_id)
);

CREATE INDEX IF NOT EXISTS idx_customer_details_quote
    ON customer_details (quote_id);
"""

# ---------------------------------------------------------------------------
# 5. Nominee Details Section Table: nominee_details
# ---------------------------------------------------------------------------
CREATE_NOMINEE_DETAILS_SQL = """
CREATE TABLE IF NOT EXISTS nominee_details (
    id                  SERIAL          PRIMARY KEY,
    quote_id            INTEGER         NOT NULL
                                        REFERENCES insurance_quote(id) ON DELETE CASCADE,
    customer_id         INTEGER         REFERENCES customer_details(id) ON DELETE CASCADE,
    nominee_name        TEXT,
    nominee_dob         TEXT,
    nominee_relation    TEXT            DEFAULT 'Father',
    nominee_age         TEXT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (quote_id)
);

CREATE INDEX IF NOT EXISTS idx_nominee_details_quote
    ON nominee_details (quote_id);
"""

# ---------------------------------------------------------------------------
# 6. Address Details Section Table: address_details
# ---------------------------------------------------------------------------
CREATE_ADDRESS_DETAILS_SQL = """
CREATE TABLE IF NOT EXISTS address_details (
    id                  SERIAL          PRIMARY KEY,
    quote_id            INTEGER         NOT NULL
                                        REFERENCES insurance_quote(id) ON DELETE CASCADE,

    -- Communication Address
    comm_flat           TEXT,
    comm_building       TEXT,
    comm_street         TEXT,
    comm_landmark       TEXT,
    comm_pincode        TEXT,
    comm_state          TEXT,
    comm_city           TEXT,
    comm_mobile         TEXT,
    comm_email          TEXT,

    -- Permanent Address
    perm_same_as_comm   BOOLEAN         NOT NULL DEFAULT TRUE,
    perm_flat           TEXT,
    perm_building       TEXT,
    perm_street         TEXT,
    perm_landmark       TEXT,
    perm_pincode        TEXT,
    perm_state          TEXT,
    perm_city           TEXT,

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (quote_id)
);

CREATE INDEX IF NOT EXISTS idx_address_details_quote
    ON address_details (quote_id);
"""

# ---------------------------------------------------------------------------
# 7. Premium Summary Section Table: premium_details
# ---------------------------------------------------------------------------
CREATE_PREMIUM_DETAILS_SQL = """
CREATE TABLE IF NOT EXISTS premium_details (
    id                  SERIAL          PRIMARY KEY,
    quote_id            INTEGER         NOT NULL
                                        REFERENCES insurance_quote(id) ON DELETE CASCADE,
    idv                 TEXT,
    base_od_premium     TEXT,
    tp_premium          TEXT,
    gst                 TEXT,
    net_premium         TEXT,
    total_premium       TEXT,
    extra_fields        JSONB           DEFAULT '{}',
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (quote_id)
);

CREATE INDEX IF NOT EXISTS idx_premium_details_quote
    ON premium_details (quote_id);
"""

# ---------------------------------------------------------------------------
# Complete View: v_complete_insurance_quote
# ---------------------------------------------------------------------------
CREATE_COMPLETE_QUOTE_VIEW_SQL = """
CREATE OR REPLACE VIEW v_complete_insurance_quote AS
SELECT
    q.id                            AS quote_id,
    q.id                            AS id,
    q.registration_number,
    q.policy_start_date,
    q.purchase_date,
    q.business_type,
    q.customer_type,
    q.cover_type,
    q.proposal_number,
    q.trace_id,
    q.continue_to_buy,
    q.validate_customer,
    q.upload_document,
    q.upload_doc_type,
    q.upload_doc_path,
    q.save_proposal,
    q.proceed_to_payment,
    q.is_active,
    q.notes,
    q.scraped_at,
    q.created_at,
    q.updated_at,

    -- Vehicle section
    v.rto_location,
    v.make_model_variant,
    v.vehicle_make_model,
    v.fuel_type,
    v.manufacturing_year,
    v.manufacturing_month,
    v.engine_number,
    v.chassis_number,
    v.seating_capacity,
    v.cc_watt,
    v.vehicle_segment,
    v.zone,
    v.lcc,
    v.ex_showroom_price,
    v.is_new_vehicle,
    v.ncb_eligibility,
    v.discount_percent,

    -- Cover section (OD)
    c.basic_od                      AS cover_basic_od,
    c.non_electrical_accessories    AS cover_non_electrical_accessories,
    c.electrical_accessories        AS cover_electrical_accessories,
    c.geo_extension                 AS cover_geo_extension,
    c.driving_tuition               AS cover_driving_tuition,
    c.fibre_glass_tank              AS cover_fibre_glass_tank,
    c.voluntary_deductible          AS cover_voluntary_deductible,
    c.anti_theft_device             AS cover_anti_theft_device,
    c.specially_designed            AS cover_specially_designed,
    c.automobile_association        AS cover_automobile_association,

    -- Cover section (Liability)
    c.tppd                          AS cover_tppd,
    c.tppd_limit                    AS cover_tppd_limit,
    c.basic_liability               AS cover_basic_liability,
    c.pa_to_owner_driver            AS cover_pa_to_owner_driver,
    c.cpa_tenure                    AS cover_cpa_tenure,
    c.pa_to_named_passenger         AS cover_pa_to_named_passenger,
    c.pa_to_unnamed_passenger       AS cover_pa_to_unnamed_passenger,
    c.pa_to_paid_driver             AS cover_pa_to_paid_driver,
    c.liability_to_paid_driver      AS cover_liability_to_paid_driver,
    c.liability_to_employees        AS cover_liability_to_employees,

    -- Cover section (Addon)
    c.nil_depreciation              AS cover_nil_depreciation,
    c.electric_motor_protect        AS cover_electric_motor_protect,
    c.ev_battery_protection         AS cover_ev_battery_protection,
    c.ev_charger_cover              AS cover_ev_charger_cover,
    c.hospi_cash_30                 AS cover_hospi_cash_30,
    c.hospi_cash_15                 AS cover_hospi_cash_15,
    c.emergency_medical             AS cover_emergency_medical,
    c.assistance_cover_tw_shield    AS cover_assistance_cover_tw_shield,
    c.assistance_cover_tw           AS cover_assistance_cover_tw,
    c.assistance_cover_24x7         AS cover_assistance_cover_24x7,
    c.key_protect                   AS cover_key_protect,
    c.daily_allowance_7days         AS cover_daily_allowance_7days,
    c.emi_protection                AS cover_emi_protection,
    c.is_side_car                   AS cover_is_side_car,

    -- Scraped booleans (aliases)
    c.basic_od                      AS scraped_basic_od,
    c.non_electrical_accessories    AS scraped_non_electrical_accessories,
    c.electrical_accessories        AS scraped_electrical_accessories,
    c.geo_extension                 AS scraped_geo_extension,
    c.driving_tuition               AS scraped_driving_tuition,
    c.fibre_glass_tank              AS scraped_fibre_glass_tank,
    c.voluntary_deductible          AS scraped_voluntary_deductible,
    c.anti_theft_device             AS scraped_anti_theft_device,
    c.specially_designed            AS scraped_specially_designed,
    c.automobile_association        AS scraped_automobile_association,
    c.tppd                          AS scraped_tppd,
    c.tppd_limit                    AS scraped_tppd_limit,
    c.basic_liability               AS scraped_basic_liability,
    c.pa_to_owner_driver            AS scraped_pa_to_owner_driver,
    c.cpa_tenure                    AS scraped_cpa_tenure,
    c.pa_to_named_passenger         AS scraped_pa_to_named_passenger,
    c.pa_to_unnamed_passenger       AS scraped_pa_to_unnamed_passenger,
    c.pa_to_paid_driver             AS scraped_pa_to_paid_driver,
    c.liability_to_paid_driver      AS scraped_liability_to_paid_driver,
    c.liability_to_employees        AS scraped_liability_to_employees,
    c.nil_depreciation              AS scraped_nil_depreciation,
    c.electric_motor_protect        AS scraped_electric_motor_protect,
    c.ev_battery_protection         AS scraped_ev_battery_protection,
    c.ev_charger_cover              AS scraped_ev_charger_cover,
    c.hospi_cash_30                 AS scraped_hospi_cash_30,
    c.hospi_cash_15                 AS scraped_hospi_cash_15,
    c.emergency_medical             AS scraped_emergency_medical,
    c.assistance_cover_tw_shield    AS scraped_assistance_cover_tw_shield,
    c.assistance_cover_tw           AS scraped_assistance_cover_tw,
    c.assistance_cover_24x7         AS scraped_assistance_cover_24x7,
    c.key_protect                   AS scraped_key_protect,
    c.daily_allowance_7days         AS scraped_daily_allowance_7days,
    c.emi_protection                AS scraped_emi_protection,
    c.is_side_car                   AS scraped_is_side_car,

    -- Customer section
    cust.id                         AS customer_id,
    cust.first_name                 AS cust_first_name,
    cust.middle_name                AS cust_middle_name,
    cust.last_name                  AS cust_last_name,
    cust.dob                        AS cust_dob,
    cust.pan                        AS cust_pan,
    cust.gender                     AS cust_gender,
    cust.occupation                 AS cust_occupation,
    cust.marital_status             AS cust_marital_status,
    cust.mobile                     AS cust_mobile,
    cust.email                      AS cust_email,
    cust.aadhar                     AS cust_aadhar,
    cust.nationality                AS cust_nationality,
    cust.registered_gst             AS cust_registered_gst,
    cust.related_party              AS cust_related_party,

    -- Nominee section
    nom.nominee_name,
    nom.nominee_dob,
    nom.nominee_relation,
    nom.nominee_age,

    -- Address section
    addr.comm_flat                  AS addr_flat,
    addr.comm_building              AS addr_building,
    addr.comm_street                AS addr_street,
    addr.comm_landmark              AS addr_landmark,
    addr.comm_pincode               AS addr_pincode,
    addr.comm_state                 AS addr_state,
    addr.comm_city                  AS addr_city,
    addr.comm_mobile                AS addr_mobile,
    addr.comm_email                 AS addr_email,
    addr.perm_same_as_comm          AS addr_perm_same_as_comm,
    addr.perm_flat,
    addr.perm_building,
    addr.perm_street,
    addr.perm_landmark,
    addr.perm_pincode,
    addr.perm_state,
    addr.perm_city,

    -- Premium section
    p.idv,
    p.base_od_premium,
    p.tp_premium,
    p.gst,
    p.net_premium,
    p.total_premium,
    p.extra_fields

FROM insurance_quote q
LEFT JOIN vehicle_details  v    ON v.quote_id    = q.id
LEFT JOIN cover_details    c    ON c.quote_id    = q.id
LEFT JOIN customer_details cust ON cust.quote_id = q.id
LEFT JOIN nominee_details  nom  ON nom.quote_id  = q.id
LEFT JOIN address_details  addr ON addr.quote_id = q.id
LEFT JOIN premium_details  p    ON p.quote_id    = q.id;
"""

# Backwards compatibility flat view
CREATE_FLAT_INPUT_VIEW_SQL = """
CREATE OR REPLACE VIEW v_scrape_input_flat AS
SELECT * FROM v_complete_insurance_quote;
"""


def _connect(dbname: str = None) -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=DB_HOST, port=int(DB_PORT),
        dbname=dbname or DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
    )


def create_database() -> None:
    log.info("Connecting to PostgreSQL server at %s:%s ...", DB_HOST, DB_PORT)
    try:
        conn = _connect("postgres")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (DB_NAME,))
        if cur.fetchone():
            log.info("Database '%s' already exists — skipping creation.", DB_NAME)
            print(f"  [SKIP] Database '{DB_NAME}' already exists.")
        else:
            cur.execute(pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(DB_NAME)))
            log.info("Database '%s' created.", DB_NAME)
            print(f"  [CREATED] Database '{DB_NAME}' created.")
        cur.close(); conn.close()
    except psycopg2.OperationalError as exc:
        log.error("Cannot connect to PostgreSQL: %s", exc)
        sys.exit(1)


def cleanup_duplicate_tables(cur) -> None:
    """Drop all redundant duplicate and legacy tables."""
    duplicate_tables = [
        "address_input",
        "cover_input",
        "customer_input",
        "nominee_input",
        "vehicle_input",
        "scrape_session",
        "scrape_input",
        "vehicle_coverage",
    ]
    log.info("Cleaning up duplicate tables: %s", duplicate_tables)
    for tbl in duplicate_tables:
        try:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
        except Exception as e:
            log.warning("Could not drop %s: %s", tbl, e)
    print("  [CLEANUP] Dropped duplicate tables successfully.")


def create_tables() -> None:
    log.info("Creating/verifying normalized schema in '%s' ...", DB_NAME)
    conn = _connect()
    cur = conn.cursor()
    try:
        # 1. Ensure the 7 Canonical Relational Tables exist
        canonical_tables = [
            (CREATE_INSURANCE_QUOTE_SQL,  "insurance_quote"),
            (CREATE_VEHICLE_DETAILS_SQL,  "vehicle_details"),
            (CREATE_COVER_DETAILS_SQL,    "cover_details"),
            (CREATE_CUSTOMER_DETAILS_SQL, "customer_details"),
            (CREATE_NOMINEE_DETAILS_SQL,  "nominee_details"),
            (CREATE_ADDRESS_DETAILS_SQL,  "address_details"),
            (CREATE_PREMIUM_DETAILS_SQL,  "premium_details"),
        ]

        for sql, name in canonical_tables:
            cur.execute(sql)
            conn.commit()
            log.info("Relational Table '%s' created / verified.", name)

        # Ensure all columns exist on insurance_quote in case table was created earlier
        cur.execute("""
            ALTER TABLE insurance_quote ADD COLUMN IF NOT EXISTS continue_to_buy BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE insurance_quote ADD COLUMN IF NOT EXISTS validate_customer BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE insurance_quote ADD COLUMN IF NOT EXISTS upload_document BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE insurance_quote ADD COLUMN IF NOT EXISTS upload_doc_type TEXT DEFAULT 'Invoice_Copy';
            ALTER TABLE insurance_quote ADD COLUMN IF NOT EXISTS upload_doc_path TEXT DEFAULT 'sample_documents/sample_document.pdf';
            ALTER TABLE insurance_quote ADD COLUMN IF NOT EXISTS save_proposal BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE insurance_quote ADD COLUMN IF NOT EXISTS proceed_to_payment BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE insurance_quote ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE insurance_quote ADD COLUMN IF NOT EXISTS notes TEXT;
            CREATE INDEX IF NOT EXISTS idx_insurance_quote_active ON insurance_quote (is_active, created_at DESC);
        """)
        conn.commit()

        # 2. Views (drop first so column order/names can be updated cleanly)
        cur.execute("DROP VIEW IF EXISTS v_scrape_input_flat CASCADE;")
        cur.execute("DROP VIEW IF EXISTS v_complete_insurance_quote CASCADE;")
        conn.commit()
        cur.execute(CREATE_COMPLETE_QUOTE_VIEW_SQL)
        cur.execute(CREATE_FLAT_INPUT_VIEW_SQL)
        conn.commit()
        log.info("Views created / replaced.")

        # 3. Drop redundant duplicate tables
        cleanup_duplicate_tables(cur)
        conn.commit()

        # 4. Report table count
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE'
            ORDER BY table_name;
        """)
        remaining = [r[0] for r in cur.fetchall()]
        print(f"\n  Active Canonical Tables in DB ({len(remaining)}):")
        for t in remaining:
            print(f"    - {t}")

    except Exception as exc:
        conn.rollback()
        log.error("Schema creation failed: %s", exc)
        sys.exit(1)
    finally:
        cur.close(); conn.close()


def setup_database() -> None:
    """Called on startup. Safe to call multiple times."""
    create_database()
    create_tables()


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("  Insurance Scraper — Clean 7-Table Relational Schema Setup")
    print("=" * 70)
    setup_database()
    print("  [OK] Setup complete!")
