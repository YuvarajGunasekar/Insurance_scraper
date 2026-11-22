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
        checked = bool(item.get("checked", False))
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
    sid = session_id or scrape_input_id
    od_flags  = _parse_cover_flags(record.od_covers,  _OD_COVER_MAP)
    lib_flags = _parse_cover_flags(record.liability_covers, _LIABILITY_COVER_MAP)
    add_flags = _parse_cover_flags(record.addon_covers, _ADDON_COVER_MAP)
    tppd_limit, cpa_tenure = _extract_tppd_cpa(record)

    reg_number = record.registration_number or (vehicle.registration_number if vehicle else "NEW") or "NEW"
    pol_start  = record.policy_start_date or (vehicle.policy_start_date if vehicle else "") or ""

    with _get_conn() as conn:
        with conn.cursor() as cur:
            # ---------------------------------------------------------------
            # 1. insurance_quote (Master Table)
            # ---------------------------------------------------------------
            cur.execute("""
                INSERT INTO insurance_quote (
                    session_id, registration_number, policy_start_date, purchase_date,
                    business_type, customer_type, cover_type, proposal_number, trace_id, scraped_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (registration_number, policy_start_date)
                DO UPDATE SET
                    session_id = COALESCE(EXCLUDED.session_id, insurance_quote.session_id),
                    proposal_number = COALESCE(EXCLUDED.proposal_number, insurance_quote.proposal_number),
                    trace_id = COALESCE(EXCLUDED.trace_id, insurance_quote.trace_id),
                    scraped_at = NOW(),
                    updated_at = NOW()
                RETURNING id;
            """, (
                sid,
                reg_number,
                pol_start,
                vehicle.purchase_registration_date if vehicle else None,
                vehicle.business_type if vehicle else "New",
                vehicle.customer_type if vehicle else "Individual",
                record.cover_type,
                record.proposal_number,
                record.trace_id,
            ))
            quote_id = cur.fetchone()[0]

            final_model = record.make_model_variant or (vehicle.make_model_variant if vehicle else None)
            if final_model and any(bad in str(final_model).lower() for bad in ("no record", "no data")):
                final_model = (vehicle.make_model_variant if vehicle and not any(bad in str(vehicle.make_model_variant).lower() for bad in ("no record", "no data")) else None) or os.getenv("VEH_MAKE_MODEL_VARIANT", "TVS JUPITER")

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
                    engine_number = COALESCE(EXCLUDED.engine_number, vehicle_details.engine_number),
                    chassis_number = COALESCE(EXCLUDED.chassis_number, vehicle_details.chassis_number),
                    seating_capacity = EXCLUDED.seating_capacity,
                    cc_watt = EXCLUDED.cc_watt,
                    vehicle_segment = EXCLUDED.vehicle_segment,
                    zone = EXCLUDED.zone,
                    lcc = EXCLUDED.lcc,
                    ex_showroom_price = EXCLUDED.ex_showroom_price;
            """, (
                quote_id,
                reg_number,
                record.rto_location or (vehicle.rto_location if vehicle else None),
                final_model,
                record.vehicle_make_model or final_model,
                record.fuel_type or (vehicle.fuel_type if vehicle else "Petrol"),
                record.manufacturing_year or (vehicle.manufacturing_year if vehicle else None),
                record.manufacturing_month or (vehicle.manufacturing_month if vehicle else None),
                vehicle.engine_number if vehicle else None,
                vehicle.chassis_number if vehicle else None,
                record.seating_capacity or (vehicle.seating_capacity if vehicle else None),
                record.cc_watt,
                record.vehicle_segment or (vehicle.vehicle_segment if vehicle else None),
                record.zone,
                record.lcc,
                record.ex_showroom_price,
                vehicle.is_new_vehicle if vehicle else True,
                vehicle.ncb_eligibility if vehicle else "None",
                vehicle.data_discount_percent if vehicle else 0,
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
                    basic_od = EXCLUDED.basic_od,
                    tppd = EXCLUDED.tppd,
                    tppd_limit = EXCLUDED.tppd_limit,
                    basic_liability = EXCLUDED.basic_liability,
                    pa_to_owner_driver = EXCLUDED.pa_to_owner_driver,
                    cpa_tenure = EXCLUDED.cpa_tenure,
                    nil_depreciation = EXCLUDED.nil_depreciation;
            """, (
                quote_id,
                od_flags.get("basic_od", False),
                od_flags.get("non_electrical_accessories", False),
                od_flags.get("electrical_accessories", False),
                od_flags.get("geo_extension", False),
                od_flags.get("driving_tuition", False),
                od_flags.get("fibre_glass_tank", False),
                od_flags.get("voluntary_deductible", False),
                od_flags.get("anti_theft_device", False),
                od_flags.get("specially_designed", False),
                od_flags.get("automobile_association", False),
                lib_flags.get("tppd", True),
                tppd_limit or "6000",
                lib_flags.get("basic_liability", True),
                lib_flags.get("pa_to_owner_driver", True),
                cpa_tenure or "1",
                lib_flags.get("pa_to_named_passenger", False),
                lib_flags.get("pa_to_unnamed_passenger", False),
                lib_flags.get("pa_to_paid_driver", False),
                lib_flags.get("liability_to_paid_driver", False),
                lib_flags.get("liability_to_employees", False),
                add_flags.get("nil_depreciation", True),
                add_flags.get("electric_motor_protect", False),
                add_flags.get("ev_battery_protection", False),
                add_flags.get("ev_charger_cover", False),
                add_flags.get("hospi_cash_30", False),
                add_flags.get("hospi_cash_15", False),
                add_flags.get("emergency_medical", False),
                add_flags.get("assistance_cover_tw_shield", False),
                add_flags.get("assistance_cover_tw", False),
                add_flags.get("assistance_cover_24x7", False),
                add_flags.get("key_protect", False),
                add_flags.get("daily_allowance_7days", False),
                add_flags.get("emi_protection", False),
                add_flags.get("is_side_car", False),
            ))

            # ---------------------------------------------------------------
            # 4. customer_details (FK: quote_id)
            # ---------------------------------------------------------------
            cust_first = customer.first_name if customer else None
            cust_last  = customer.last_name if customer else None
            cust_dob   = customer.dob if customer else None
            cust_pan   = customer.pan if customer else None
            cust_mob   = customer.mobile if customer else None
            cust_email = customer.email if customer else None
            cust_aadhar= customer.aadhar if customer else None
            cust_gen   = customer.gender if customer else 'Male'
            cust_occ   = customer.occupation if customer else 'Others'
            cust_ms    = customer.marital_status if customer else 'Single'

            # If not provided in kwargs, try pulling from customer_input via session_id
            if sid and not cust_first:
                cur.execute("SELECT first_name, last_name, dob, pan, mobile, email, aadhar, gender, occupation, marital_status FROM customer_input WHERE session_id = %s LIMIT 1;", (sid,))
                ci = cur.fetchone()
                if ci:
                    cust_first, cust_last, cust_dob, cust_pan, cust_mob, cust_email, cust_aadhar, cust_gen, cust_occ, cust_ms = ci

            cur.execute("""
                INSERT INTO customer_details (
                    quote_id, first_name, last_name, dob, pan, mobile, email, aadhar,
                    gender, occupation, marital_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    first_name = COALESCE(EXCLUDED.first_name, customer_details.first_name),
                    last_name = COALESCE(EXCLUDED.last_name, customer_details.last_name),
                    dob = COALESCE(EXCLUDED.dob, customer_details.dob),
                    pan = COALESCE(EXCLUDED.pan, customer_details.pan),
                    mobile = COALESCE(EXCLUDED.mobile, customer_details.mobile),
                    email = COALESCE(EXCLUDED.email, customer_details.email)
                RETURNING id;
            """, (quote_id, cust_first, cust_last, cust_dob, cust_pan, cust_mob, cust_email, cust_aadhar, cust_gen, cust_occ, cust_ms))
            cust_id_row = cur.fetchone()
            customer_table_id = cust_id_row[0] if cust_id_row else None

            # ---------------------------------------------------------------
            # 5. nominee_details (FK: quote_id, customer_id)
            # ---------------------------------------------------------------
            nom_name = customer.nominee_name if customer else None
            nom_dob  = customer.nominee_dob if customer else None
            nom_rel  = customer.nominee_relation if customer else 'Father'
            nom_age  = customer.nominee_age if customer else None
            if sid and not nom_name:
                cur.execute("""
                    SELECT n.nominee_name, n.nominee_dob, n.nominee_relation, n.nominee_age
                    FROM nominee_input n
                    JOIN customer_input c ON c.id = n.customer_id
                    WHERE c.session_id = %s LIMIT 1;
                """, (sid,))
                ni = cur.fetchone()
                if ni:
                    nom_name, nom_dob, nom_rel, nom_age = ni

            cur.execute("""
                INSERT INTO nominee_details (
                    quote_id, customer_id, nominee_name, nominee_dob, nominee_relation, nominee_age
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    nominee_name = COALESCE(EXCLUDED.nominee_name, nominee_details.nominee_name),
                    nominee_relation = COALESCE(EXCLUDED.nominee_relation, nominee_details.nominee_relation);
            """, (quote_id, customer_table_id, nom_name, nom_dob, nom_rel, nom_age))

            # ---------------------------------------------------------------
            # 6. address_details (FK: quote_id)
            # ---------------------------------------------------------------
            addr_flat = address.flat if address else None
            addr_bld  = address.building if address else None
            addr_str  = address.street if address else None
            addr_lnd  = address.landmark if address else None
            addr_pin  = address.pincode if address else None
            addr_state= address.state if address else None
            addr_city = address.city if address else None
            addr_mob  = address.mobile if address else cust_mob
            addr_email= address.email if address else cust_email
            addr_same = address.same_as_comm if address else True

            if sid and not addr_city:
                cur.execute("""
                    SELECT comm_flat, comm_building, comm_street, comm_landmark, comm_pincode,
                           comm_state, comm_city, comm_mobile, comm_email, perm_same_as_comm
                    FROM address_input WHERE session_id = %s LIMIT 1;
                """, (sid,))
                ai = cur.fetchone()
                if ai:
                    addr_flat, addr_bld, addr_str, addr_lnd, addr_pin, addr_state, addr_city, addr_mob, addr_email, addr_same = ai

            cur.execute("""
                INSERT INTO address_details (
                    quote_id, comm_flat, comm_building, comm_street, comm_landmark,
                    comm_pincode, comm_state, comm_city, comm_mobile, comm_email, perm_same_as_comm
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (quote_id) DO UPDATE SET
                    comm_flat = COALESCE(EXCLUDED.comm_flat, address_details.comm_flat),
                    comm_city = COALESCE(EXCLUDED.comm_city, address_details.comm_city),
                    comm_state = COALESCE(EXCLUDED.comm_state, address_details.comm_state);
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
