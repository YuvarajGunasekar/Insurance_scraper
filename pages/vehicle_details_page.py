"""Page Object for the Vehicle Details + Proposal flow on sz.indusindinsurance.co.in."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class VehicleFormInput:
    """All fields needed to fill the Vehicle Details form."""
    registration_number: str
    rto_location: str
    manufacturing_year: str
    manufacturing_month: str
    purchase_registration_date: str
    policy_start_date: str
    make_model_variant: str
    idv: str
    fuel_type: str
    business_type: str
    customer_type: str
    ncb_eligibility: str
    data_discount_percent: int
    is_new_vehicle: bool
    seating_capacity: str = ""
    vehicle_segment: str = ""
    engine_number: str = ""
    chassis_number: str = ""


@dataclass
class CoverDetailsInput:
    """Options for the Cover Details section (OD Package, Liability, AddOns)."""
    # OD Package checkboxes
    basic_od: bool
    non_electrical_accessories: bool
    electrical_accessories: bool
    geo_extension: bool
    driving_tuition: bool
    fibre_glass_tank: bool
    voluntary_deductible: bool
    anti_theft_device: bool
    specially_designed: bool
    automobile_association: bool

    # Liability Covers
    tppd: bool
    tppd_limit: str
    basic_liability: bool
    pa_to_owner_driver: bool
    cpa_tenure: str
    pa_to_named_passenger: bool
    pa_to_unnamed_passenger: bool
    pa_to_paid_driver: bool
    liability_to_paid_driver: bool
    liability_to_employees: bool
    nil_depreciation_first_time: bool | None

    # AddOn Covers
    nil_depreciation: bool
    electric_motor_protect: bool
    ev_battery_protection: bool
    ev_charger_cover: bool
    hospi_cash_30: bool
    hospi_cash_15: bool
    emergency_medical: bool
    assistance_cover_tw_shield: bool
    assistance_cover_tw: bool
    assistance_cover_24x7: bool
    key_protect: bool
    daily_allowance_7days: bool
    emi_protection: bool
    is_side_car: bool


@dataclass
class CustomerInput:
    """All fields needed to fill the Customer Details section of the Proposal form."""
    customer_type: str
    first_name: str
    middle_name: str
    last_name: str
    dob: str
    pan: str
    gender: str
    occupation: str
    marital_status: str
    mobile: str
    email: str
    aadhar: str
    nationality: str
    registered_under_gst: str
    related_party: str
    nominee_name: str
    nominee_dob: str
    nominee_relation: str
    nominee_age: str


@dataclass
class AddressInput:
    """Communication (and optionally Permanent) address fields."""
    flat: str
    building: str
    street: str
    landmark: str
    pincode: str
    state: str
    city: str
    mobile: str
    email: str
    same_as_comm: bool


@dataclass
class CoverageDetails:
    """Coverage + premium details scraped after the full Proposal flow."""
    registration_number: str = ""
    rto_location: str = ""
    manufacturing_year: str = ""
    manufacturing_month: str = ""
    policy_start_date: str = ""
    fuel_type: str = ""
    make_model_variant: str = ""
    # Vehicle Details panel output
    idv: Optional[str] = None
    ex_showroom_price: Optional[str] = None
    # Premium Summary fields
    base_od_premium: Optional[str] = None
    tp_premium: Optional[str] = None
    net_premium: Optional[str] = None
    gst: Optional[str] = None
    total_premium: Optional[str] = None
    # Legacy / fallback
    own_damage_premium: Optional[str] = None
    third_party_premium: Optional[str] = None
    # Proposal identifiers
    proposal_number: Optional[str] = None
    trace_id: Optional[str] = None
    cover_type: Optional[str] = None
    vehicle_make_model: Optional[str] = None
    cc_watt: Optional[str] = None
    vehicle_segment: Optional[str] = None
    zone: Optional[str] = None
    seating_capacity: Optional[str] = None
    lcc: Optional[str] = None
    # Cover details scraped from the real website
    od_covers: Optional[str] = None       # JSON string of OD covers selected
    liability_covers: Optional[str] = None # JSON string of liability covers
    addon_covers: Optional[str] = None     # JSON string of addon covers selected
    # Raw scraped data
    raw_coverage_text: Optional[str] = None
    extra_fields: dict = field(default_factory=dict)
