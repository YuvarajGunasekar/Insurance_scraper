"""
db/scrape_input_client.py  (Unified Clean Relational Client)
===========================================================
Reads and writes the unified 7 relational tables directly.
No duplicate tables.

Public API:
-----------
  save_scrape_input(data: dict) -> int          insert/update quote across tables, return quote_id
  get_latest_scrape_input()     -> dict | None  newest is_active=TRUE row (from view)
  get_scrape_input_by_id(id)    -> dict | None  specific quote by ID
  list_scrape_inputs(limit)     -> list[dict]   recent quotes summary
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Generator

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logger = logging.getLogger("scrape_api")


def _pg_config() -> dict:
    return {
        "host":     os.getenv("PG_HOST",     "localhost"),
        "port":     int(os.getenv("PG_PORT", "5432")),
        "dbname":   os.getenv("PG_DB",       "insurance"),
        "user":     os.getenv("PG_USER",     "postgres"),
        "password": os.getenv("PG_PASSWORD", ""),
    }


@contextmanager
def _conn() -> Generator[psycopg2.extensions.connection, None, None]:
    c = psycopg2.connect(**_pg_config())
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


_REQUIRED_FIELDS = (
    "registration_number", "policy_start_date", "purchase_date",
    "rto_location", "make_model_variant", "manufacturing_year",
    "manufacturing_month", "fuel_type", "business_type", "customer_type",
    "is_new_vehicle", "idv", "ncb_eligibility", "discount_percent",
    "cover_basic_od", "cover_non_electrical_accessories",
    "cover_electrical_accessories", "cover_geo_extension",
    "cover_driving_tuition", "cover_fibre_glass_tank",
    "cover_voluntary_deductible", "cover_anti_theft_device",
    "cover_specially_designed", "cover_automobile_association",
    "cover_tppd", "cover_tppd_limit", "cover_basic_liability",
    "cover_pa_to_owner_driver", "cover_cpa_tenure",
    "cover_pa_to_named_passenger", "cover_pa_to_unnamed_passenger",
    "cover_pa_to_paid_driver", "cover_liability_to_paid_driver",
    "cover_liability_to_employees", "cover_nil_depreciation",
    "cover_electric_motor_protect", "cover_ev_battery_protection",
    "cover_ev_charger_cover", "cover_hospi_cash_30", "cover_hospi_cash_15",
    "cover_emergency_medical", "cover_assistance_cover_tw_shield",
    "cover_assistance_cover_tw", "cover_assistance_cover_24x7",
    "cover_key_protect", "cover_daily_allowance_7days",
    "cover_emi_protection", "cover_is_side_car",
    "addr_perm_same_as_comm", "continue_to_buy", "validate_customer",
    "upload_document", "save_proposal",
    "proceed_to_payment", "click_get_coverage_details",
    "click_calculate_premium", "is_active",
)

_BOOLEAN_FIELDS = (
    "is_new_vehicle", "cover_basic_od", "cover_non_electrical_accessories",
    "cover_electrical_accessories", "cover_geo_extension",
    "cover_driving_tuition", "cover_fibre_glass_tank",
    "cover_voluntary_deductible", "cover_anti_theft_device",
    "cover_specially_designed", "cover_automobile_association", "cover_tppd",
    "cover_basic_liability", "cover_pa_to_owner_driver",
    "cover_pa_to_named_passenger", "cover_pa_to_unnamed_passenger",
    "cover_pa_to_paid_driver", "cover_liability_to_paid_driver",
    "cover_liability_to_employees", "cover_nil_depreciation",
    "cover_electric_motor_protect", "cover_ev_battery_protection",
    "cover_ev_charger_cover", "cover_hospi_cash_30", "cover_hospi_cash_15",
    "cover_emergency_medical", "cover_assistance_cover_tw_shield",
    "cover_assistance_cover_tw", "cover_assistance_cover_24x7",
    "cover_key_protect", "cover_daily_allowance_7days", "cover_emi_protection",
    "cover_is_side_car", "addr_perm_same_as_comm", "continue_to_buy",
    "validate_customer", "upload_document", "save_proposal",
    "proceed_to_payment", "click_get_coverage_details",
    "click_calculate_premium", "is_active",
)


def save_scrape_input(data: dict) -> int:
    """Persist explicitly supplied values; never synthesize scrape inputs."""
    row = dict(data)
    missing = [
        key for key in _REQUIRED_FIELDS
        if key not in row or row[key] is None
        or (isinstance(row[key], str) and not row[key].strip())
    ]
    if missing:
        raise ValueError(
            "Missing required scrape-input values: " + ", ".join(missing)
        )
    invalid_booleans = [
        key for key in _BOOLEAN_FIELDS if not isinstance(row[key], bool)
    ]
    if invalid_booleans:
        raise ValueError(
            "Scrape-input values must be booleans: "
            + ", ".join(invalid_booleans)
        )
    nil_depreciation_first_time = row.get("cover_nil_depreciation_first_time")
    if row["cover_nil_depreciation"] and not isinstance(
        nil_depreciation_first_time, bool
    ):
        raise ValueError(
            "cover_nil_depreciation_first_time is required when "
            "cover_nil_depreciation is true."
        )
    if nil_depreciation_first_time is not None and not isinstance(
        nil_depreciation_first_time, bool
    ):
        raise ValueError("cover_nil_depreciation_first_time must be a boolean.")
    proposal_actions = (
        row["upload_document"], row["save_proposal"], row["proceed_to_payment"]
    )
    if any(proposal_actions) and not row["continue_to_buy"]:
        raise ValueError(
            "Upload, save, and payment actions require continue_to_buy to be true."
        )
    if any(proposal_actions) and not row["validate_customer"]:
        raise ValueError(
            "Upload, save, and payment actions require validate_customer to be true."
        )
    if row["continue_to_buy"] and not row["click_get_coverage_details"]:
        raise ValueError(
            "continue_to_buy requires click_get_coverage_details to be true."
        )
    if row["continue_to_buy"] and not row["click_calculate_premium"]:
        raise ValueError(
            "continue_to_buy requires click_calculate_premium to be true."
        )
    if row["proceed_to_payment"] and not row["save_proposal"]:
        raise ValueError(
            "proceed_to_payment requires save_proposal to be true."
        )
    if row["continue_to_buy"]:
        proposal_required = ("cust_first_name", "cust_last_name")
        missing_proposal = [
            key for key in proposal_required
            if not row.get(key) or not str(row[key]).strip()
        ]
        if missing_proposal:
            raise ValueError(
                "Missing required proposal DB values: " + ", ".join(missing_proposal)
            )
        if row["validate_customer"]:
            vehicle_required = ("engine_number", "chassis_number")
            missing_vehicle = [
                key for key in vehicle_required
                if not row.get(key) or not str(row[key]).strip()
            ]
            if missing_vehicle:
                raise ValueError(
                    "Missing required proposal DB values: "
                    + ", ".join(missing_vehicle)
                )
        if row["validate_customer"] and row["cover_pa_to_owner_driver"]:
            nominee_required = (
                "nominee_name", "nominee_dob", "nominee_relation", "nominee_age"
            )
            missing_nominee = [
                key for key in nominee_required
                if not row.get(key) or not str(row[key]).strip()
            ]
            if missing_nominee:
                raise ValueError(
                    "Missing required nominee DB values: "
                    + ", ".join(missing_nominee)
                )
    if row["continue_to_buy"] and row["upload_document"]:
        for key in ("upload_doc_type", "upload_doc_path"):
            if not row.get(key) or not str(row[key]).strip():
                raise ValueError(f"{key} is required when upload_document is true.")
        document_path = Path(row["upload_doc_path"])
        if not document_path.is_absolute():
            document_path = Path(__file__).resolve().parents[1] / document_path
        if not document_path.is_file():
            raise ValueError(
                f"Document configured in the DB row does not exist: {document_path}"
            )

    reg_num = row["registration_number"]
    pol_start = row["policy_start_date"]

    with _conn() as c:
        with c.cursor() as cur:
            # 1. Master Table: insurance_quote
            cur.execute("""
                INSERT INTO insurance_quote (
                    registration_number, policy_start_date, purchase_date,
                    business_type, customer_type, continue_to_buy, validate_customer,
                    upload_document, upload_doc_type, upload_doc_path, save_proposal,
                    proceed_to_payment, click_get_coverage_details,
                    click_calculate_premium, is_active, notes, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
                )
                ON CONFLICT (registration_number, policy_start_date)
                DO UPDATE SET
                    purchase_date = EXCLUDED.purchase_date,
                    business_type = EXCLUDED.business_type,
                    customer_type = EXCLUDED.customer_type,
                    continue_to_buy = EXCLUDED.continue_to_buy,
                    validate_customer = EXCLUDED.validate_customer,
                    upload_document = EXCLUDED.upload_document,
                    upload_doc_type = EXCLUDED.upload_doc_type,
                    upload_doc_path = EXCLUDED.upload_doc_path,
                    save_proposal = EXCLUDED.save_proposal,
                    proceed_to_payment = EXCLUDED.proceed_to_payment,
                    click_get_coverage_details = EXCLUDED.click_get_coverage_details,
                    click_calculate_premium = EXCLUDED.click_calculate_premium,
                    is_active = EXCLUDED.is_active,
                    notes = EXCLUDED.notes,
                    updated_at = NOW()
                RETURNING id;
            """, (
                reg_num, pol_start,                 row["purchase_date"],
                row["business_type"], row["customer_type"],
                row["continue_to_buy"], row["validate_customer"],
                row["upload_document"], row["upload_doc_type"],
                row["upload_doc_path"], row["save_proposal"],
                row["proceed_to_payment"], row["click_get_coverage_details"],
                row["click_calculate_premium"], row["is_active"],
                row.get("notes"),
            ))
            quote_id = cur.fetchone()[0]

            # 2. vehicle_details
            cur.execute("""
                INSERT INTO vehicle_details (
                    quote_id, registration_number, rto_location, make_model_variant,
                    vehicle_make_model, fuel_type, manufacturing_year, manufacturing_month, idv,
                    engine_number, chassis_number, is_new_vehicle, ncb_eligibility, discount_percent
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    registration_number = EXCLUDED.registration_number,
                    rto_location = EXCLUDED.rto_location,
                    make_model_variant = EXCLUDED.make_model_variant,
                    vehicle_make_model = EXCLUDED.vehicle_make_model,
                    fuel_type = EXCLUDED.fuel_type,
                    manufacturing_year = EXCLUDED.manufacturing_year,
                    manufacturing_month = EXCLUDED.manufacturing_month,
                    idv = EXCLUDED.idv,
                    engine_number = EXCLUDED.engine_number,
                    chassis_number = EXCLUDED.chassis_number,
                    is_new_vehicle = EXCLUDED.is_new_vehicle,
                    ncb_eligibility = EXCLUDED.ncb_eligibility,
                    discount_percent = EXCLUDED.discount_percent;
            """, (
                quote_id, reg_num, row["rto_location"], row["make_model_variant"],
                row.get("vehicle_make_model"), row["fuel_type"], row["manufacturing_year"],
                row["manufacturing_month"], row["idv"],
                row.get("engine_number"), row.get("chassis_number"),
                row["is_new_vehicle"], row["ncb_eligibility"],
                row["discount_percent"],
            ))

            # 3. cover_details
            cur.execute("""
                INSERT INTO cover_details (
                    quote_id, basic_od, non_electrical_accessories, electrical_accessories,
                    geo_extension, driving_tuition, fibre_glass_tank, voluntary_deductible,
                    anti_theft_device, specially_designed, automobile_association,
                    tppd, tppd_limit, basic_liability, pa_to_owner_driver, cpa_tenure,
                    pa_to_named_passenger, pa_to_unnamed_passenger, pa_to_paid_driver,
                    liability_to_paid_driver,                     liability_to_employees, nil_depreciation,
                    nil_depreciation_first_time,
                    electric_motor_protect, ev_battery_protection, ev_charger_cover,
                    hospi_cash_30, hospi_cash_15, emergency_medical,
                    assistance_cover_tw_shield, assistance_cover_tw, assistance_cover_24x7,
                    key_protect, daily_allowance_7days, emi_protection, is_side_car
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (quote_id) DO UPDATE SET
                    basic_od = EXCLUDED.basic_od,
                    non_electrical_accessories = EXCLUDED.non_electrical_accessories,
                    electrical_accessories = EXCLUDED.electrical_accessories,
                    geo_extension = EXCLUDED.geo_extension,
                    driving_tuition = EXCLUDED.driving_tuition,
                    fibre_glass_tank = EXCLUDED.fibre_glass_tank,
                    voluntary_deductible = EXCLUDED.voluntary_deductible,
                    anti_theft_device = EXCLUDED.anti_theft_device,
                    specially_designed = EXCLUDED.specially_designed,
                    automobile_association = EXCLUDED.automobile_association,
                    tppd = EXCLUDED.tppd,
                    tppd_limit = EXCLUDED.tppd_limit,
                    basic_liability = EXCLUDED.basic_liability,
                    pa_to_owner_driver = EXCLUDED.pa_to_owner_driver,
                    cpa_tenure = EXCLUDED.cpa_tenure,
                    pa_to_named_passenger = EXCLUDED.pa_to_named_passenger,
                    pa_to_unnamed_passenger = EXCLUDED.pa_to_unnamed_passenger,
                    pa_to_paid_driver = EXCLUDED.pa_to_paid_driver,
                    liability_to_paid_driver = EXCLUDED.liability_to_paid_driver,
                    liability_to_employees = EXCLUDED.liability_to_employees,
                    nil_depreciation = EXCLUDED.nil_depreciation,
                    nil_depreciation_first_time = EXCLUDED.nil_depreciation_first_time,
                    electric_motor_protect = EXCLUDED.electric_motor_protect,
                    ev_battery_protection = EXCLUDED.ev_battery_protection,
                    ev_charger_cover = EXCLUDED.ev_charger_cover,
                    hospi_cash_30 = EXCLUDED.hospi_cash_30,
                    hospi_cash_15 = EXCLUDED.hospi_cash_15,
                    emergency_medical = EXCLUDED.emergency_medical,
                    assistance_cover_tw_shield = EXCLUDED.assistance_cover_tw_shield,
                    assistance_cover_tw = EXCLUDED.assistance_cover_tw,
                    assistance_cover_24x7 = EXCLUDED.assistance_cover_24x7,
                    key_protect = EXCLUDED.key_protect,
                    daily_allowance_7days = EXCLUDED.daily_allowance_7days,
                    emi_protection = EXCLUDED.emi_protection,
                    is_side_car = EXCLUDED.is_side_car;
            """, (
                quote_id,
                row["cover_basic_od"], row["cover_non_electrical_accessories"],
                row["cover_electrical_accessories"], row["cover_geo_extension"],
                row["cover_driving_tuition"], row["cover_fibre_glass_tank"],
                row["cover_voluntary_deductible"], row["cover_anti_theft_device"],
                row["cover_specially_designed"], row["cover_automobile_association"],
                row["cover_tppd"], row["cover_tppd_limit"],
                row["cover_basic_liability"], row["cover_pa_to_owner_driver"],
                row["cover_cpa_tenure"], row["cover_pa_to_named_passenger"],
                row["cover_pa_to_unnamed_passenger"], row["cover_pa_to_paid_driver"],
                row["cover_liability_to_paid_driver"], row["cover_liability_to_employees"],
                row["cover_nil_depreciation"], row.get("cover_nil_depreciation_first_time"),
                row["cover_electric_motor_protect"],
                row["cover_ev_battery_protection"], row["cover_ev_charger_cover"],
                row["cover_hospi_cash_30"], row["cover_hospi_cash_15"],
                row["cover_emergency_medical"], row["cover_assistance_cover_tw_shield"],
                row["cover_assistance_cover_tw"], row["cover_assistance_cover_24x7"],
                row["cover_key_protect"], row["cover_daily_allowance_7days"],
                row["cover_emi_protection"], row["cover_is_side_car"],
            ))

            # 4. customer_details
            cur.execute("""
                INSERT INTO customer_details (
                    quote_id, first_name, middle_name, last_name, dob, pan, gender,
                    occupation, marital_status, mobile, email, aadhar, nationality,
                    registered_gst, related_party
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    middle_name = EXCLUDED.middle_name,
                    last_name = EXCLUDED.last_name,
                    dob = EXCLUDED.dob,
                    pan = EXCLUDED.pan,
                    gender = EXCLUDED.gender,
                    occupation = EXCLUDED.occupation,
                    marital_status = EXCLUDED.marital_status,
                    mobile = EXCLUDED.mobile,
                    email = EXCLUDED.email,
                    aadhar = EXCLUDED.aadhar,
                    nationality = EXCLUDED.nationality,
                    registered_gst = EXCLUDED.registered_gst,
                    related_party = EXCLUDED.related_party
                RETURNING id;
            """, (
                quote_id, row.get("cust_first_name"), row.get("cust_middle_name"),
                row.get("cust_last_name"), row.get("cust_dob"), row.get("cust_pan"),
                row.get("cust_gender"), row.get("cust_occupation"),
                row.get("cust_marital_status"), row.get("cust_mobile"),
                row.get("cust_email"), row.get("cust_aadhar"), row.get("cust_nationality"),
                row.get("cust_registered_gst"), row.get("cust_related_party"),
            ))
            cust_tbl_id = cur.fetchone()[0]

            # 5. nominee_details
            cur.execute("""
                INSERT INTO nominee_details (
                    quote_id, customer_id, nominee_name, nominee_dob, nominee_relation, nominee_age
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    nominee_name = EXCLUDED.nominee_name,
                    nominee_dob = EXCLUDED.nominee_dob,
                    nominee_relation = EXCLUDED.nominee_relation,
                    nominee_age = EXCLUDED.nominee_age;
            """, (
                quote_id, cust_tbl_id, row.get("nominee_name"), row.get("nominee_dob"),
                row.get("nominee_relation"), row.get("nominee_age"),
            ))

            # 6. address_details
            cur.execute("""
                INSERT INTO address_details (
                    quote_id, comm_flat, comm_building, comm_street, comm_landmark,
                    comm_pincode, comm_state, comm_city, comm_mobile, comm_email, perm_same_as_comm,
                    perm_flat, perm_building, perm_street, perm_landmark, perm_pincode, perm_state, perm_city
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    comm_flat = EXCLUDED.comm_flat,
                    comm_building = EXCLUDED.comm_building,
                    comm_street = EXCLUDED.comm_street,
                    comm_landmark = EXCLUDED.comm_landmark,
                    comm_mobile = EXCLUDED.comm_mobile,
                    comm_email = EXCLUDED.comm_email,
                    comm_city = EXCLUDED.comm_city,
                    comm_state = EXCLUDED.comm_state,
                    comm_pincode = EXCLUDED.comm_pincode,
                    perm_same_as_comm = EXCLUDED.perm_same_as_comm,
                    perm_flat = EXCLUDED.perm_flat,
                    perm_building = EXCLUDED.perm_building,
                    perm_street = EXCLUDED.perm_street,
                    perm_landmark = EXCLUDED.perm_landmark,
                    perm_pincode = EXCLUDED.perm_pincode,
                    perm_state = EXCLUDED.perm_state,
                    perm_city = EXCLUDED.perm_city;
            """, (
                quote_id, row.get("addr_flat"), row.get("addr_building"), row.get("addr_street"),
                row.get("addr_landmark"), row.get("addr_pincode"), row.get("addr_state"),
                row.get("addr_city"), row.get("addr_mobile"), row.get("addr_email"),
                row["addr_perm_same_as_comm"],
                row.get("perm_flat"), row.get("perm_building"), row.get("perm_street"),
                row.get("perm_landmark"), row.get("perm_pincode"), row.get("perm_state"),
                row.get("perm_city"),
            ))

    logger.info("[OK] Saved quote input configuration | quote_id=%d", quote_id)
    return quote_id


def get_latest_scrape_input() -> dict | None:
    """Return the latest active quote configuration without modifying DB values."""
    sql = """
        SELECT * FROM v_complete_insurance_quote
        WHERE is_active = TRUE
        ORDER BY updated_at DESC, quote_id DESC
        LIMIT 1;
    """
    try:
        with _conn() as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as exc:
        logger.exception("get_latest_scrape_input failed.")
        raise


def get_scrape_input_by_id(quote_id: int) -> dict | None:
    """Return a specific quote from the master view."""
    sql = "SELECT * FROM v_complete_insurance_quote WHERE quote_id = %s;"
    try:
        with _conn() as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (quote_id,))
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as exc:
        logger.warning("get_scrape_input_by_id(%s) failed: %s", quote_id, exc)
        return None


def list_scrape_inputs(limit: int = 20) -> list[dict]:
    """List recent quotes summary."""
    sql = """
        SELECT
            q.id, q.is_active, q.notes, q.created_at, q.updated_at,
            v.registration_number, v.make_model_variant,
            v.rto_location, v.manufacturing_year, v.manufacturing_month,
            v.fuel_type, q.policy_start_date
        FROM insurance_quote q
        LEFT JOIN vehicle_details v ON v.quote_id = q.id
        ORDER BY q.created_at DESC
        LIMIT %s;
    """
    try:
        with _conn() as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (limit,))
                return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        logger.warning("list_scrape_inputs failed: %s", exc)
        return []
