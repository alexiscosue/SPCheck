"""
Clear (NULL) time_in + time_out for a campus_attendance row.

Example:
  python3 clear_campus_attendance_times.py --firstname Angelyn --lastname Bautista \
    --date 2026-04-08 --session Afternoon
"""

import argparse
import os

import pg8000
from dotenv import load_dotenv

load_dotenv()

DB = dict(
    host=os.getenv("DB_HOST"),
    port=int(os.getenv("DB_PORT", "5432")),
    database=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    ssl_context=True,
    timeout=10,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--firstname", required=True)
    parser.add_argument("--lastname", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--session", required=True, help="Morning or Afternoon")
    parser.add_argument("--dry-run", action="store_true", help="Only show rows that match")
    args = parser.parse_args()

    firstname = args.firstname.strip()
    lastname = args.lastname.strip()
    day = args.date.strip()
    session = args.session.strip()

    conn = pg8000.dbapi.connect(**DB)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT personnel_id
            FROM personnel
            WHERE lower(firstname) = lower(%s)
              AND lower(lastname)  = lower(%s)
            ORDER BY personnel_id
            LIMIT 1
            """,
            (firstname, lastname),
        )
        row = cursor.fetchone()
        if not row:
            print(f"❌ No personnel found for {lastname}, {firstname}")
            return

        personnel_id = row[0]

        cursor.execute(
            """
            SELECT campus_attendance_id, status, time_in, time_out
            FROM campus_attendance
            WHERE personnel_id = %s
              AND attendance_date = %s::date
              AND session = %s
            """,
            (personnel_id, day, session),
        )
        matches = cursor.fetchall() or []
        if not matches:
            print("ℹ️ Nothing matched (no campus_attendance row to clear).")
            return

        print(f"✅ Matched {len(matches)} row(s). Before:")
        for cid, status, ti, to in matches:
            print(f"  - id={cid} status={status} time_in={ti} time_out={to}")

        if args.dry_run:
            print("Dry-run enabled; not updating.")
            return

        cursor.execute(
            """
            UPDATE campus_attendance
            SET time_in = NULL,
                time_out = NULL,
                status = 'Absent'
            WHERE personnel_id = %s
              AND attendance_date = %s::date
              AND session = %s
            """,
            (personnel_id, day, session),
        )
        conn.commit()
        print(f"🗑️ Cleared time_in/time_out for {len(matches)} row(s).")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


if __name__ == "__main__":
    main()

