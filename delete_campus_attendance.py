"""
Delete campus_attendance rows for a specific personnel/date/session.

Example:
  python3 delete_campus_attendance.py --firstname Angelyn --lastname Bautista \
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
    parser.add_argument("--dry-run", action="store_true", help="Print what would be deleted")
    args = parser.parse_args()

    firstname = args.firstname.strip()
    lastname = args.lastname.strip()
    day = args.date.strip()
    session = args.session.strip()

    conn = pg8000.dbapi.connect(**DB)
    try:
        cursor = conn.cursor()

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
            SELECT campus_attendance_id
            FROM campus_attendance
            WHERE personnel_id = %s
              AND attendance_date = %s::date
              AND session = %s
            """,
            (personnel_id, day, session),
        )
        ids = [r[0] for r in (cursor.fetchall() or [])]

        if not ids:
            print("ℹ️ Nothing to delete (no matching campus_attendance row).")
            return

        print(
            f"✅ Found {len(ids)} matching campus_attendance row(s) for personnel_id={personnel_id}: {ids}"
        )

        if args.dry_run:
            print("Dry-run enabled; not deleting.")
            return

        cursor.execute(
            """
            DELETE FROM campus_attendance
            WHERE personnel_id = %s
              AND attendance_date = %s::date
              AND session = %s
            """,
            (personnel_id, day, session),
        )
        deleted = cursor.rowcount
        conn.commit()
        print(f"🗑️ Deleted {deleted} row(s) from campus_attendance.")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


if __name__ == "__main__":
    main()

