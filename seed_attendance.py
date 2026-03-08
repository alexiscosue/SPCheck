"""
Re-populate attendance + campus_attendance for the current semester.
Uses multi-row VALUES batches → only ~tens of SQL calls total.

Distributions: 80% Present, 15% Late, 4% Absent, 1% Excused
Campus: no Sundays, no Saturday Afternoon.

Run: python seed_attendance.py
"""

import random
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo
import pg8000.dbapi

DB = dict(
    host="dpg-d6lg7s94tr6s739s64n0-a.oregon-postgres.render.com",
    port=5432, database="spcheck_b86i",
    user="spcheck_user", password="RyCugEVE4EDUXZCpDbsgc6RcPMc2HhqA",
    ssl_context=True,
)
MNL   = ZoneInfo("Asia/Manila")
TODAY = datetime.now(MNL).date()

STATUS_WEIGHTS = [("Present",0.80),("Late",0.15),("Absent",0.04),("Excused",0.01)]
SESSION_CFG = {
    "Morning":   dict(in_present=(420,494), in_late=(495,585), out=(660,764)),
    "Afternoon": dict(in_present=(765,824), in_late=(825,915), out=(990,1065)),
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

def pick_status():
    r,cum=random.random(),0.0
    for s,w in STATUS_WEIGHTS:
        cum+=w
        if r<cum: return s
    return "Present"

def mins_to_time(m): return time(m//60, m%60)

def parse_time(v):
    if v is None: return None
    if isinstance(v, timedelta):
        t=int(v.total_seconds())%86400; return time(t//3600,(t%3600)//60)
    if isinstance(v, time): return v.replace(tzinfo=None)
    if isinstance(v, str):
        c=v.split("+")[0].split("-")[0].strip(); p=c.split(":")
        return time(int(p[0]),int(p[1]))
    return None

def class_timein(cls_date, cs, status):
    if status=="Absent": return None
    base=datetime.combine(cls_date,cs,tzinfo=MNL)
    delta=timedelta(minutes=random.randint(10,35)) if status=="Late" \
          else timedelta(minutes=random.randint(-5,4))
    return base+delta

def class_timeout(ti, cls_date, ce):
    if ti is None: return None
    return datetime.combine(cls_date,ce,tzinfo=MNL)+timedelta(minutes=random.randint(-3,8))

def campus_times(att_date, session, status):
    if status=="Absent": return None,None
    cfg=SESSION_CFG[session]
    in_range=cfg["in_late"] if status=="Late" else cfg["in_present"]
    ti=datetime.combine(att_date,mins_to_time(random.randint(*in_range)),tzinfo=MNL)
    to=datetime.combine(att_date,mins_to_time(random.randint(*cfg["out"])),tzinfo=MNL)
    return ti,to

def fmt(v):
    if v is None: return "None"
    return v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v,datetime) else str(v)

WEEKDAY={"Monday":0,"Tuesday":1,"Wednesday":2,"Thursday":3,"Friday":4,"Saturday":5,"Sunday":6}

def _exec_chunk(conn, sql_tpl, rows, chunk, label, cols=4):
    if not rows: return
    ph_single = "(%s)" % ",".join(["%s"]*cols)
    for i in range(0, len(rows), chunk):
        batch = rows[i:i+chunk]
        n = len(batch)
        placeholders = ",".join([ph_single] * n)
        flat = [v for row in batch for v in row]
        c = conn.cursor(); c.execute(sql_tpl.format(ph=placeholders), flat)
        c.close(); conn.commit()
        print(f"      {label} chunk {i//chunk+1}: {n} rows", flush=True)

def batch_insert_att(conn, rows, chunk=100):
    """rows: (status, timein, timeout, attendance_id) — UPDATE by PK"""
    _exec_chunk(conn, """
        UPDATE attendance AS a
        SET attendancestatus = v.status,
            timein  = v.timein::timestamptz,
            timeout = v.timeout::timestamptz
        FROM (VALUES {ph}) AS v(status, timein, timeout, attendance_id)
        WHERE a.attendance_id = v.attendance_id::int
    """, rows, chunk, "att-upd", cols=4)

def batch_update_att_status(conn, rows, chunk=100):
    """rows: (status, attendance_id) — status-only UPDATE (for null-timein records)"""
    _exec_chunk(conn, """
        UPDATE attendance AS a
        SET attendancestatus = v.status
        FROM (VALUES {ph}) AS v(status, attendance_id)
        WHERE a.attendance_id = v.attendance_id::int
    """, rows, chunk, "att-status-upd", cols=2)

def batch_update_campus(conn, rows, chunk=100):
    """rows: (status, time_in, time_out, personnel_id, attendance_date, session)
       UPDATE by natural key to avoid PK mismatch issues."""
    _exec_chunk(conn, """
        UPDATE campus_attendance AS ca
        SET status   = v.status,
            time_in  = v.time_in::timestamptz,
            time_out = v.time_out::timestamptz
        FROM (VALUES {ph}) AS v(status, time_in, time_out, personnel_id, attendance_date, session)
        WHERE ca.personnel_id      = v.personnel_id::int
          AND ca.attendance_date   = v.attendance_date::date
          AND ca.session           = v.session
    """, rows, chunk, "ca-upd", cols=6)

def batch_insert_ca(conn, rows, chunk=100):
    """rows: (personnel_id, attendance_date, session, time_in, time_out, status)"""
    _exec_chunk(conn, """
        INSERT INTO campus_attendance
            (personnel_id,attendance_date,session,time_in,time_out,status)
        SELECT v.pid::int, v.adate::date, v.sess,
               v.ti::timestamptz, v.to_::timestamptz, v.status
        FROM (VALUES {ph}) AS v(pid,adate,sess,ti,to_,status)
        ON CONFLICT DO NOTHING
    """, rows, chunk, "ca-ins", cols=6)

def batch_audit(conn, rows, chunk=100):
    _exec_chunk(conn, """
        INSERT INTO auditlogs(personnel_id,action,details,created_at)
        SELECT v.pid::int, v.action, v.details, v.created_at::timestamptz
        FROM (VALUES {ph}) AS v(pid,action,details,created_at)
    """, rows, chunk, "audit", cols=4)

def main():
    conn=pg8000.dbapi.connect(**DB); conn.autocommit=False

    def q(sql,params=()):
        c=conn.cursor(); c.execute(sql,params); rows=c.fetchall() or []; c.close(); return rows
    def q1(sql,params=()):
        rows=q(sql,params); return rows[0] if rows else None

    sem=q1("""SELECT acadcalendar_id,semesterstart,semesterend,semester,acadyear
              FROM acadcalendar WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
              ORDER BY semesterstart DESC LIMIT 1""")
    if not sem: print("❌  No active semester."); return
    sem_id,sem_start,sem_end,sem_name,acad_year=sem
    print(f"✅  Semester: {sem_name} {acad_year}  ({sem_start} → {sem_end})", flush=True)

    hr_ids=[r[0] for r in q("SELECT personnel_id FROM personnel WHERE role_id=20003")] or [None]
    print(f"👥  HR actors: {hr_ids}", flush=True)

    # ══════════════════════════════════════════════════════════════
    # 1. Class attendance
    # ══════════════════════════════════════════════════════════════
    print("\n── Class Attendance ─────────────────────────────────────────", flush=True)
    records=q("""
        SELECT a.attendance_id,a.attendancestatus,a.timein,a.timeout,
               DATE(a.timein AT TIME ZONE 'Asia/Manila'),
               sch.starttime_1,sch.endtime_1,sch.starttime_2,sch.endtime_2,
               sch.classday_1,sch.classday_2,
               a.class_id,p.firstname,p.lastname
        FROM attendance a
        JOIN schedule sch ON a.class_id=sch.class_id
        JOIN personnel p ON a.personnel_id=p.personnel_id
        WHERE sch.acadcalendar_id=%s ORDER BY a.attendance_id
    """,(sem_id,))
    print(f"   Records fetched: {len(records)}", flush=True)

    att_upd=[]; att_status_upd=[]; att_aud=[]
    for row in records:
        (att_id,old_st,old_ti,old_to,class_date,
         s1,e1,s2,e2,d1,d2,class_id,fname,lname)=row

        status=pick_status()

        if class_date is None:
            # NULL timein record — update status only (keep NULL times)
            att_status_upd.append((status, att_id))
            # Still generate audit entry sometimes
            if random.random()<0.25:
                hr=random.choice(hr_ids)
                # Use a random past date for the audit timestamp
                days_back=random.randint(1, max(1,(TODAY-sem_start).days))
                audit_date=TODAY-timedelta(days=days_back)
                details=(f"Attendance #{att_id} | {fname} {lname} | Class {class_id} | {audit_date}\n"
                         f"Remark: {random.choice(HR_REMARKS)}\n"
                         f"Before: Status: {old_st} | Time-in: {fmt(old_ti)} | Time-out: {fmt(old_to)}\n"
                         f"After: Status: {status} | Time-in: None | Time-out: None")
                at=datetime.combine(audit_date,time(random.randint(8,17),random.randint(0,59)),tzinfo=MNL)
                att_aud.append((hr,"HR Attendance Time Edit",details,at))
            continue

        day_name=class_date.strftime("%A")
        if d1 and WEEKDAY.get(d1)==WEEKDAY.get(day_name): cs,ce=parse_time(s1),parse_time(e1)
        elif d2 and WEEKDAY.get(d2)==WEEKDAY.get(day_name): cs,ce=parse_time(s2),parse_time(e2)
        else: cs,ce=parse_time(s1),parse_time(e1)
        if not cs or not ce: continue

        new_ti=class_timein(class_date,cs,status)
        new_to=class_timeout(new_ti,class_date,ce)
        att_upd.append((status,new_ti,new_to,att_id))
        if random.random()<0.25:
            hr=random.choice(hr_ids)
            details=(f"Attendance #{att_id} | {fname} {lname} | Class {class_id} | {class_date}\n"
                     f"Remark: {random.choice(HR_REMARKS)}\n"
                     f"Before: Status: {old_st} | Time-in: {fmt(old_ti)} | Time-out: {fmt(old_to)}\n"
                     f"After: Status: {status} | Time-in: {fmt(new_ti)} | Time-out: {fmt(new_to)}")
            at=datetime.combine(class_date,time(random.randint(8,17),random.randint(0,59)),tzinfo=MNL)
            att_aud.append((hr,"HR Attendance Time Edit",details,at))

    print(f"   Full-update rows: {len(att_upd)}  |  Status-only rows: {len(att_status_upd)}", flush=True)
    batch_insert_att(conn, att_upd)
    batch_update_att_status(conn, att_status_upd)
    print(f"   Inserting {len(att_aud)} audit rows...", flush=True)
    batch_audit(conn, att_aud)
    print(f"   ✓ Class attendance done", flush=True)

    # ══════════════════════════════════════════════════════════════
    # 2. Campus attendance — cleanup + repopulate
    # ══════════════════════════════════════════════════════════════
    print("\n── Campus Attendance ────────────────────────────────────────", flush=True)

    # Delete Sundays and Saturday Afternoon (entire semester range)
    c=conn.cursor()
    c.execute("DELETE FROM campus_attendance WHERE attendance_date BETWEEN %s AND %s AND EXTRACT(DOW FROM attendance_date)=0",(sem_start,sem_end))
    del_sun=c.rowcount
    c.execute("DELETE FROM campus_attendance WHERE attendance_date BETWEEN %s AND %s AND EXTRACT(DOW FROM attendance_date)=6 AND session='Afternoon'",(sem_start,sem_end))
    del_sat=c.rowcount
    # Delete any future records
    c.execute("DELETE FROM campus_attendance WHERE attendance_date > %s",(TODAY,))
    del_fut=c.rowcount
    c.close(); conn.commit()
    print(f"   Deleted Sundays: {del_sun}  |  Sat PM: {del_sat}  |  Future: {del_fut}", flush=True)

    all_personnel=q("SELECT personnel_id,firstname,lastname FROM personnel WHERE role_id IN (20001,20002,20003) ORDER BY personnel_id")
    print(f"   Personnel: {len(all_personnel)}", flush=True)

    # Build past slots only (no future)
    past_slots=[]
    d=sem_start
    while d<=TODAY:
        dow=d.weekday()
        if dow<6:  # Mon-Sat
            past_slots.append((d,"Morning"))
            if dow<5:  # Mon-Fri only get Afternoon
                past_slots.append((d,"Afternoon"))
        d+=timedelta(days=1)

    print(f"   Past slots: {len(past_slots)}", flush=True)

    # Load existing records by natural key
    existing={}
    for row in q("""SELECT campus_attendance_id,personnel_id,attendance_date,session,status,time_in,time_out
                    FROM campus_attendance WHERE attendance_date BETWEEN %s AND %s""",(sem_start,TODAY)):
        cid,pid,adate,sess,st,ti,to=row
        existing[(pid,adate,sess)]=(cid,st,ti,to)
    print(f"   Existing past records: {len(existing)}", flush=True)

    ca_upd=[]; ca_ins=[]; ca_aud=[]
    for pid,fname,lname in all_personnel:
        for att_date,session in past_slots:
            key=(pid,att_date,session)
            status=pick_status()
            new_ti,new_to=campus_times(att_date,session,status)
            if key in existing:
                cid,old_st,old_ti,old_to=existing[key]
                # Use natural key (pid, att_date, session) instead of cid
                ca_upd.append((status,new_ti,new_to,pid,att_date,session))
                if random.random()<0.25:
                    hr=random.choice(hr_ids)
                    details=(f"Campus #{cid} | {fname} {lname} | {session} | {att_date}\n"
                             f"Remark: {random.choice(HR_REMARKS)}\n"
                             f"Before: Status: {old_st} | Time-in: {fmt(old_ti)} | Time-out: {fmt(old_to)}\n"
                             f"After: Status: {status} | Time-in: {fmt(new_ti)} | Time-out: {fmt(new_to)}")
                    at=datetime.combine(att_date,time(random.randint(8,17),random.randint(0,59)),tzinfo=MNL)
                    ca_aud.append((hr,"HR Campus Attendance Edit",details,at))
            else:
                ca_ins.append((pid,att_date,session,new_ti,new_to,status))

    print(f"   Queued — upd={len(ca_upd)}  ins={len(ca_ins)}  aud={len(ca_aud)}", flush=True)
    print("   Sending campus updates...", flush=True)
    batch_update_campus(conn, ca_upd)
    print("   Sending campus inserts...", flush=True)
    batch_insert_ca(conn, ca_ins)
    print("   Sending campus audit logs...", flush=True)
    batch_audit(conn, ca_aud)

    print(f"\n✅  All done!", flush=True)
    conn.close()

if __name__=="__main__":
    random.seed(42)
    main()
