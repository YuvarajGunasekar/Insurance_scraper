"""Request and response models for the scraping API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr


class ScrapeRequest(BaseModel):
    """Parameters for a scraper job."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "example": {
                "website_url": "https://sz.indusindinsurance.co.in/",
                "username": "your-authorized-username",
                "password": "your-authorized-password",
                "scrape_type": "full",
                "make_model_variant": "TVS JUPITER",
                "rto_location": "TN-",
                "registration_number": "",
                "is_new_vehicle": True,
            }
        },
    )

    website_url: AnyHttpUrl
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr
    scrape_type: Literal["full"] = "full"
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
    vehicle: VehicleRequest | None = None
    cover: CoverRequest | None = None
    customer: CustomerRequest | None = None
    address: AddressRequest | None = None


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