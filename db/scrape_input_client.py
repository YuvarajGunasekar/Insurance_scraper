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
from datetime import date
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


_DEFAULTS: dict = {
    # Flow
    "continue_to_buy": True, "validate_customer": True,
    "upload_document": True, "upload_doc_type": "Invoice_Copy",
    "upload_doc_path": "sample_documents/sample_document.pdf",
    "save_proposal": True, "proceed_to_payment": True,
    "is_active": True, "notes": None,
    # Vehicle
    "registration_number": "NEW", "rto_location": "", "make_model_variant": "",
    "vehicle_make_model": "", "manufacturing_year": "", "manufacturing_month": "",
    "fuel_type": "Petrol", "business_type": "New", "customer_type": "Individual",
    "is_new_vehicle": True, "idv": "auto", "ncb_eligibility": "None", "discount_percent": 0,
    "purchase_date": None, "policy_start_date": None,
    "engine_number": None, "chassis_number": None,
    # Covers
    "cover_basic_od": False, "cover_non_electrical_accessories": False,
    "cover_electrical_accessories": False, "cover_geo_extension": False,
    "cover_driving_tuition": False, "cover_fibre_glass_tank": False,
    "cover_voluntary_deductible": False, "cover_anti_theft_device": False,
    "cover_specially_designed": False, "cover_automobile_association": False,
    "cover_tppd": True, "cover_tppd_limit": "6000", "cover_basic_liability": True,
    "cover_pa_to_owner_driver": True, "cover_cpa_tenure": "1",
    "cover_pa_to_named_passenger": False, "cover_pa_to_unnamed_passenger": False,
    "cover_pa_to_paid_driver": False, "cover_liability_to_paid_driver": False,
    "cover_liability_to_employees": False, "cover_nil_depreciation": True,
    "cover_electric_motor_protect": False, "cover_ev_battery_protection": False,
    "cover_ev_charger_cover": False, "cover_hospi_cash_30": False,
    "cover_hospi_cash_15": False, "cover_emergency_medical": False,
    "cover_assistance_cover_tw_shield": False, "cover_assistance_cover_tw": False,
    "cover_assistance_cover_24x7": False, "cover_key_protect": False,
    "cover_daily_allowance_7days": False, "cover_emi_protection": False,
    "cover_is_side_car": False,
    # Customer
    "cust_first_name": None, "cust_middle_name": None, "cust_last_name": None,
    "cust_dob": None, "cust_pan": None, "cust_gender": "Male",
    "cust_occupation": "Others", "cust_marital_status": "Single",
    "cust_mobile": None, "cust_email": None, "cust_aadhar": None,
    "cust_nationality": "Indian", "cust_registered_gst": "No", "cust_related_party": "No",
    # Nominee
    "nominee_name": None, "nominee_dob": None, "nominee_relation": "Father", "nominee_age": None,
    # Address
    "addr_flat": None, "addr_building": None, "addr_street": None, "addr_landmark": None,
    "addr_pincode": None, "addr_state": None, "addr_city": None,
    "addr_mobile": None, "addr_email": None, "addr_perm_same_as_comm": True,
    "perm_flat": None, "perm_building": None, "perm_street": None, "perm_landmark": None,
    "perm_pincode": None, "perm_state": None, "perm_city": None,
}


def save_scrape_input(data: dict) -> int:
    """Save an input configuration directly into the 7 canonical relational tables."""
    today = date.today().strftime("%d-%m-%Y")
    row = {**_DEFAULTS, **data}
    if not row.get("purchase_date"):
        row["purchase_date"] = today
    if not row.get("policy_start_date"):
        row["policy_start_date"] = today
    if not row.get("manufacturing_year"):
        row["manufacturing_year"] = str(date.today().year)
    if not row.get("manufacturing_month"):
        row["manufacturing_month"] = date.today().strftime("%B").upper()

    if not row.get("make_model_variant") or any(bad in str(row.get("make_model_variant", "")).lower() for bad in ("no record", "no data")):
        row["make_model_variant"] = os.getenv("VEH_MAKE_MODEL_VARIANT", "TVS JUPITER")
    if not row.get("vehicle_make_model") or any(bad in str(row.get("vehicle_make_model", "")).lower() for bad in ("no record", "no data")):
        row["vehicle_make_model"] = row["make_model_variant"]

    reg_num = row.get("registration_number") or "NEW"
    pol_start = row.get("policy_start_date") or today

    with _conn() as c:
        with c.cursor() as cur:
            # 1. Master Table: insurance_quote
            cur.execute("""
                INSERT INTO insurance_quote (
                    registration_number, policy_start_date, purchase_date,
                    business_type, customer_type, continue_to_buy, validate_customer,
                    upload_document, upload_doc_type, upload_doc_path, save_proposal,
                    proceed_to_payment, is_active, notes, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
                )
                ON CONFLICT (registration_number, policy_start_date)
                DO UPDATE SET
                    business_type = EXCLUDED.business_type,
                    customer_type = EXCLUDED.customer_type,
                    continue_to_buy = EXCLUDED.continue_to_buy,
                    validate_customer = EXCLUDED.validate_customer,
                    is_active = EXCLUDED.is_active,
                    notes = EXCLUDED.notes,
                    updated_at = NOW()
                RETURNING id;
            """, (
                reg_num, pol_start, row.get("purchase_date"),
                row.get("business_type", "New"), row.get("customer_type", "Individual"),
                row.get("continue_to_buy", True), row.get("validate_customer", True),
                row.get("upload_document", True), row.get("upload_doc_type", "Invoice_Copy"),
                row.get("upload_doc_path"), row.get("save_proposal", True),
                row.get("proceed_to_payment", True), row.get("is_active", True),
                row.get("notes"),
            ))
            quote_id = cur.fetchone()[0]

            # 2. vehicle_details
            cur.execute("""
                INSERT INTO vehicle_details (
                    quote_id, registration_number, rto_location, make_model_variant,
                    vehicle_make_model, fuel_type, manufacturing_year, manufacturing_month,
                    engine_number, chassis_number, is_new_vehicle, ncb_eligibility, discount_percent
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    rto_location = EXCLUDED.rto_location,
                    make_model_variant = EXCLUDED.make_model_variant,
                    fuel_type = EXCLUDED.fuel_type,
                    manufacturing_year = EXCLUDED.manufacturing_year,
                    manufacturing_month = EXCLUDED.manufacturing_month,
                    engine_number = COALESCE(EXCLUDED.engine_number, vehicle_details.engine_number),
                    chassis_number = COALESCE(EXCLUDED.chassis_number, vehicle_details.chassis_number),
                    is_new_vehicle = EXCLUDED.is_new_vehicle;
            """, (
                quote_id, reg_num, row.get("rto_location", ""), row.get("make_model_variant", ""),
                row.get("vehicle_make_model") or row.get("make_model_variant", ""),
                row.get("fuel_type", "Petrol"), row.get("manufacturing_year", ""), row.get("manufacturing_month", ""),
                row.get("engine_number"), row.get("chassis_number"),
                row.get("is_new_vehicle", True), row.get("ncb_eligibility", "None"),
                int(row.get("discount_percent", 0) or 0),
            ))

            # 3. cover_details
            cur.execute("""
                INSERT INTO cover_details (
                    quote_id, basic_od, non_electrical_accessories, electrical_accessories,
                    geo_extension, driving_tuition, fibre_glass_tank, voluntary_deductible,
                    anti_theft_device, specially_designed, automobile_association,
                    tppd, tppd_limit, basic_liability, pa_to_owner_driver, cpa_tenure,
                    pa_to_named_passenger, pa_to_unnamed_passenger, pa_to_paid_driver,
                    liability_to_paid_driver, liability_to_employees, nil_depreciation,
                    electric_motor_protect, ev_battery_protection, ev_charger_cover,
                    hospi_cash_30, hospi_cash_15, emergency_medical,
                    assistance_cover_tw_shield, assistance_cover_tw, assistance_cover_24x7,
                    key_protect, daily_allowance_7days, emi_protection, is_side_car
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (quote_id) DO UPDATE SET
                    basic_od = EXCLUDED.basic_od,
                    tppd = EXCLUDED.tppd,
                    tppd_limit = EXCLUDED.tppd_limit,
                    basic_liability = EXCLUDED.basic_liability,
                    pa_to_owner_driver = EXCLUDED.pa_to_owner_driver,
                    cpa_tenure = EXCLUDED.cpa_tenure,
                    nil_depreciation = EXCLUDED.nil_depreciation;
            """, (
                quote_id,
                bool(row.get("cover_basic_od")), bool(row.get("cover_non_electrical_accessories")),
                bool(row.get("cover_electrical_accessories")), bool(row.get("cover_geo_extension")),
                bool(row.get("cover_driving_tuition")), bool(row.get("cover_fibre_glass_tank")),
                bool(row.get("cover_voluntary_deductible")), bool(row.get("cover_anti_theft_device")),
                bool(row.get("cover_specially_designed")), bool(row.get("cover_automobile_association")),
                bool(row.get("cover_tppd", True)), str(row.get("cover_tppd_limit", "6000")),
                bool(row.get("cover_basic_liability", True)), bool(row.get("cover_pa_to_owner_driver", True)),
                str(row.get("cover_cpa_tenure", "1")),
                bool(row.get("cover_pa_to_named_passenger")), bool(row.get("cover_pa_to_unnamed_passenger")),
                bool(row.get("cover_pa_to_paid_driver")), bool(row.get("cover_liability_to_paid_driver")),
                bool(row.get("cover_liability_to_employees")), bool(row.get("cover_nil_depreciation", True)),
                bool(row.get("cover_electric_motor_protect")), bool(row.get("cover_ev_battery_protection")),
                bool(row.get("cover_ev_charger_cover")), bool(row.get("cover_hospi_cash_30")),
                bool(row.get("cover_hospi_cash_15")), bool(row.get("cover_emergency_medical")),
                bool(row.get("cover_assistance_cover_tw_shield")), bool(row.get("cover_assistance_cover_tw")),
                bool(row.get("cover_assistance_cover_24x7")), bool(row.get("cover_key_protect")),
                bool(row.get("cover_daily_allowance_7days")), bool(row.get("cover_emi_protection")),
                bool(row.get("cover_is_side_car")),
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
                    last_name = EXCLUDED.last_name,
                    dob = EXCLUDED.dob,
                    pan = EXCLUDED.pan,
                    mobile = EXCLUDED.mobile,
                    email = EXCLUDED.email
                RETURNING id;
            """, (
                quote_id, row.get("cust_first_name"), row.get("cust_middle_name"),
                row.get("cust_last_name"), row.get("cust_dob"), row.get("cust_pan"),
                row.get("cust_gender", "Male"), row.get("cust_occupation", "Others"),
                row.get("cust_marital_status", "Single"), row.get("cust_mobile"),
                row.get("cust_email"), row.get("cust_aadhar"), row.get("cust_nationality", "Indian"),
                row.get("cust_registered_gst", "No"), row.get("cust_related_party", "No"),
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
                row.get("nominee_relation", "Father"), row.get("nominee_age"),
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
                    comm_city = EXCLUDED.comm_city,
                    comm_state = EXCLUDED.comm_state,
                    comm_pincode = EXCLUDED.comm_pincode;
            """, (
                quote_id, row.get("addr_flat"), row.get("addr_building"), row.get("addr_street"),
                row.get("addr_landmark"), row.get("addr_pincode"), row.get("addr_state"),
                row.get("addr_city"), row.get("addr_mobile"), row.get("addr_email"),
                bool(row.get("addr_perm_same_as_comm", True)),
                row.get("perm_flat"), row.get("perm_building"), row.get("perm_street"),
                row.get("perm_landmark"), row.get("perm_pincode"), row.get("perm_state"),
                row.get("perm_city"),
            ))

    logger.info("[OK] Saved quote input configuration | quote_id=%d", quote_id)
    return quote_id


def get_latest_scrape_input() -> dict | None:
    """Return the latest active quote configuration as a dict from the master view."""
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
                if row:
                    res = dict(row)
                    mm = str(res.get("make_model_variant") or "")
                    if any(bad in mm.lower() for bad in ("no record", "no data")):
                        res["make_model_variant"] = os.getenv("VEH_MAKE_MODEL_VARIANT", "TVS JUPITER")
                    vmm = str(res.get("vehicle_make_model") or "")
                    if any(bad in vmm.lower() for bad in ("no record", "no data")):
                        res["vehicle_make_model"] = res["make_model_variant"]
                    return res
                return None
    except Exception as exc:
        logger.warning("get_latest_scrape_input failed: %s", exc)
        return None


def get_scrape_input_by_id(quote_id: int) -> dict | None:
    """Return a specific quote from the master view."""
    sql = "SELECT * FROM v_complete_insurance_quote WHERE quote_id = %s;"
    try:
        with _conn() as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (quote_id,))
                row = cur.fetchone()
                if row:
                    res = dict(row)
                    mm = str(res.get("make_model_variant") or "")
                    if any(bad in mm.lower() for bad in ("no record", "no data")):
                        res["make_model_variant"] = os.getenv("VEH_MAKE_MODEL_VARIANT", "TVS JUPITER")
                    vmm = str(res.get("vehicle_make_model") or "")
                    if any(bad in vmm.lower() for bad in ("no record", "no data")):
                        res["vehicle_make_model"] = res["make_model_variant"]
                    return res
                return None
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
