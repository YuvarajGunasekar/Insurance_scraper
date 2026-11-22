"""Request and response models for the scraping API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr


class ScrapeRequest(BaseModel):
    """Credentials for a scraper job; other settings come from .env."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "username": "your-authorized-username",
                "password": "your-authorized-password",
            }
        },
    )

    username: str = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    scraper_configured: bool
    allowed_hosts_configured: bool


class ScrapeAccepted(BaseModel):
    task_id: UUID
    status: Literal["pending", "running"]
    deduplicated: bool = False


class ScrapeStatusResponse(BaseModel):
    task_id: UUID
    website_url: AnyHttpUrl
    scrape_type: Literal["full"]
    status: Literal["pending", "running", "completed", "failed"]
    created_at: datetime
    updated_at: datetime
    result_summary: str | None = None
    error: str | None = Field(default=None, max_length=1000)


class LoginRequest(BaseModel):
    """Credentials and URL for a visible, manual-CAPTCHA login task."""

    model_config = ConfigDict(extra="forbid")

    website_url: AnyHttpUrl
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr


class LoginAccepted(BaseModel):
    task_id: UUID
    status: Literal[
        "pending", "awaiting_captcha", "running", "completed", "failed"
    ]
    deduplicated: bool = False
    message: str | None = None


class LoginResponse(BaseModel):
    """Final synchronous result returned by POST /login (200 OK)."""

    task_id: UUID
    status: Literal["completed", "failed"]
    deduplicated: bool = False
    message: str | None = None
    login_succeeded: bool | None = None
    error: str | None = Field(default=None, max_length=1000)


class LoginContinueResponse(BaseModel):
    task_id: UUID
    status: Literal["awaiting_captcha"]
    message: str


class LoginStatusResponse(BaseModel):
    task_id: UUID
    website_url: AnyHttpUrl
    status: Literal[
        "pending", "awaiting_captcha", "running", "completed", "failed"
    ]
    created_at: datetime
    updated_at: datetime
    message: str | None = None
    login_succeeded: bool | None = None
    error: str | None = Field(default=None, max_length=1000)


class CoverRequest(BaseModel):
    nil_depreciation: bool = True
    pa_to_owner_driver: bool = True


class CustomerRequest(BaseModel):
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    dob: str | None = None
    pan: str | None = None
    gender: str = "Male"
    occupation: str = "Others"
    marital_status: str = "Single"
    mobile: str | None = None
    email: str | None = None
    aadhar: str | None = None
    nationality: str = "Indian"
    registered_under_gst: str = "No"
    related_party: str = "No"


class AddressRequest(BaseModel):
    flat: str | None = None
    building: str | None = None
    street: str | None = None
    landmark: str | None = None
    pincode: str | None = None
    state: str | None = None
    city: str | None = None
    mobile: str | None = None
    email: str | None = None
    same_as_comm: bool = True


class VehicleRequest(BaseModel):
    registration_number: str | None = None
    rto_location: str | None = None
    make_model_variant: str | None = None
    manufacturing_year: str | None = None
    manufacturing_month: str | None = None
    purchase_date: str | None = None
    policy_start_date: str | None = None
    fuel_type: str | None = None
    business_type: str | None = None
    customer_type: str | None = None
    ncb: str | None = None
    discount: int | None = None
    is_new_vehicle: bool | None = None


class ScrapeInputCreate(BaseModel):
    """All fields for creating a new scrape session (flat format, backward compatible).

    Internally the server distributes these fields across 6 normalized tables:
    scrape_session, vehicle_input, cover_input, customer_input, nominee_input, address_input.
    """

    model_config = ConfigDict(extra="forbid")

    # Vehicle
    registration_number: str = "NEW"
    rto_location: str = ""
    make_model_variant: str = ""
    manufacturing_year: str = ""
    manufacturing_month: str = ""
    fuel_type: str = "Petrol"
    business_type: str = "New"
    customer_type: str = "Individual"
    is_new_vehicle: bool = True
    idv: str = "auto"
    ncb_eligibility: str = "None"
    discount_percent: int = 0
    purchase_date: str | None = None
    policy_start_date: str | None = None
    engine_number: str | None = None
    chassis_number: str | None = None

    # OD covers
    cover_basic_od: bool = False
    cover_non_electrical_accessories: bool = False
    cover_electrical_accessories: bool = False
    cover_geo_extension: bool = False
    cover_driving_tuition: bool = False
    cover_fibre_glass_tank: bool = False
    cover_voluntary_deductible: bool = False
    cover_anti_theft_device: bool = False
    cover_specially_designed: bool = False
    cover_automobile_association: bool = False

    # Liability covers
    cover_tppd: bool = True
    cover_tppd_limit: str = "6000"
    cover_basic_liability: bool = True
    cover_pa_to_owner_driver: bool = True
    cover_cpa_tenure: str = "1"
    cover_pa_to_named_passenger: bool = False
    cover_pa_to_unnamed_passenger: bool = False
    cover_pa_to_paid_driver: bool = False
    cover_liability_to_paid_driver: bool = False
    cover_liability_to_employees: bool = False

    # AddOn covers
    cover_nil_depreciation: bool = True
    cover_electric_motor_protect: bool = False
    cover_ev_battery_protection: bool = False
    cover_ev_charger_cover: bool = False
    cover_hospi_cash_30: bool = False
    cover_hospi_cash_15: bool = False
    cover_emergency_medical: bool = False
    cover_assistance_cover_tw_shield: bool = False
    cover_assistance_cover_tw: bool = False
    cover_assistance_cover_24x7: bool = False
    cover_key_protect: bool = False
    cover_daily_allowance_7days: bool = False
    cover_emi_protection: bool = False
    cover_is_side_car: bool = False

    # Customer
    cust_first_name: str | None = None
    cust_middle_name: str | None = None
    cust_last_name: str | None = None
    cust_dob: str | None = None
    cust_pan: str | None = None
    cust_gender: str = "Male"
    cust_occupation: str = "Others"
    cust_marital_status: str = "Single"
    cust_mobile: str | None = None
    cust_email: str | None = None
    cust_aadhar: str | None = None
    cust_nationality: str = "Indian"
    cust_registered_gst: str = "No"
    cust_related_party: str = "No"

    # Nominee
    nominee_name: str | None = None
    nominee_dob: str | None = None
    nominee_relation: str = "Father"
    nominee_age: str | None = None

    # Address
    addr_flat: str | None = None
    addr_building: str | None = None
    addr_street: str | None = None
    addr_landmark: str | None = None
    addr_pincode: str | None = None
    addr_state: str | None = None
    addr_city: str | None = None
    addr_mobile: str | None = None
    addr_email: str | None = None
    addr_perm_same_as_comm: bool = True

    # Flow flags
    continue_to_buy: bool = True
    validate_customer: bool = True
    upload_document: bool = True
    upload_doc_type: str = "Invoice_Copy"
    upload_doc_path: str = "sample_documents/sample_document.pdf"
    save_proposal: bool = True
    proceed_to_payment: bool = True

    # Meta
    is_active: bool = True
    notes: str | None = None


class ScrapeInputResponse(BaseModel):
    """Summary of a scrape_input row (returned by list/get endpoints)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    registration_number: str
    rto_location: str
    make_model_variant: str
    manufacturing_year: str
    manufacturing_month: str
    fuel_type: str
    policy_start_date: str | None = None
    is_active: bool
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Nested (normalized) schemas — one sub-model per DB table
# ---------------------------------------------------------------------------

class VehicleInputCreate(BaseModel):
    """Vehicle Details form section → vehicle_input table."""
    registration_number: str = "NEW"
    rto_location: str = ""
    make_model_variant: str = ""
    manufacturing_year: str = ""
    manufacturing_month: str = ""
    fuel_type: str = "Petrol"
    business_type: str = "New"
    customer_type: str = "Individual"
    is_new_vehicle: bool = True
    idv: str = "auto"
    ncb_eligibility: str = "None"
    discount_percent: int = 0
    purchase_date: str | None = None
    policy_start_date: str | None = None
    engine_number: str | None = None
    chassis_number: str | None = None


class CoverInputCreate(BaseModel):
    """Cover Details form section → cover_input table."""
    # OD Package
    cover_basic_od: bool = False
    cover_non_electrical_accessories: bool = False
    cover_electrical_accessories: bool = False
    cover_geo_extension: bool = False
    cover_driving_tuition: bool = False
    cover_fibre_glass_tank: bool = False
    cover_voluntary_deductible: bool = False
    cover_anti_theft_device: bool = False
    cover_specially_designed: bool = False
    cover_automobile_association: bool = False
    # Liability
    cover_tppd: bool = True
    cover_tppd_limit: str = "6000"
    cover_basic_liability: bool = True
    cover_pa_to_owner_driver: bool = True
    cover_cpa_tenure: str = "1"
    cover_pa_to_named_passenger: bool = False
    cover_pa_to_unnamed_passenger: bool = False
    cover_pa_to_paid_driver: bool = False
    cover_liability_to_paid_driver: bool = False
    cover_liability_to_employees: bool = False
    # AddOn
    cover_nil_depreciation: bool = True
    cover_electric_motor_protect: bool = False
    cover_ev_battery_protection: bool = False
    cover_ev_charger_cover: bool = False
    cover_hospi_cash_30: bool = False
    cover_hospi_cash_15: bool = False
    cover_emergency_medical: bool = False
    cover_assistance_cover_tw_shield: bool = False
    cover_assistance_cover_tw: bool = False
    cover_assistance_cover_24x7: bool = False
    cover_key_protect: bool = False
    cover_daily_allowance_7days: bool = False
    cover_emi_protection: bool = False
    cover_is_side_car: bool = False


class NomineeInputCreate(BaseModel):
    """Nominee sub-section → nominee_input table (child of customer_input)."""
    nominee_name: str | None = None
    nominee_dob: str | None = None
    nominee_relation: str = "Father"
    nominee_age: str | None = None


class CustomerInputCreate(BaseModel):
    """Customer Details form section → customer_input table."""
    cust_first_name: str | None = None
    cust_middle_name: str | None = None
    cust_last_name: str | None = None
    cust_dob: str | None = None
    cust_pan: str | None = None
    cust_gender: str = "Male"
    cust_occupation: str = "Others"
    cust_marital_status: str = "Single"
    cust_mobile: str | None = None
    cust_email: str | None = None
    cust_aadhar: str | None = None
    cust_nationality: str = "Indian"
    cust_registered_gst: str = "No"
    cust_related_party: str = "No"
    nominee: NomineeInputCreate = Field(default_factory=NomineeInputCreate)


class AddressInputCreate(BaseModel):
    """Communication + Permanent Address sections → address_input table."""
    # Communication address
    addr_flat: str | None = None
    addr_building: str | None = None
    addr_street: str | None = None
    addr_landmark: str | None = None
    addr_pincode: str | None = None
    addr_state: str | None = None
    addr_city: str | None = None
    addr_mobile: str | None = None
    addr_email: str | None = None
    # Permanent address
    addr_perm_same_as_comm: bool = True
    perm_flat: str | None = None
    perm_building: str | None = None
    perm_street: str | None = None
    perm_landmark: str | None = None
    perm_pincode: str | None = None
    perm_state: str | None = None
    perm_city: str | None = None


class SessionFlowCreate(BaseModel):
    """Proposal flow control flags → scrape_session table."""
    continue_to_buy: bool = True
    validate_customer: bool = True
    upload_document: bool = True
    upload_doc_type: str = "Invoice_Copy"
    upload_doc_path: str = "sample_documents/sample_document.pdf"
    save_proposal: bool = True
    proceed_to_payment: bool = True
    is_active: bool = True
    notes: str | None = None


class ScrapeSessionCreate(BaseModel):
    """
    Structured (nested) alternative to ScrapeInputCreate.
    POST to /api/scrape-session to create a fully normalized session.
    """
    vehicle: VehicleInputCreate = Field(default_factory=VehicleInputCreate)
    covers: CoverInputCreate = Field(default_factory=CoverInputCreate)
    customer: CustomerInputCreate = Field(default_factory=CustomerInputCreate)
    address: AddressInputCreate = Field(default_factory=AddressInputCreate)
    flow: SessionFlowCreate = Field(default_factory=SessionFlowCreate)

    def to_flat(self) -> dict:
        """Flatten nested structure to the same dict format as ScrapeInputCreate."""
        flat: dict = {}
        flat.update(self.vehicle.model_dump())
        flat.update(self.covers.model_dump())
        # Customer (keep cust_ prefix keys)
        cust = self.customer.model_dump(exclude={"nominee"})
        flat.update(cust)
        # Nominee (merge from nested)
        flat.update(self.customer.nominee.model_dump())
        flat.update(self.address.model_dump())
        flat.update(self.flow.model_dump())
        return flat
