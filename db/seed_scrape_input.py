"""
db/seed_scrape_input.py
=======================
Seeds the initial scrape_input row (TVS Jupiter / TN-01) from known .env values.
Skips if an active row already exists.

Usage:
    .venv\\Scripts\\python.exe db/seed_scrape_input.py
"""
from __future__ import annotations
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from db.scrape_input_client import get_latest_scrape_input, save_scrape_input

existing = get_latest_scrape_input()
if existing:
    print(f"[SKIP] Active scrape_input already exists (id={existing['id']}). No seed needed.")
    print(f"       reg={existing.get('registration_number')} | model={existing.get('make_model_variant')}")
else:
    row_id = save_scrape_input({
        "registration_number": "NEW",
        "rto_location": "TAMIL NADU | Chennai Central (Ayanavaram) | TN-01",
        "make_model_variant": "TVS JUPITER",
        "manufacturing_year": "2026",
        "manufacturing_month": "FEBRUARY",
        "fuel_type": "Petrol",
        "business_type": "New",
        "is_new_vehicle": True,
        "idv": "auto",
        "engine_number": "JA12E3456789",
        "chassis_number": "MD625BG45M1234567",
        # Covers
        "cover_nil_depreciation": True,
        "cover_pa_to_owner_driver": True,
        "cover_tppd": True,
        "cover_tppd_limit": "6000",
        "cover_cpa_tenure": "1",
        "cover_basic_liability": True,
        # Customer
        "cust_first_name": "KEERTHIKA",
        "cust_last_name": "ELUMALAI",
        "cust_dob": "02-04-2001",
        "cust_gender": "Female",
        "cust_marital_status": "Single",
        "cust_occupation": "Others",
        "cust_mobile": "7904150276",
        "cust_email": "keerthikaelumalai1812@gmail.com",
        "cust_pan": "IMSPK6171G",
        "cust_aadhar": "738747484499",
        # Nominee
        "nominee_name": "Murugan",
        "nominee_dob": "01-01-1975",
        "nominee_relation": "Father",
        "nominee_age": "49",
        # Address
        "addr_flat": "1/93",
        "addr_building": "thatta street",
        "addr_street": "Mariyamman koil street",
        "addr_pincode": "605652",
        "addr_state": "TAMIL NADU",
        "addr_city": "Villupuram",
        "addr_perm_same_as_comm": True,
        # Flow
        "continue_to_buy": True,
        "validate_customer": True,
        "is_active": True,
        "notes": "Seeded from .env — TN-01 / TVS Jupiter",
    })
    print(f"[OK] Seeded scrape_input id={row_id}")
