"""
db/postgres_client.py  (v4 — Full Relational Storage)
=====================================================
Saves scraped CoverageDetails across the 7 normalized relational tables:
  1. insurance_quote   - Master quote/session table
  2. vehicle_details   - Vehicle Details section (FK: quote_id)
  3. cover_details     - Cover Details section (FK: quote_id)
  4. customer_details  - Customer Details section (FK: quote_id)
  5. nominee_details   - Nominee Details section (FK: quote_id, customer_id)
  6. address_details   - Communication & Permanent Address (FK: quote_id)
  7. premium_details   - Scraped Premium Summary (FK: quote_id)

Also updates the legacy `vehicle_coverage` table for backward compatibility.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Generator

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PgConnection

from pages.vehicle_details_page import CoverageDetails

logger = logging.getLogger("scrape_api")
TABLE_NAME = "vehicle_coverage"


def _pg_config() -> dict:
    return {
        "host":     os.getenv("PG_HOST",     "localhost"),
        "port":     int(os.getenv("PG_PORT", "5432")),
        "dbname":   os.getenv("PG_DB",       "insurance"),
        "user":     os.getenv("PG_USER",     "postgres"),
        "password": os.getenv("PG_PASSWORD", ""),
    }


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


def ensure_table() -> None:
    """Auto-create all relational tables and migrate schema. Called on every startup."""
    from db.init_db import setup_database
    setup_database()
    logger.info("[OK] Normalized database schema is ready.")


# ---------------------------------------------------------------------------
# Cover Flag Parsing Helpers
# ---------------------------------------------------------------------------
def _parse_cover_flags(json_str: str | None, name_map: dict) -> dict:
    result = {}
    if not json_str:
        return result
    try:
        items = json.loads(json_str) if isinstance(json_str, str) else json_str
    except Exception:
        return result
    for item in items:
        name = (item.get("name") or "").strip()
        checked = item.get("checked")
        if not isinstance(checked, bool):
            continue
        for src_name, col_name in name_map.items():
            if src_name.lower() in name.lower():
                result[col_name] = checked
                break
    return result


_OD_COVER_MAP = {
    "Basic OD":                       "basic_od",
    "Non Electrical":                 "non_electrical_accessories",
    "Electrical Acc":                 "electrical_accessories",
    "Geo Extension":                  "geo_extension",
    "Driving Tut":                    "driving_tuition",
    "Fibre Glass":                    "fibre_glass_tank",
    "Voluntary Deductible":           "voluntary_deductible",
    "Anti-Theft":                     "anti_theft_device",
    "Specially Designed":             "specially_designed",
    "Automobile Association":         "automobile_association",
}

_LIABILITY_COVER_MAP = {
    "TPPD":                           "tppd",
    "Basic Liability":                "basic_liability",
    "PA to Owner":                    "pa_to_owner_driver",
    "PA to Named Passenger":          "pa_to_named_passenger",
    "PA to Unnamed Passenger":        "pa_to_unnamed_passenger",
    "PA to Paid Driver":              "pa_to_paid_driver",
    "Liability to Paid Driver":       "liability_to_paid_driver",
    "Liability to Employees":         "liability_to_employees",
}

_ADDON_COVER_MAP = {
    "Nil Depreciation":               "nil_depreciation",
    "Electric Motor Protect":         "electric_motor_protect",
    "Battery Protection":             "ev_battery_protection",
    "Charger Cover":                  "ev_charger_cover",
    "Hospi Cash (30":                 "hospi_cash_30",
    "Hospi Cash (15":                 "hospi_cash_15",
    "Emergency Medical":              "emergency_medical",
    "Assistance Cover (Two Wheeler Shield)": "assistance_cover_tw_shield",
    "Assistance Cover - Two Wheeler Shield": "assistance_cover_tw",
    "24/7 RSA":                       "assistance_cover_24x7",
    "Key Protect":                    "key_protect",
    "Daily Allowance":                "daily_allowance_7days",
    "EMI Protection":                 "emi_protection",
    "Side Car":                       "is_side_car",
}


def _extract_tppd_cpa(record: CoverageDetails) -> tuple[str | None, str | None]:
    tppd_limit = record.extra_fields.get("ddlTPPDLimit") if record.extra_fields else None
    cpa_tenure = None
    if record.liability_covers:
        try:
            items = json.loads(record.liability_covers) if isinstance(record.liability_covers, str) else record.liability_covers
            for item in items:
                name = (item.get("name") or "").lower()
                if "cpa tenure" in name or "pa to owner" in name:
                    for part in (item.get("name") or "").split("\n"):
                        if part.strip().isdigit():
                            cpa_tenure = part.strip()
                            break
        except Exception:
            pass
    return tppd_limit, cpa_tenure


# ---------------------------------------------------------------------------
# Core Save Function (Populates all 7 relational tables with Foreign Keys)
# ---------------------------------------------------------------------------
def save_coverage(
    record: CoverageDetails,
    scrape_input_id: int | None = None,
    session_id: int | None = None,
    vehicle=None,
    customer=None,
    address=None,
) -> int:
    """
    Saves a completed coverage record across the 7 normalized relational tables:
      1. insurance_quote
      2. vehicle_details   (FK: quote_id)
      3. cover_details     (FK: quote_id)
      4. customer_details  (FK: quote_id)
      5. nominee_details   (FK: quote_id, customer_id)
      6. address_details   (FK: quote_id)
      7. premium_details   (FK: quote_id)

    Also updates legacy vehicle_coverage for backward compatibility.
    Returns the generated insurance_quote.id.
    """
    if scrape_input_id is None:
        raise ValueError("A database scrape-input ID is required to save coverage.")
    if vehicle is None or customer is None or address is None:
        raise ValueError(
            "Vehicle, customer, and address values from the database are required."
        )

    quote_id = scrape_input_id
    od_flags  = _parse_cover_flags(record.od_covers,  _OD_COVER_MAP)
    lib_flags = _parse_cover_flags(record.liability_covers, _LIABILITY_COVER_MAP)
    add_flags = _parse_cover_flags(record.addon_covers, _ADDON_COVER_MAP)
    tppd_limit, cpa_tenure = _extract_tppd_cpa(record)

    reg_number = vehicle.registration_number
    pol_start = vehicle.policy_start_date
    if not reg_number or not pol_start:
        raise ValueError("Registration number and policy start date are required in the DB row.")

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM insurance_quote WHERE id = %s;",
                (quote_id,),
            )
            if cur.fetchone() is None:
                raise ValueError(f"Database scrape-input row {quote_id} no longer exists.")
            cur.execute("""
                UPDATE insurance_quote
                SET proposal_number = COALESCE(%s, proposal_number),
                    trace_id = COALESCE(%s, trace_id),
                    scraped_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s;
            """, (record.proposal_number, record.trace_id, quote_id))

            final_model = record.make_model_variant or vehicle.make_model_variant
            if final_model and any(bad in str(final_model).lower() for bad in ("no record", "no data")):
                final_model = vehicle.make_model_variant
            if not final_model or any(
                bad in str(final_model).lower() for bad in ("no record", "no data")
            ):
                raise ValueError("A valid make/model value is required in the DB row.")

            # ---------------------------------------------------------------
            # 2. vehicle_details (FK: quote_id)
            # ---------------------------------------------------------------
            cur.execute("""
                INSERT INTO vehicle_details (
                    quote_id, registration_number, rto_location, make_model_variant,
                    vehicle_make_model, fuel_type, manufacturing_year, manufacturing_month,
                    engine_number, chassis_number, seating_capacity, cc_watt,
                    vehicle_segment, zone, lcc, ex_showroom_price, is_new_vehicle,
                    ncb_eligibility, discount_percent
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    rto_location = EXCLUDED.rto_location,
                    make_model_variant = EXCLUDED.make_model_variant,
                    vehicle_make_model = EXCLUDED.vehicle_make_model,
                    fuel_type = EXCLUDED.fuel_type,
                    manufacturing_year = EXCLUDED.manufacturing_year,
                    manufacturing_month = EXCLUDED.manufacturing_month,
                    engine_number = COALESCE(NULLIF(EXCLUDED.engine_number, ''), vehicle_details.engine_number),
                    chassis_number = COALESCE(NULLIF(EXCLUDED.chassis_number, ''), vehicle_details.chassis_number),
                    seating_capacity = EXCLUDED.seating_capacity,
                    cc_watt = EXCLUDED.cc_watt,
                    vehicle_segment = EXCLUDED.vehicle_segment,
                    zone = EXCLUDED.zone,
                    lcc = EXCLUDED.lcc,
                    ex_showroom_price = EXCLUDED.ex_showroom_price;
            """, (
                quote_id,
                reg_number,
                record.rto_location or vehicle.rto_location,
                final_model,
                record.vehicle_make_model or final_model,
                vehicle.fuel_type,
                vehicle.manufacturing_year,
                vehicle.manufacturing_month,
                vehicle.engine_number,
                vehicle.chassis_number,
                record.seating_capacity or vehicle.seating_capacity,
                record.cc_watt,
                record.vehicle_segment or vehicle.vehicle_segment,
                record.zone,
                record.lcc,
                record.ex_showroom_price,
                vehicle.is_new_vehicle,
                vehicle.ncb_eligibility,
                vehicle.data_discount_percent,
            ))

            # ---------------------------------------------------------------
            # 3. cover_details (FK: quote_id)
            # ---------------------------------------------------------------
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
                    basic_od = COALESCE(EXCLUDED.basic_od, cover_details.basic_od),
                    non_electrical_accessories = COALESCE(EXCLUDED.non_electrical_accessories, cover_details.non_electrical_accessories),
                    electrical_accessories = COALESCE(EXCLUDED.electrical_accessories, cover_details.electrical_accessories),
                    geo_extension = COALESCE(EXCLUDED.geo_extension, cover_details.geo_extension),
                    driving_tuition = COALESCE(EXCLUDED.driving_tuition, cover_details.driving_tuition),
                    fibre_glass_tank = COALESCE(EXCLUDED.fibre_glass_tank, cover_details.fibre_glass_tank),
                    voluntary_deductible = COALESCE(EXCLUDED.voluntary_deductible, cover_details.voluntary_deductible),
                    anti_theft_device = COALESCE(EXCLUDED.anti_theft_device, cover_details.anti_theft_device),
                    specially_designed = COALESCE(EXCLUDED.specially_designed, cover_details.specially_designed),
                    automobile_association = COALESCE(EXCLUDED.automobile_association, cover_details.automobile_association),
                    tppd = COALESCE(EXCLUDED.tppd, cover_details.tppd),
                    tppd_limit = COALESCE(EXCLUDED.tppd_limit, cover_details.tppd_limit),
                    basic_liability = COALESCE(EXCLUDED.basic_liability, cover_details.basic_liability),
                    pa_to_owner_driver = COALESCE(EXCLUDED.pa_to_owner_driver, cover_details.pa_to_owner_driver),
                    cpa_tenure = COALESCE(EXCLUDED.cpa_tenure, cover_details.cpa_tenure),
                    pa_to_named_passenger = COALESCE(EXCLUDED.pa_to_named_passenger, cover_details.pa_to_named_passenger),
                    pa_to_unnamed_passenger = COALESCE(EXCLUDED.pa_to_unnamed_passenger, cover_details.pa_to_unnamed_passenger),
                    pa_to_paid_driver = COALESCE(EXCLUDED.pa_to_paid_driver, cover_details.pa_to_paid_driver),
                    liability_to_paid_driver = COALESCE(EXCLUDED.liability_to_paid_driver, cover_details.liability_to_paid_driver),
                    liability_to_employees = COALESCE(EXCLUDED.liability_to_employees, cover_details.liability_to_employees),
                    nil_depreciation = COALESCE(EXCLUDED.nil_depreciation, cover_details.nil_depreciation),
                    electric_motor_protect = COALESCE(EXCLUDED.electric_motor_protect, cover_details.electric_motor_protect),
                    ev_battery_protection = COALESCE(EXCLUDED.ev_battery_protection, cover_details.ev_battery_protection),
                    ev_charger_cover = COALESCE(EXCLUDED.ev_charger_cover, cover_details.ev_charger_cover),
                    hospi_cash_30 = COALESCE(EXCLUDED.hospi_cash_30, cover_details.hospi_cash_30),
                    hospi_cash_15 = COALESCE(EXCLUDED.hospi_cash_15, cover_details.hospi_cash_15),
                    emergency_medical = COALESCE(EXCLUDED.emergency_medical, cover_details.emergency_medical),
                    assistance_cover_tw_shield = COALESCE(EXCLUDED.assistance_cover_tw_shield, cover_details.assistance_cover_tw_shield),
                    assistance_cover_tw = COALESCE(EXCLUDED.assistance_cover_tw, cover_details.assistance_cover_tw),
                    assistance_cover_24x7 = COALESCE(EXCLUDED.assistance_cover_24x7, cover_details.assistance_cover_24x7),
                    key_protect = COALESCE(EXCLUDED.key_protect, cover_details.key_protect),
                    daily_allowance_7days = COALESCE(EXCLUDED.daily_allowance_7days, cover_details.daily_allowance_7days),
                    emi_protection = COALESCE(EXCLUDED.emi_protection, cover_details.emi_protection),
                    is_side_car = COALESCE(EXCLUDED.is_side_car, cover_details.is_side_car);
            """, (
                quote_id,
                od_flags.get("basic_od"),
                od_flags.get("non_electrical_accessories"),
                od_flags.get("electrical_accessories"),
                od_flags.get("geo_extension"),
                od_flags.get("driving_tuition"),
                od_flags.get("fibre_glass_tank"),
                od_flags.get("voluntary_deductible"),
                od_flags.get("anti_theft_device"),
                od_flags.get("specially_designed"),
                od_flags.get("automobile_association"),
                lib_flags.get("tppd"),
                tppd_limit,
                lib_flags.get("basic_liability"),
                lib_flags.get("pa_to_owner_driver"),
                cpa_tenure,
                lib_flags.get("pa_to_named_passenger"),
                lib_flags.get("pa_to_unnamed_passenger"),
                lib_flags.get("pa_to_paid_driver"),
                lib_flags.get("liability_to_paid_driver"),
                lib_flags.get("liability_to_employees"),
                add_flags.get("nil_depreciation"),
                add_flags.get("electric_motor_protect"),
                add_flags.get("ev_battery_protection"),
                add_flags.get("ev_charger_cover"),
                add_flags.get("hospi_cash_30"),
                add_flags.get("hospi_cash_15"),
                add_flags.get("emergency_medical"),
                add_flags.get("assistance_cover_tw_shield"),
                add_flags.get("assistance_cover_tw"),
                add_flags.get("assistance_cover_24x7"),
                add_flags.get("key_protect"),
                add_flags.get("daily_allowance_7days"),
                add_flags.get("emi_protection"),
                add_flags.get("is_side_car"),
            ))

            # ---------------------------------------------------------------
            # 4. customer_details (FK: quote_id)
            # ---------------------------------------------------------------
            cust_first = customer.first_name
            cust_middle = customer.middle_name
            cust_last = customer.last_name
            cust_dob = customer.dob
            cust_pan = customer.pan
            cust_mob = customer.mobile
            cust_email = customer.email
            cust_aadhar = customer.aadhar
            cust_gen = customer.gender
            cust_occ = customer.occupation
            cust_ms = customer.marital_status
            cust_nationality = customer.nationality
            cust_registered_gst = customer.registered_under_gst
            cust_related_party = customer.related_party

            cur.execute("""
                INSERT INTO customer_details (
                    quote_id, first_name, middle_name, last_name, dob, pan, mobile, email, aadhar,
                    gender, occupation, marital_status, nationality, registered_gst, related_party
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    first_name = COALESCE(EXCLUDED.first_name, customer_details.first_name),
                    middle_name = COALESCE(EXCLUDED.middle_name, customer_details.middle_name),
                    last_name = COALESCE(EXCLUDED.last_name, customer_details.last_name),
                    dob = COALESCE(EXCLUDED.dob, customer_details.dob),
                    pan = COALESCE(EXCLUDED.pan, customer_details.pan),
                    gender = COALESCE(EXCLUDED.gender, customer_details.gender),
                    occupation = COALESCE(EXCLUDED.occupation, customer_details.occupation),
                    marital_status = COALESCE(EXCLUDED.marital_status, customer_details.marital_status),
                    mobile = COALESCE(EXCLUDED.mobile, customer_details.mobile),
                    email = COALESCE(EXCLUDED.email, customer_details.email),
                    aadhar = COALESCE(EXCLUDED.aadhar, customer_details.aadhar),
                    nationality = COALESCE(EXCLUDED.nationality, customer_details.nationality),
                    registered_gst = COALESCE(EXCLUDED.registered_gst, customer_details.registered_gst),
                    related_party = COALESCE(EXCLUDED.related_party, customer_details.related_party)
                RETURNING id;
            """, (
                quote_id, cust_first, cust_middle, cust_last, cust_dob, cust_pan,
                cust_mob, cust_email, cust_aadhar, cust_gen, cust_occ, cust_ms,
                cust_nationality, cust_registered_gst, cust_related_party,
            ))
            cust_id_row = cur.fetchone()
            customer_table_id = cust_id_row[0] if cust_id_row else None

            # ---------------------------------------------------------------
            # 5. nominee_details (FK: quote_id, customer_id)
            # ---------------------------------------------------------------
            nom_name = customer.nominee_name
            nom_dob = customer.nominee_dob
            nom_rel = customer.nominee_relation
            nom_age = customer.nominee_age

            cur.execute("""
                INSERT INTO nominee_details (
                    quote_id, customer_id, nominee_name, nominee_dob, nominee_relation, nominee_age
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    nominee_name = COALESCE(EXCLUDED.nominee_name, nominee_details.nominee_name),
                    nominee_dob = COALESCE(EXCLUDED.nominee_dob, nominee_details.nominee_dob),
                    nominee_relation = COALESCE(EXCLUDED.nominee_relation, nominee_details.nominee_relation),
                    nominee_age = COALESCE(EXCLUDED.nominee_age, nominee_details.nominee_age);
            """, (quote_id, customer_table_id, nom_name, nom_dob, nom_rel, nom_age))

            # ---------------------------------------------------------------
            # 6. address_details (FK: quote_id)
            # ---------------------------------------------------------------
            addr_flat = address.flat
            addr_bld = address.building
            addr_str = address.street
            addr_lnd = address.landmark
            addr_pin = address.pincode
            addr_state = address.state
            addr_city = address.city
            addr_mob = address.mobile
            addr_email = address.email
            addr_same = address.same_as_comm

            cur.execute("""
                INSERT INTO address_details (
                    quote_id, comm_flat, comm_building, comm_street, comm_landmark,
                    comm_pincode, comm_state, comm_city, comm_mobile, comm_email, perm_same_as_comm
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    comm_flat = COALESCE(EXCLUDED.comm_flat, address_details.comm_flat),
                    comm_building = COALESCE(EXCLUDED.comm_building, address_details.comm_building),
                    comm_street = COALESCE(EXCLUDED.comm_street, address_details.comm_street),
                    comm_landmark = COALESCE(EXCLUDED.comm_landmark, address_details.comm_landmark),
                    comm_city = COALESCE(EXCLUDED.comm_city, address_details.comm_city),
                    comm_state = COALESCE(EXCLUDED.comm_state, address_details.comm_state),
                    comm_pincode = COALESCE(EXCLUDED.comm_pincode, address_details.comm_pincode),
                    comm_mobile = COALESCE(EXCLUDED.comm_mobile, address_details.comm_mobile),
                    comm_email = COALESCE(EXCLUDED.comm_email, address_details.comm_email),
                    perm_same_as_comm = EXCLUDED.perm_same_as_comm;
            """, (quote_id, addr_flat, addr_bld, addr_str, addr_lnd, addr_pin, addr_state, addr_city, addr_mob, addr_email, addr_same))

            # ---------------------------------------------------------------
            # 7. premium_details (FK: quote_id)
            # ---------------------------------------------------------------
            cur.execute("""
                INSERT INTO premium_details (
                    quote_id, idv, base_od_premium, tp_premium, gst, net_premium, total_premium, extra_fields
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    idv = EXCLUDED.idv,
                    base_od_premium = EXCLUDED.base_od_premium,
                    tp_premium = EXCLUDED.tp_premium,
                    gst = EXCLUDED.gst,
                    net_premium = EXCLUDED.net_premium,
                    total_premium = EXCLUDED.total_premium,
                    extra_fields = EXCLUDED.extra_fields;
            """, (
                quote_id,
                record.idv,
                record.base_od_premium,
                record.tp_premium,
                record.gst,
                record.net_premium,
                record.total_premium,
                psycopg2.extras.Json(record.extra_fields) if record.extra_fields else psycopg2.extras.Json({}),
            ))

    logger.info(
        "[OK] Saved to 7 normalized tables | quote_id=%d | reg=%s | start_date=%s",
        quote_id, reg_number, pol_start,
    )
    return quote_id
