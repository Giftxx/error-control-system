"""
seed_db.py — Migrate existing CSV/JSON data into PostgreSQL
Run once to populate machines + error_codes + error_code_keywords tables.

Usage:
    python seed_db.py
"""

import psycopg2
import psycopg2.extras
import csv
import json
import os
import sys

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "ai_agent",
    "user": "athip",
    "password": "123456",
}

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(SCRIPT_DIR, "dashboard", "backend")
MACHINES_FILE = os.path.join(BACKEND_DIR, "machines.json")
ERROR_DB_CSV = os.path.join(SCRIPT_DIR, "error_db.csv")


def seed():
    print("=" * 60)
    print("  Detect_Na — Seed DB from CSV / JSON")
    print("=" * 60)

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ─── 1. Machines ───
    print("\n🖥️  Seeding machines...")
    if os.path.isfile(MACHINES_FILE):
        with open(MACHINES_FILE, "r", encoding="utf-8") as f:
            machines = json.load(f)

        count = 0
        for mid, info in machines.items():
            cur.execute("""
                INSERT INTO machines (machine_id, display_name, ip_address, vnc_port, vnc_password, vnc_window_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (machine_id) DO UPDATE SET
                  display_name  = EXCLUDED.display_name,
                  ip_address    = EXCLUDED.ip_address,
                  vnc_port      = EXCLUDED.vnc_port,
                  vnc_password  = EXCLUDED.vnc_password,
                  vnc_window_id = EXCLUDED.vnc_window_id;
            """, (
                mid,
                info.get("name", mid),
                info.get("ip", "0.0.0.0"),
                info.get("vnc_port", 0),
                info.get("vnc_password", ""),
                info.get("vnc_id", f"RealVNC_{mid}"),
            ))
            count += 1
        print(f"  ✅ {count} machines upserted")
    else:
        print(f"  ⚠️  {MACHINES_FILE} not found, skipping")

    # ─── 2. Error Codes + Keywords ───
    print("\n⚠️  Seeding error_codes + keywords...")
    if os.path.isfile(ERROR_DB_CSV):
        with open(ERROR_DB_CSV, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        code_count = 0
        kw_count = 0
        for row in rows:
            eid = row.get("error_id", "").strip()
            if not eid:
                continue

            cat = row.get("cat", "")
            desc = row.get("desc", "")
            sev = row.get("sev", "medium")
            handler = row.get("handler", "")
            kw_raw = row.get("kw", "")

            # Validate severity
            if sev not in ("high", "medium", "low"):
                sev = "medium"

            cur.execute("""
                INSERT INTO error_codes (error_id, category, description, severity, handler, keywords)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (error_id) DO UPDATE SET
                  category    = EXCLUDED.category,
                  description = EXCLUDED.description,
                  severity    = EXCLUDED.severity,
                  handler     = EXCLUDED.handler,
                  keywords    = EXCLUDED.keywords;
            """, (eid, cat, desc, sev, handler, kw_raw.strip()))
            code_count += 1
            if kw_raw.strip():
                kw_count += len([p for p in kw_raw.split("|") if p.strip()])

        print(f"  ✅ {code_count} error codes upserted")
        print(f"  ✅ {kw_count} keywords (in error_codes.keywords column)")
    else:
        print(f"  ⚠️  {ERROR_DB_CSV} not found, skipping")

    # ─── 3. Verify ───
    print("\n🔍 Verification...")
    cur.execute("SELECT COUNT(*) AS cnt FROM machines")
    print(f"  machines:     {cur.fetchone()['cnt']} rows")
    cur.execute("SELECT COUNT(*) AS cnt FROM error_codes")
    print(f"  error_codes:  {cur.fetchone()['cnt']} rows")
    cur.execute("SELECT COUNT(*) AS cnt FROM error_events")
    print(f"  error_events: {cur.fetchone()['cnt']} rows")

    # Show sample data
    print("\n📋 Sample: error_codes (first 3)")
    cur.execute("""
        SELECT error_id, category, severity, keywords
        FROM error_codes
        ORDER BY error_id
        LIMIT 3;
    """)
    for r in cur.fetchall():
        kws = r["keywords"][:60] + "..." if len(r["keywords"]) > 60 else r["keywords"]
        print(f"  {r['error_id']:25s} | {r['category']:20s} | {r['severity']:6s} | {kws}")

    cur.close()
    conn.close()

    print("\n" + "=" * 60)
    print("  🎉 Seed complete!")
    print("=" * 60)


if __name__ == "__main__":
    seed()
