"""Page Object for the Vehicle Details + Proposal flow on sz.indusindinsurance.co.in."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class VehicleFormInput:
    """All fields needed to fill the Vehicle Details form."""
    registration_number: str = ""      # e.g. "MH12AB1234" or blank for new
    rto_location: str = ""             # e.g. "TN-" to trigger autocomplete
    manufacturing_year: str = ""       # e.g. "2026" — defaults to current year
    manufacturing_month: str = ""      # e.g. "February" — defaults to current month
    purchase_registration_date: str = ""  # DD-MM-YYYY — defaults to today
    policy_start_date: str = ""        # DD-MM-YYYY — defaults to today
    make_model_variant: str = ""       # e.g. "TVS JUPITER" – typed into autocomplete
    idv: str = ""                      # e.g. "95000" or blank for auto-calc
    fuel_type: str = "Petrol"          # Diesel / Petrol / CNG / Electric
    business_type: str = "New"         # New / Renewal
    customer_type: str = "Individual"  # Individual / Corporate
    ncb_eligibility: str = "None"      # None / 20% / 25% / 35% / 45% / 50%
    data_discount_percent: int = 0
    is_new_vehicle: bool = True        # Check "New Vehicle" checkbox
    seating_capacity: str = ""
    vehicle_segment: str = ""

    def apply_dynamic_defaults(self) -> None:
        """Fill in blank mandatory fields with sensible dynamic defaults."""
        today = date.today()
        if not self.manufacturing_year:
            self.manufacturing_year = str(today.year)
        if not self.manufacturing_month:
            self.manufacturing_month = today.strftime("%B")  # e.g. "November"
        if not self.purchase_registration_date:
            self.purchase_registration_date = today.strftime("%d-%m-%Y")
        if not self.policy_start_date:
            self.policy_start_date = today.strftime("%d-%m-%Y")


@dataclass
class CoverDetailsInput:
    """Options for the Cover Details section (OD Package, Liability, AddOns)."""
    # OD Package checkboxes
    basic_od: bool = False
    non_electrical_accessories: bool = False
    electrical_accessories: bool = False
    geo_extension: bool = False
    driving_tuition: bool = False
    fibre_glass_tank: bool = False
    voluntary_deductible: bool = False
    anti_theft_device: bool = False
    specially_designed: bool = False
    automobile_association: bool = False

    # Liability Covers
    tppd: bool = True
    tppd_limit: str = "100000"
    basic_liability: bool = True
    pa_to_owner_driver: bool = True
    cpa_tenure: str = "1"
    pa_to_named_passenger: bool = False
    pa_to_unnamed_passenger: bool = False
    pa_to_paid_driver: bool = False
    liability_to_paid_driver: bool = False
    liability_to_employees: bool = False

    # AddOn Covers
    nil_depreciation: bool = True
    nil_depreciation_rate: str = ""  # auto-filled by website (e.g. "0.3")
    electric_motor_protect: bool = False
    ev_battery_protection: bool = False
    ev_charger_cover: bool = False
    hospi_cash_30: bool = False
    hospi_cash_15: bool = False
    emergency_medical: bool = False
    assistance_cover_tw_shield: bool = False
    assistance_cover_tw: bool = False
    assistance_cover_24x7: bool = False
    key_protect: bool = False
    daily_allowance_7days: bool = False
    emi_protection: bool = False
    is_side_car: bool = False


@dataclass
class CustomerInput:
    """All fields needed to fill the Customer Details section of the Proposal form."""
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    dob: str = ""                   # DD-MM-YYYY
    pan: str = ""
    gender: str = "Male"            # Male / Female / Other
    occupation: str = "Others"
    marital_status: str = "Single"  # Single / Married / Divorced / Widowed
    mobile: str = ""
    email: str = ""
    aadhar: str = ""
    nationality: str = "Indian"
    registered_under_gst: str = "No"
    related_party: str = "No"


@dataclass
class AddressInput:
    """Communication (and optionally Permanent) address fields."""
    flat: str = ""
    building: str = ""
    street: str = ""
    landmark: str = ""
    pincode: str = ""
    state: str = ""
    city: str = ""
    mobile: str = ""
    email: str = ""
    same_as_comm: bool = True       # tick "Permanent Address same as Communication" if True


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
