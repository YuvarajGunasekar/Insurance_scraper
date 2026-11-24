"""Test DB-row input mapping with synthetic data; no live DB or portal access."""

from __future__ import annotations

from datetime import date, timedelta
import logging
from uuid import uuid4

import pytest

from db.scrape_input_client import save_scrape_input
from scrapers.vehicle_scraper import (
    _build_cover_details_input,
    _build_vehicle_input,
    _normalize_policy_start_date,
    _wait_for_browser_review,
)


def _vehicle_row() -> dict:
    token = uuid4().hex
    today = date.today()
    return {
        "registration_number": f"TEST-{token}",
        "rto_location": f"TEST-RTO-{token[:6]}",
        "make_model_variant": f"TEST-MODEL-{token}",
        "manufacturing_year": str(today.year),
        "manufacturing_month": "TEST-MONTH",
        "purchase_date": today.strftime("%d-%m-%Y"),
        "policy_start_date": (today + timedelta(days=1)).strftime("%d-%m-%Y"),
        "idv": str(80000 + int(token[:4], 16)),
        "fuel_type": f"TEST-FUEL-{token[:6]}",
        "business_type": f"TEST-BUSINESS-{token[:6]}",
        "customer_type": f"TEST-CUSTOMER-{token[:6]}",
        "ncb_eligibility": f"TEST-NCB-{token[:4]}",
        "discount_percent": int(token[4:6], 16),
        "is_new_vehicle": False,
    }


def test_vehicle_builder_uses_database_values() -> None:
    row = _vehicle_row()
    vehicle = _build_vehicle_input(row)

    assert vehicle.registration_number == row["registration_number"]
    assert vehicle.make_model_variant == row["make_model_variant"]
    assert vehicle.fuel_type == row["fuel_type"]
    assert vehicle.business_type == row["business_type"]
    assert vehicle.customer_type == row["customer_type"]
    assert vehicle.is_new_vehicle == row["is_new_vehicle"]


def test_vehicle_builder_rejects_missing_database_value() -> None:
    row = _vehicle_row()
    row["fuel_type"] = None

    with pytest.raises(ValueError, match="fuel_type"):
        _build_vehicle_input(row)


def test_policy_start_date_rejects_past_date() -> None:
    today = date.today()
    past_date = (today - timedelta(days=1)).strftime("%d-%m-%Y")
    with pytest.raises(ValueError, match="is in the past"):
        _normalize_policy_start_date(past_date, today=today)


def test_policy_start_date_accepts_today_and_normalizes_iso() -> None:
    today = date.today()
    assert _normalize_policy_start_date(
        today.isoformat(), today=today
    ) == today.strftime("%d-%m-%Y")


def test_browser_review_wait_can_be_interrupted() -> None:
    class InterruptingPage:
        def wait_for_timeout(self, _timeout_ms: int) -> None:
            raise KeyboardInterrupt

    _wait_for_browser_review(
        InterruptingPage(),
        logging.getLogger("test_browser_review_wait"),
        seconds=1,
    )


_COVER_BOOLEAN_FIELDS = (
    "basic_od", "non_electrical_accessories", "electrical_accessories",
    "geo_extension", "driving_tuition", "fibre_glass_tank",
    "voluntary_deductible", "anti_theft_device", "specially_designed",
    "automobile_association", "tppd", "basic_liability",
    "pa_to_owner_driver", "pa_to_named_passenger",
    "pa_to_unnamed_passenger", "pa_to_paid_driver",
    "liability_to_paid_driver", "liability_to_employees",
    "nil_depreciation", "electric_motor_protect", "ev_battery_protection",
    "ev_charger_cover", "hospi_cash_30", "hospi_cash_15",
    "emergency_medical", "assistance_cover_tw_shield",
    "assistance_cover_tw", "assistance_cover_24x7", "key_protect",
    "daily_allowance_7days", "emi_protection", "is_side_car",
)


def _cover_row(
    nil_depreciation: bool = False,
    nil_depreciation_first_time: bool | None = None,
) -> dict:
    row = {f"cover_{field}": False for field in _COVER_BOOLEAN_FIELDS}
    row["cover_nil_depreciation"] = nil_depreciation
    row["cover_tppd_limit"] = str(5000 + date.today().day)
    row["cover_cpa_tenure"] = str((date.today().month - 1) % 3 + 1)
    if nil_depreciation_first_time is not None:
        row["cover_nil_depreciation_first_time"] = nil_depreciation_first_time
    return row


def test_cover_builder_uses_database_nil_depreciation_choice() -> None:
    row = _cover_row(nil_depreciation=True, nil_depreciation_first_time=False)

    covers = _build_cover_details_input(row)

    assert covers.nil_depreciation == row["cover_nil_depreciation"]
    assert covers.nil_depreciation_first_time == row["cover_nil_depreciation_first_time"]
    assert covers.tppd_limit == row["cover_tppd_limit"]
    assert covers.cpa_tenure == row["cover_cpa_tenure"]


def test_cover_builder_does_not_require_first_time_choice_when_cover_is_disabled() -> None:
    row = _cover_row()

    covers = _build_cover_details_input(row)

    assert covers.nil_depreciation == row["cover_nil_depreciation"]
    assert covers.nil_depreciation_first_time is None


def test_cover_builder_requires_first_time_choice_when_cover_is_enabled() -> None:
    row = _cover_row(nil_depreciation=True)

    with pytest.raises(ValueError, match="cover_nil_depreciation_first_time"):
        _build_cover_details_input(row)


def test_input_writer_rejects_missing_values_without_database_fallback() -> None:
    with pytest.raises(ValueError, match="Missing required scrape-input values"):
        save_scrape_input({})
