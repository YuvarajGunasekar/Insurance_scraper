"""
db/verify_db.py
===============
One-command script to verify all normalized relational tables and foreign keys in PostgreSQL.

Usage:
    .\\.venv\\Scripts\\python.exe db/verify_db.py
"""
import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

def verify():
    print("=" * 75)
    print("  INSURANCE SCRAPER — POSTGRESQL RELATIONAL SCHEMA VERIFICATION")
    print("=" * 75)

    host = os.getenv("PG_HOST", "localhost")
    port = int(os.getenv("PG_PORT", 5432))
    dbname = os.getenv("PG_DB", "insurance")
    user = os.getenv("PG_USER", "postgres")
    password = os.getenv("PG_PASSWORD", "")

    try:
        conn = psycopg2.connect(
            host=host, port=port, dbname=dbname, user=user, password=password
        )
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        print(f"\n[OK] Connected to PostgreSQL: {user}@{host}:{port}/{dbname}\n")
    except Exception as e:
        print(f"\n[ERROR] Failed to connect to PostgreSQL: {e}")
        return

    # 1. Table Row Counts
    tables = [
        ("insurance_quote",  "Master Quote Table"),
        ("vehicle_details",  "Vehicle Details Form Section"),
        ("cover_details",    "Cover Details Form Section (OD/TP/Addons)"),
        ("customer_details", "Customer Details Form Section"),
        ("nominee_details",  "Nominee Details Form Section"),
        ("address_details",  "Communication & Permanent Address Section"),
        ("premium_details",  "Premium Summary Section"),
    ]

    print("-" * 75)
    print(f" {'TABLE NAME':<22} | {'ROWS':<6} | {'FORM SECTION / ROLE':<40}")
    print("-" * 75)
    for tbl, desc in tables:
        cur.execute(f"SELECT count(*) AS cnt FROM {tbl};")
        cnt = cur.fetchone()["cnt"]
        print(f" {tbl:<22} | {cnt:<6} | {desc:<40}")
    print("-" * 75)

    # 2. Foreign Key Verification
    print("\n" + "=" * 75)
    print("  ACTIVE FOREIGN KEY CONSTRAINTS")
    print("=" * 75)
    cur.execute("""
        SELECT
            tc.table_name, kcu.column_name,
            ccu.table_name AS foreign_table_name,
            ccu.column_name AS foreign_column_name
        FROM
            information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
              AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
              AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema='public'
        ORDER BY tc.table_name;
    """)
    fks = cur.fetchall()
    for fk in fks:
        print(f"  [FK LINK] {fk['table_name']}.{fk['column_name']}  ──>  {fk['foreign_table_name']}.{fk['foreign_column_name']}")

    # 3. Sample Joined Quote via Master View
    print("\n" + "=" * 75)
    print("  LATEST QUOTE DATA (VIEW: v_complete_insurance_quote)")
    print("=" * 75)
    cur.execute("""
        SELECT quote_id, registration_number, policy_start_date, vehicle_make_model,
               cust_first_name, nominee_name, addr_city, idv, net_premium, total_premium
        FROM v_complete_insurance_quote
        ORDER BY quote_id DESC LIMIT 1;
    """)
    latest = cur.fetchone()
    if latest:
        for k, v in latest.items():
            print(f"  {k:<22} : {v}")
    else:
        print("  No quotes found yet.")

    print("\n" + "=" * 75)
    print("  ALL VERIFICATIONS COMPLETED SUCCESSFULLY!")
    print("=" * 75)
    cur.close()
    conn.close()

if __name__ == "__main__":
    verify()
