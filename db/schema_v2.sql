-- =============================================================================
-- Insurance Scraper – PostgreSQL Schema v2
-- =============================================================================
-- Two tables:
--   1. scrape_input      ← vehicle/customer/cover parameters (input to automation)
--   2. vehicle_coverage  ← scraped premium results (output from website)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. scrape_input – flat, properly typed input rows
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrape_input (
    id                      SERIAL          PRIMARY KEY,

    -- ── Vehicle Details ────────────────────────────────────────────────────
    registration_number     TEXT            NOT NULL DEFAULT 'NEW',
    rto_location            TEXT            NOT NULL,
    make_model_variant      TEXT            NOT NULL,
    manufacturing_year      TEXT            NOT NULL,
    manufacturing_month     TEXT            NOT NULL,
    fuel_type               TEXT            NOT NULL DEFAULT 'Petrol',
    business_type           TEXT            NOT NULL DEFAULT 'New',
    customer_type           TEXT            NOT NULL DEFAULT 'Individual',
    is_new_vehicle          BOOLEAN         NOT NULL DEFAULT TRUE,
    idv                     TEXT            NOT NULL DEFAULT 'auto',
    ncb_eligibility         TEXT            NOT NULL DEFAULT 'None',
    discount_percent        INTEGER         NOT NULL DEFAULT 0,
    purchase_date           TEXT,                            -- DD-MM-YYYY
    policy_start_date       TEXT,                            -- DD-MM-YYYY
    engine_number           TEXT,
    chassis_number          TEXT,

    -- ── OD Package covers ─────────────────────────────────────────────────
    cover_basic_od                      BOOLEAN NOT NULL DEFAULT FALSE,
    cover_non_electrical_accessories    BOOLEAN NOT NULL DEFAULT FALSE,
    cover_electrical_accessories        BOOLEAN NOT NULL DEFAULT FALSE,
    cover_geo_extension                 BOOLEAN NOT NULL DEFAULT FALSE,
    cover_driving_tuition               BOOLEAN NOT NULL DEFAULT FALSE,
    cover_fibre_glass_tank              BOOLEAN NOT NULL DEFAULT FALSE,
    cover_voluntary_deductible          BOOLEAN NOT NULL DEFAULT FALSE,
    cover_anti_theft_device             BOOLEAN NOT NULL DEFAULT FALSE,
    cover_specially_designed            BOOLEAN NOT NULL DEFAULT FALSE,
    cover_automobile_association        BOOLEAN NOT NULL DEFAULT FALSE,

    -- ── Liability Covers ──────────────────────────────────────────────────
    cover_tppd                          BOOLEAN NOT NULL DEFAULT TRUE,
    cover_tppd_limit                    TEXT    NOT NULL DEFAULT '6000',
    cover_basic_liability               BOOLEAN NOT NULL DEFAULT TRUE,
    cover_pa_to_owner_driver            BOOLEAN NOT NULL DEFAULT TRUE,
    cover_cpa_tenure                    TEXT    NOT NULL DEFAULT '1',
    cover_pa_to_named_passenger         BOOLEAN NOT NULL DEFAULT FALSE,
    cover_pa_to_unnamed_passenger       BOOLEAN NOT NULL DEFAULT FALSE,
    cover_pa_to_paid_driver             BOOLEAN NOT NULL DEFAULT FALSE,
    cover_liability_to_paid_driver      BOOLEAN NOT NULL DEFAULT FALSE,
    cover_liability_to_employees        BOOLEAN NOT NULL DEFAULT FALSE,

    -- ── AddOn Covers ──────────────────────────────────────────────────────
    cover_nil_depreciation              BOOLEAN NOT NULL DEFAULT TRUE,
    cover_electric_motor_protect        BOOLEAN NOT NULL DEFAULT FALSE,
    cover_ev_battery_protection         BOOLEAN NOT NULL DEFAULT FALSE,
    cover_ev_charger_cover              BOOLEAN NOT NULL DEFAULT FALSE,
    cover_hospi_cash_30                 BOOLEAN NOT NULL DEFAULT FALSE,
    cover_hospi_cash_15                 BOOLEAN NOT NULL DEFAULT FALSE,
    cover_emergency_medical             BOOLEAN NOT NULL DEFAULT FALSE,
    cover_assistance_cover_tw_shield    BOOLEAN NOT NULL DEFAULT FALSE,
    cover_assistance_cover_tw           BOOLEAN NOT NULL DEFAULT FALSE,
    cover_assistance_cover_24x7         BOOLEAN NOT NULL DEFAULT FALSE,
    cover_key_protect                   BOOLEAN NOT NULL DEFAULT FALSE,
    cover_daily_allowance_7days         BOOLEAN NOT NULL DEFAULT FALSE,
    cover_emi_protection                BOOLEAN NOT NULL DEFAULT FALSE,
    cover_is_side_car                   BOOLEAN NOT NULL DEFAULT FALSE,

    -- ── Customer Details ──────────────────────────────────────────────────
    cust_first_name         TEXT,
    cust_middle_name        TEXT,
    cust_last_name          TEXT,
    cust_dob                TEXT,                            -- DD-MM-YYYY
    cust_pan                TEXT,
    cust_gender             TEXT            DEFAULT 'Male',
    cust_occupation         TEXT            DEFAULT 'Others',
    cust_marital_status     TEXT            DEFAULT 'Single',
    cust_mobile             TEXT,
    cust_email              TEXT,
    cust_aadhar             TEXT,
    cust_nationality        TEXT            DEFAULT 'Indian',
    cust_registered_gst     TEXT            DEFAULT 'No',
    cust_related_party      TEXT            DEFAULT 'No',

    -- ── Nominee (PA to Owner Driver) ──────────────────────────────────────
    nominee_name            TEXT,
    nominee_dob             TEXT,                            -- DD-MM-YYYY
    nominee_relation        TEXT            DEFAULT 'Father',
    nominee_age             TEXT,

    -- ── Communication Address ─────────────────────────────────────────────
    addr_flat               TEXT,
    addr_building           TEXT,
    addr_street             TEXT,
    addr_landmark           TEXT,
    addr_pincode            TEXT,
    addr_state              TEXT,
    addr_city               TEXT,
    addr_mobile             TEXT,
    addr_email              TEXT,
    addr_perm_same_as_comm  BOOLEAN         NOT NULL DEFAULT TRUE,

    -- ── Proposal flow flags ───────────────────────────────────────────────
    continue_to_buy         BOOLEAN         NOT NULL DEFAULT TRUE,
    validate_customer       BOOLEAN         NOT NULL DEFAULT TRUE,
    upload_document         BOOLEAN         NOT NULL DEFAULT TRUE,
    upload_doc_type         TEXT            DEFAULT 'Invoice_Copy',
    upload_doc_path         TEXT            DEFAULT 'sample_documents/sample_document.pdf',
    save_proposal           BOOLEAN         NOT NULL DEFAULT TRUE,
    proceed_to_payment      BOOLEAN         NOT NULL DEFAULT TRUE,

    -- ── Audit ────────────────────────────────────────────────────────────
    is_active               BOOLEAN         NOT NULL DEFAULT TRUE,
    notes                   TEXT,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Index to quickly fetch the latest active row
CREATE INDEX IF NOT EXISTS idx_scrape_input_active_created
    ON scrape_input (is_active, created_at DESC);

-- ---------------------------------------------------------------------------
-- 2. vehicle_coverage – flat scraped output (fix: no JSON for covers)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vehicle_coverage (

    -- Primary key (auto-increment)
    id                      SERIAL          PRIMARY KEY,

    -- Back-reference to the input that generated this run
    scrape_input_id         INTEGER         REFERENCES scrape_input(id),

    -- ── Vehicle / registration info ────────────────────────────────────────
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

    -- ── Policy info ────────────────────────────────────────────────────────
    policy_start_date       TEXT,
    purchase_date           TEXT,
    business_type           TEXT,
    customer_type           TEXT,
    ncb_eligibility         TEXT,
    cover_type              TEXT,

    -- ── Coverage / premium (scraped values) ───────────────────────────────
    idv                     TEXT,
    base_od_premium         TEXT,
    tp_premium              TEXT,
    gst                     TEXT,
    net_premium             TEXT,
    total_premium           TEXT,

    -- ── OD Package covers (flat boolean from website) ──────────────────────
    scraped_basic_od                    BOOLEAN,
    scraped_non_electrical_accessories  BOOLEAN,
    scraped_electrical_accessories      BOOLEAN,
    scraped_geo_extension               BOOLEAN,
    scraped_driving_tuition             BOOLEAN,
    scraped_fibre_glass_tank            BOOLEAN,
    scraped_voluntary_deductible        BOOLEAN,
    scraped_anti_theft_device           BOOLEAN,
    scraped_specially_designed          BOOLEAN,
    scraped_automobile_association      BOOLEAN,

    -- ── Liability Covers (flat boolean from website) ──────────────────────
    scraped_tppd                        BOOLEAN,
    scraped_tppd_limit                  TEXT,
    scraped_basic_liability             BOOLEAN,
    scraped_pa_to_owner_driver          BOOLEAN,
    scraped_cpa_tenure                  TEXT,
    scraped_pa_to_named_passenger       BOOLEAN,
    scraped_pa_to_unnamed_passenger     BOOLEAN,
    scraped_pa_to_paid_driver           BOOLEAN,
    scraped_liability_to_paid_driver    BOOLEAN,
    scraped_liability_to_employees      BOOLEAN,

    -- ── AddOn Covers (flat boolean from website) ──────────────────────────
    scraped_nil_depreciation            BOOLEAN,
    scraped_electric_motor_protect      BOOLEAN,
    scraped_ev_battery_protection       BOOLEAN,
    scraped_ev_charger_cover            BOOLEAN,
    scraped_hospi_cash_30               BOOLEAN,
    scraped_hospi_cash_15               BOOLEAN,
    scraped_emergency_medical           BOOLEAN,
    scraped_assistance_cover_tw_shield  BOOLEAN,
    scraped_assistance_cover_tw         BOOLEAN,
    scraped_assistance_cover_24x7       BOOLEAN,
    scraped_key_protect                 BOOLEAN,
    scraped_daily_allowance_7days       BOOLEAN,
    scraped_emi_protection              BOOLEAN,
    scraped_is_side_car                 BOOLEAN,

    -- ── Proposal identifiers ──────────────────────────────────────────────
    proposal_number         TEXT,
    trace_id                TEXT,

    -- ── Raw backup (keep for diagnostics, not primary storage) ────────────
    extra_fields            JSONB           DEFAULT '{}',

    -- ── Audit ────────────────────────────────────────────────────────────
    scraped_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (registration_number, policy_start_date)
);

CREATE INDEX IF NOT EXISTS idx_vehicle_coverage_scraped_at
    ON vehicle_coverage (scraped_at DESC);
