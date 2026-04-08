"""
Overwrite attendance + campus_attendance for the current semester
but only for dates >= March 17 (Manila time) up to today.

Distributions: 80% Present, 15% Late, 4% Absent, 1% Excused
Class Attendance:
  - updates attendance rows whose derived class_date (from DATE(attendance.timein AT TIME ZONE 'Asia/Manila'))
    falls within the range.
  - leaves records with NULL timein (therefore NULL derived class_date) unchanged.
Campus Attendance:
  - deletes and regenerates campus_attendance rows within the range.
  - respects existing "no Sundays" and "no Saturday Afternoon" constraints.

Run:
  python3 seed_attendance_from_march17.py
"""

import argparse
import random
import os
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo

import pg8000.dbapi
from dotenv import load_dotenv

# Match `app.py`: load .env for DB credentials.
load_dotenv()


#
# Use the SAME DB env vars as `app.py` so this script can connect reliably.
# Fallback defaults are kept for safety, but should normally be overridden.
#
DB = dict(
    host=os.getenv("DB_HOST", "dpg-d6lg7s94tr6s739s64n0-a.oregon-postgres.render.com"),
    port=int(os.getenv("DB_PORT", "5432")),
    database=os.getenv("DB_NAME", "spcheck_b86i"),
    user=os.getenv("DB_USER", "spcheck_user"),
    password=os.getenv("DB_PASSWORD", "RyCugEVE4EDUXZCpDbsgc6RcPMc2HhqA"),
    ssl_context=True,
    timeout=10,
)

MNL = ZoneInfo("Asia/Manila")
TODAY = datetime.now(MNL).date()

STATUS_WEIGHTS = [
    ("Present", 0.80),
    ("Late", 0.15),
    ("Absent", 0.04),
    ("Excused", 0.01),
]

SESSION_CFG = {
    "Morning": dict(in_present=(420, 494), in_late=(495, 585), out=(660, 764)),
    "Afternoon": dict(in_present=(765, 824), in_late=(825, 915), out=(990, 1065)),
}

HR_REMARKS = [
    "Corrected based on faculty's physical logbook entry.",
    "Updated per DTR submission reviewed by HR.",
    "Time adjusted — RFID scan missed; verified via CCTV.",
    "Manual correction — biometric reader offline that day.",
    "Revised per department memo and supporting documents.",
    "Adjusted after review of classroom log sheet.",
    "HR correction based on faculty-submitted incident report.",
    "Time-in corrected; faculty presented proof of presence.",
    "Correction per dean's endorsement letter.",
    "Discrepancy resolved after cross-checking RFID logs.",
]

WEEKDAY = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6}


def pick_status():
    r, cum = random.random(), 0.0
    for s, w in STATUS_WEIGHTS:
        cum += w
        if r < cum:
            return s
    return "Present"


def mins_to_time(m: int) -> time:
    return time(m // 60, m % 60)


def parse_time(v):
    if v is None:
        return None
    if isinstance(v, timedelta):
        t = int(v.total_seconds()) % 86400
        return time(t // 3600, (t % 3600) // 60)
    if isinstance(v, time):
        return v.replace(tzinfo=None)
    if isinstance(v, str):
        c = v.split("+")[0].split("-")[0].strip()
        p = c.split(":")
        return time(int(p[0]), int(p[1]))
    return None


def class_timein(cls_date: date, cs: time, status: str):
    if status in ("Absent", "Excused"):
        return None
    base = datetime.combine(cls_date, cs, tzinfo=MNL)
    # Late: move forward; Present: near scheduled time; keep small variance.
    if status == "Late":
        delta = timedelta(minutes=random.randint(10, 35))
    else:
        delta = timedelta(minutes=random.randint(-5, 4))
    return base + delta


def class_timeout(ti_dt, cls_date: date, ce: time):
    if ti_dt is None:
        return None
    return datetime.combine(cls_date, ce, tzinfo=MNL) + timedelta(minutes=random.randint(-3, 8))


def campus_times(att_date: date, session: str, status: str):
    if status in ("Absent", "Excused"):
        return None, None
    cfg = SESSION_CFG[session]
    in_range = cfg["in_late"] if status == "Late" else cfg["in_present"]
    ti = datetime.combine(att_date, mins_to_time(random.randint(*in_range)), tzinfo=MNL)
    to = datetime.combine(att_date, mins_to_time(random.randint(*cfg["out"])), tzinfo=MNL)
    return ti, to


def fmt(v):
    if v is None:
        return "None"
    return v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else str(v)


def _exec_chunk(conn, sql_tpl, rows, chunk, label, cols=4):
    if not rows:
        return
    ph_single = "(%s)" % ",".join(["%s"] * cols)
    for i in range(0, len(rows), chunk):
        batch = rows[i : i + chunk]
        n = len(batch)
        placeholders = ",".join([ph_single] * n)
        flat = [v for row in batch for v in row]
        c = conn.cursor()
        c.execute(sql_tpl.format(ph=placeholders), flat)
        c.close()
        conn.commit()
        print(f"      {label} chunk {i // chunk + 1}: {n} rows", flush=True)


def batch_update_att(conn, rows, chunk=100):
    """
    rows: (status, timein, timeout, attendance_id)
    """
    _exec_chunk(
        conn,
        """
        UPDATE attendance AS a
        SET attendancestatus = v.status,
            timein  = v.timein::timestamptz,
            timeout = v.timeout::timestamptz
        FROM (VALUES {ph}) AS v(status, timein, timeout, attendance_id)
        WHERE a.attendance_id = v.attendance_id::int
        """,
        rows,
        chunk,
        "att-upd",
        cols=4,
    )


def batch_update_campus(conn, rows, chunk=100):
    """
    rows: (status, time_in, time_out, personnel_id, attendance_date, session)
    """
    _exec_chunk(
        conn,
        """
        UPDATE campus_attendance AS ca
        SET status   = v.status,
            time_in  = v.time_in::timestamptz,
            time_out = v.time_out::timestamptz
        FROM (VALUES {ph}) AS v(status, time_in, time_out, personnel_id, attendance_date, session)
        WHERE ca.personnel_id      = v.personnel_id::int
          AND ca.attendance_date   = v.attendance_date::date
          AND ca.session           = v.session
        """,
        rows,
        chunk,
        "ca-upd",
        cols=6,
    )


def batch_insert_ca(conn, rows, chunk=100):
    """
    rows: (personnel_id, attendance_date, session, time_in, time_out, status)
    """
    _exec_chunk(
        conn,
        """
        INSERT INTO campus_attendance
            (personnel_id,attendance_date,session,time_in,time_out,status)
        SELECT v.pid::int, v.adate::date, v.sess,
               v.ti::timestamptz, v.to_::timestamptz, v.status
        FROM (VALUES {ph}) AS v(pid,adate,sess,ti,to_,status)
        ON CONFLICT (personnel_id, attendance_date, session) DO NOTHING
        """,
        rows,
        chunk,
        "ca-ins",
        cols=6,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD (defaults to March 17 of current Manila year)")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD (defaults to today in Manila)")
    parser.add_argument("--seed", default=42, type=int, help="random seed for reproducibility")
    args = parser.parse_args()

    random.seed(args.seed)

    # Quick sanity so we know which DB this will target
    resolved_host = DB.get("host")
    resolved_db = DB.get("database")
    resolved_user = DB.get("user")
    resolved_port = DB.get("port")
    print(f"Connecting DB: {resolved_host}:{resolved_port} db={resolved_db} user={resolved_user}", flush=True)

    start_date = (
        datetime.strptime(args.start_date, "%Y-%m-%d").date()
        if args.start_date
        else date(TODAY.year, 3, 17)
    )
    end_date = (
        datetime.strptime(args.end_date, "%Y-%m-%d").date()
        if args.end_date
        else TODAY
    )

    if start_date > end_date:
        print(f"❌ start-date {start_date} is after end-date {end_date}")
        return

    conn = pg8000.dbapi.connect(**DB)
    conn.autocommit = False

    def q(sql, params=()):
        c = conn.cursor()
        c.execute(sql, params)
        rows = c.fetchall() or []
        c.close()
        return rows

    def q1(sql, params=()):
        rows = q(sql, params)
        return rows[0] if rows else None

    sem = q1(
        """
        SELECT acadcalendar_id, semesterstart, semesterend, semester, acadyear
        FROM acadcalendar
        WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
        ORDER BY semesterstart DESC
        LIMIT 1
        """
    )
    if not sem:
        print("❌ No active semester.")
        return

    sem_id, sem_start, sem_end, sem_name, acad_year = sem

    # clamp to semester range
    start_date = max(start_date, sem_start)
    end_date = min(end_date, sem_end)
    if start_date > end_date:
        print("ℹ️ Date range does not intersect the active semester. Nothing to do.")
        return

    print(f"✅ Semester: {sem_name} {acad_year} ({sem_start} → {sem_end})", flush=True)
    print(f"✅ Overwriting dates: {start_date} → {end_date}", flush=True)

    # ----------------------------
    # 1) Class attendance
    # ----------------------------
    print("\n── Class Attendance (partial) ─────────────────────────────────", flush=True)

    records = q(
        """
        SELECT a.attendance_id, a.attendancestatus, a.timein, a.timeout,
               DATE(a.timein AT TIME ZONE 'Asia/Manila'),
               sch.starttime_1, sch.endtime_1, sch.starttime_2, sch.endtime_2,
               sch.classday_1, sch.classday_2,
               a.class_id, p.firstname, p.lastname
        FROM attendance a
        JOIN schedule sch ON a.class_id=sch.class_id
        JOIN personnel p ON a.personnel_id=p.personnel_id
        WHERE sch.acadcalendar_id=%s
        ORDER BY a.attendance_id
        """,
        (sem_id,),
    )

    att_upd = []
    skipped_null_class_date = 0
    for row in records:
        (att_id, old_st, old_ti, old_to, class_date,
         s1, e1, s2, e2, d1, d2, class_id, fname, lname) = row

        if class_date is None:
            # can't apply a date filter
            skipped_null_class_date += 1
            continue

        if not (start_date <= class_date <= end_date):
            continue

        status = pick_status()
        # Determine which scheduled time range matches the weekday of class_date.
        day_name = class_date.strftime("%A")

        if d1 and WEEKDAY.get(d1) == WEEKDAY.get(day_name):
            cs, ce = parse_time(s1), parse_time(e1)
        elif d2 and WEEKDAY.get(d2) == WEEKDAY.get(day_name):
            cs, ce = parse_time(s2), parse_time(e2)
        else:
            cs, ce = parse_time(s1), parse_time(e1)

        if not cs or not ce:
            continue

        new_ti = class_timein(class_date, cs, status)
        new_to = class_timeout(new_ti, class_date, ce)
        att_upd.append((status, new_ti, new_to, att_id))

    print(f"   Class rows queued for overwrite: {len(att_upd)}", flush=True)
    print(f"   Skipped (NULL derived class_date): {skipped_null_class_date}", flush=True)
    batch_update_att(conn, att_upd)
    print("   ✓ Class attendance overwrite done", flush=True)

    # ----------------------------
    # 2) Campus attendance
    # ----------------------------
    print("\n── Campus Attendance (partial) ────────────────────────────────", flush=True)

    # Deterministic overwrite:
    # Delete everything within the date range, then regenerate only valid sessions:
    # - No Sundays
    # - No Saturday Afternoon
    c = conn.cursor()
    c.execute(
        """
        DELETE FROM campus_attendance
        WHERE attendance_date BETWEEN %s AND %s
        """,
        (start_date, end_date),
    )
    del_all = c.rowcount
    c.close()
    conn.commit()
    print(f"   Deleted all campus rows in range: {del_all}", flush=True)

    # Build slots for overwrite range
    past_slots = []
    d = start_date
    while d <= end_date:
        dow = d.weekday()  # Monday=0 ... Sunday=6
        if dow < 6:  # Mon-Sat
            past_slots.append((d, "Morning"))
            if dow < 5:  # Mon-Fri only get Afternoon
                past_slots.append((d, "Afternoon"))
        d += timedelta(days=1)

    all_personnel = q(
        """
        SELECT personnel_id, firstname, lastname
        FROM personnel
        WHERE role_id IN (20001, 20002, 20003)
        ORDER BY personnel_id
        """
    )

    ca_ins = []
    for pid, fname, lname in all_personnel:
        for att_date, session in past_slots:
            status = pick_status()
            new_ti, new_to = campus_times(att_date, session, status)
            # After deletion, insert-only is enough.
            ca_ins.append((pid, att_date, session, new_ti, new_to, status))

    print(f"   Queued — ins={len(ca_ins)}", flush=True)
    print("   Sending campus inserts...", flush=True)
    batch_insert_ca(conn, ca_ins)

    print("\n✅ Partial seeding complete!", flush=True)
    conn.close()


if __name__ == "__main__":
    main()

