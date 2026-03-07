import os
from datetime import datetime, timedelta, date
import pytz
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from dotenv import load_dotenv
import pg8000
from pg8000 import dbapi
from rfid_reader import RFIDReader
from biometric_reader import BiometricReader
from shared_serial import SharedSerialPort
from datetime import timedelta, datetime
from flask import Response
import json
import queue
import threading
import atexit
import time
from flask import make_response
from io import BytesIO
import base64
from PIL import Image
import gspread
from google.oauth2.service_account import Credentials
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, mm
from reportlab.lib import colors

def log_audit(action, details, personnel_id=None):
    """
    Log actions to existing auditlogs table
    """
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO auditlogs (action, details, personnel_id, created_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        """, (action, details, personnel_id))
        
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
    except Exception as e:
        print(f"Audit log error: {str(e)}")


load_dotenv()

app = Flask(__name__)
app.secret_key = 'spc-faculty-system-2025-secret-key'

# ========== SESSION CONFIGURATION ==========
app.config['SESSION_PERMANENT'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=60)  

@app.before_request
def make_session_permanent():
    session.permanent = False

def convert_to_24hour(time_str):
    """Convert time string to 24-hour format. Default to AM if no period specified."""
    if not time_str:
        return '00:00:00'
    
    if ':' in time_str and 'AM' not in time_str.upper() and 'PM' not in time_str.upper():
        if time_str.count(':') == 1:
            return time_str + ':00'
        return time_str
    
    time_str = time_str.upper().strip()
    time_part = time_str.replace('AM', '').replace('PM', '').strip()
    period = 'AM'
    if 'PM' in time_str:
        period = 'PM'
    elif 'AM' not in time_str:
        period = 'AM'
    
    if ':' in time_part:
        parts = time_part.split(':')
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    else:
        hours = int(time_part)
        minutes = 0
    
    if period == 'PM' and hours != 12:
        hours += 12
    elif period == 'AM' and hours == 12:
        hours = 0
    
    return f"{hours:02d}:{minutes:02d}:00"

def update_attendance_report(personnel_id, class_id, acadcalendar_id, conn=None):
    """
    Calculate and update attendance report for a specific personnel/class/semester.
    Called whenever attendance is recorded or modified.
    """
    should_close = False
    if conn is None:
        conn = db_pool.get_connection()
        should_close = True
    
    try:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                COUNT(*) FILTER (WHERE attendancestatus = 'Present') as present_count,
                COUNT(*) FILTER (WHERE attendancestatus = 'Late') as late_count,
                COUNT(*) FILTER (WHERE attendancestatus = 'Excused') as excused_count,
                COUNT(*) FILTER (WHERE attendancestatus = 'Absent') as absent_count,
                COUNT(*) as total_classes
            FROM attendance
            WHERE personnel_id = %s 
            AND class_id = %s
            AND class_id IN (
                SELECT class_id FROM schedule WHERE acadcalendar_id = %s
            )
        """, (personnel_id, class_id, acadcalendar_id))
        
        stats = cursor.fetchone()
        if not stats:
            cursor.close()
            if should_close:
                db_pool.return_connection(conn)
            return
        
        present, late, excused, absent, total = stats
        
        # Calculate attendance rate: ((present + excused + (late * 0.75)) / total) * 100
        if total > 0:
            attendance_rate = ((present + excused + (late * 0.75)) / total) * 100
        else:
            attendance_rate = 0.0
        
        cursor.execute("""
            SELECT attendancereport_id FROM attendancereport
            WHERE personnel_id = %s AND class_id = %s AND acadcalendar_id = %s
        """, (personnel_id, class_id, acadcalendar_id))
        
        existing = cursor.fetchone()
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz)
        
        if existing:
            cursor.execute("""
                UPDATE attendancereport 
                SET presentcount = %s,
                    latecount = %s,
                    excusedcount = %s,
                    absentcount = %s,
                    totalclasses = %s,
                    attendancerate = %s,
                    lastupdated = %s
                WHERE attendancereport_id = %s
            """, (present, late, excused, absent, total, attendance_rate, current_time, existing[0]))
        else:
            cursor.execute("SELECT COALESCE(MAX(attendancereport_id), 130000) + 1 FROM attendancereport")
            new_id = cursor.fetchone()[0]
            
            cursor.execute("""
                INSERT INTO attendancereport (
                    attendancereport_id, personnel_id, class_id, acadcalendar_id,
                    presentcount, latecount, excusedcount, absentcount, 
                    totalclasses, attendancerate, lastupdated
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (new_id, personnel_id, class_id, acadcalendar_id, 
                  present, late, excused, absent, total, attendance_rate, current_time))
        
        conn.commit()
        cursor.close()
        
        if should_close:
            db_pool.return_connection(conn)
        
        print(f"✅ Updated attendance report: Personnel {personnel_id}, Class {class_id}, Rate: {attendance_rate:.2f}")
        
    except Exception as e:
        print(f"❌ Error updating attendance report: {e}")
        if conn:
            conn.rollback()
        if should_close and cursor:
            cursor.close()
            db_pool.return_connection(conn)


def create_initial_attendance_report(personnel_id, class_id, acadcalendar_id, conn):
    """
    Insert a zero-initialized attendance report row for a newly created class.
    Safe to call inside an existing transaction (uses the passed conn, never commits).
    No-op if a row already exists for this personnel/class/semester.
    """
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1 FROM attendancereport
            WHERE personnel_id = %s AND class_id = %s AND acadcalendar_id = %s
        """, (personnel_id, class_id, acadcalendar_id))
        if cursor.fetchone():
            cursor.close()
            return
        cursor.execute("SELECT COALESCE(MAX(attendancereport_id), 130000) + 1 FROM attendancereport")
        new_id = cursor.fetchone()[0]
        philippines_tz = pytz.timezone('Asia/Manila')
        now = datetime.now(philippines_tz)
        cursor.execute("""
            INSERT INTO attendancereport (
                attendancereport_id, personnel_id, class_id, acadcalendar_id,
                presentcount, latecount, excusedcount, absentcount,
                totalclasses, attendancerate, lastupdated
            ) VALUES (%s, %s, %s, %s, 0, 0, 0, 0, 0, 0.0, %s)
        """, (new_id, personnel_id, class_id, acadcalendar_id, now))
        cursor.close()
    except Exception as e:
        print(f"❌ Error creating initial attendance report: {e}")


# ========== CONNECTION POOLING ==========
from queue import Queue, Empty
import threading

class ConnectionPool:
    def __init__(self, min_connections=2, max_connections=10):
        self.min_connections = min_connections
        self.max_connections = max_connections
        self.pool = Queue(maxsize=max_connections)
        self.current_connections = 0
        self.lock = threading.Lock()
        
        for _ in range(min_connections):
            self.pool.put(self._create_connection())
            self.current_connections += 1
    
    def _create_connection(self):
        conn = pg8000.dbapi.connect(
            host=os.getenv('DB_HOST'),
            port=int(os.getenv('DB_PORT', 5432)),
            database=os.getenv('DB_NAME'),
            user=os.getenv('DB_USER'),
            password=os.getenv('DB_PASSWORD'),
            ssl_context=True
        )

        cursor = conn.cursor()
        cursor.execute("SET TIME ZONE 'Asia/Manila'")
        cursor.close()
        return conn
    
    def get_connection(self):
        try:
            conn = self.pool.get(block=False)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.close()
                return conn
            except:
                conn.close()
                return self._create_connection()
        except Empty:
            with self.lock:
                if self.current_connections < self.max_connections:
                    self.current_connections += 1
                    return self._create_connection()

            return self.pool.get(block=True, timeout=5)
    
    def return_connection(self, conn):
        try:
            self.pool.put(conn, block=False)
        except:
            conn.close()
            with self.lock:
                self.current_connections -= 1

db_pool = ConnectionPool(min_connections=3, max_connections=15)


def ensure_notifications_table():
    """Create notifications table if it doesn't exist."""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                notif_id SERIAL PRIMARY KEY,
                target_audience VARCHAR(20) NOT NULL,
                target_personnel_id INTEGER,
                notification_type VARCHAR(20),
                person_name VARCHAR(255),
                tapped_personnel_id INTEGER,
                rfid_uid VARCHAR(100),
                biometric_uid VARCHAR(100),
                biometric_id INTEGER,
                action VARCHAR(50),
                status VARCHAR(50),
                message TEXT,
                subject_code VARCHAR(50),
                subject_name VARCHAR(255),
                class_section VARCHAR(100),
                classroom VARCHAR(100),
                tap_time VARCHAR(100),
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        print("Notifications table ready.")
    except Exception as e:
        print(f"Error ensuring notifications table: {e}")


def save_notification_to_db(target_audience, target_personnel_id, data):
    """Insert one notification row and return its notif_id (or None on error)."""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO notifications (
                target_audience, target_personnel_id, notification_type,
                person_name, tapped_personnel_id,
                rfid_uid, biometric_uid, biometric_id,
                action, status, message,
                subject_code, subject_name, class_section, classroom, tap_time
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING notif_id
        """, (
            target_audience, target_personnel_id,
            data.get('notification_type', 'rfid'),
            data.get('person_name'), data.get('personnel_id'),
            data.get('rfid_uid'), data.get('biometric_uid'), data.get('biometric_id'),
            data.get('action'), data.get('status'), data.get('message'),
            data.get('subject_code'), data.get('subject_name'),
            data.get('class_section'), data.get('classroom'),
            data.get('tap_time')
        ))
        notif_id = cursor.fetchone()[0]
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        return notif_id
    except Exception as e:
        print(f"Error saving notification to DB: {e}")
        return None

ensure_notifications_table()


def migrate_campus_attendance_status_constraint():
    """Allow 'Excused' in campus_attendance.status check constraint."""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            ALTER TABLE campus_attendance
                DROP CONSTRAINT IF EXISTS campus_attendance_status_check
        """)
        cursor.execute("""
            ALTER TABLE campus_attendance
                ADD CONSTRAINT campus_attendance_status_check
                CHECK (status IN ('Present', 'Late', 'Absent', 'Excused'))
        """)
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        print("campus_attendance status constraint updated.")
    except Exception as e:
        print(f"Error migrating campus_attendance constraint: {e}")


migrate_campus_attendance_status_constraint()
shared_serial = SharedSerialPort()
rfid_reader = RFIDReader(db_pool, shared_serial)
biometric_reader = BiometricReader(db_pool, shared_serial)

absence_checker_thread = None
absence_checker_running = False

rfid_reader_state = {
    'is_running': False,
    'port': None,
    'started_by': None,
    'started_at': None
}
rfid_state_lock = threading.Lock()

biometric_reader_state = {
    'is_running': False,
    'port': None,
    'started_by': None,
    'started_at': None
}
biometric_state_lock = threading.Lock()

def get_rfid_state():
    """Get current RFID reader state"""
    with rfid_state_lock:
        return rfid_reader_state.copy()

def update_rfid_state(is_running, port=None, started_by=None):
    """Update RFID reader state"""
    with rfid_state_lock:
        rfid_reader_state['is_running'] = is_running
        rfid_reader_state['port'] = port
        if started_by:
            rfid_reader_state['started_by'] = started_by
            rfid_reader_state['started_at'] = datetime.now(pytz.timezone('Asia/Manila')).isoformat()
        elif not is_running:
            rfid_reader_state['started_by'] = None
            rfid_reader_state['started_at'] = None

def get_biometric_state():
    """Get current biometric reader state"""
    with biometric_state_lock:
        return biometric_reader_state.copy()

def update_biometric_state(is_running, port=None, started_by=None):
    """Update biometric reader state"""
    with biometric_state_lock:
        biometric_reader_state['is_running'] = is_running
        biometric_reader_state['port'] = port
        if started_by:
            biometric_reader_state['started_by'] = started_by
            biometric_reader_state['started_at'] = datetime.now(pytz.timezone('Asia/Manila')).isoformat()
        elif not is_running:
            biometric_reader_state['started_by'] = None
            biometric_reader_state['started_at'] = None

def check_and_record_absences():
    """Independent absence checker - calculates dates from schedule and records absences"""
    global absence_checker_running
    
    while absence_checker_running:
        conn = None
        cursor = None
        try:
            philippines_tz = pytz.timezone('Asia/Manila')
            current_time = datetime.now(philippines_tz)
            current_date = current_time.date()
            current_time_only = current_time.time()

            conn = db_pool.get_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                WITH current_calendar AS (
                    SELECT acadcalendar_id, semesterstart, semesterend
                    FROM acadcalendar 
                    WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                    ORDER BY semesterstart DESC LIMIT 1
                )
                SELECT 
                    sch.class_id, sch.personnel_id, 
                    sch.classday_1, sch.starttime_1, sch.endtime_1,
                    sch.classday_2, sch.starttime_2, sch.endtime_2,
                    sub.subjectcode, sub.subjectname, sch.classsection, sch.classroom,
                    p.firstname, p.lastname,
                    cc.semesterstart, cc.semesterend
                FROM schedule sch
                JOIN subjects sub ON sch.subject_id = sub.subject_id
                JOIN personnel p ON sch.personnel_id = p.personnel_id
                CROSS JOIN current_calendar cc
                WHERE sch.acadcalendar_id = cc.acadcalendar_id
                AND p.role_id IN (20001, 20002)
            """)
            
            all_schedules = cursor.fetchall()
            absences_recorded = 0
            
            weekday_map = {
                'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
                'Friday': 4, 'Saturday': 5, 'Sunday': 6
            }
            
            for schedule in all_schedules:
                (class_id, personnel_id, day1, start1, end1, day2, start2, end2, 
                 subject_code, subject_name, class_section, classroom, firstname, lastname,
                 semester_start, semester_end) = schedule
                
                for day, start_time, end_time in [(day1, start1, end1), (day2, start2, end2)]:
                    if not day or not start_time or not end_time:
                        continue
                    
                    if isinstance(start_time, str):
                        start_t = datetime.strptime(start_time[:8], '%H:%M:%S').time()
                    else:
                        start_t = start_time
                    
                    if isinstance(end_time, str):
                        end_t = datetime.strptime(end_time[:8], '%H:%M:%S').time()
                    else:
                        end_t = end_time
                    
                    end_dt = datetime.combine(datetime.today(), end_t)
                    absence_cutoff_dt = end_dt + timedelta(minutes=15)
                    absence_cutoff = absence_cutoff_dt.time()
                    
                    target_weekday = weekday_map.get(day)
                    if target_weekday is None:
                        continue
                    
                    check_date = semester_start
                    while check_date.weekday() != target_weekday:
                        check_date += timedelta(days=1)
                    
                    while check_date <= current_date and check_date <= semester_end:
                        should_record_absence = False
                        
                        if check_date < current_date:
                            should_record_absence = True
                        elif check_date == current_date:
                            if current_time_only > absence_cutoff:
                                should_record_absence = True
                        
                        if should_record_absence:
                            cursor.execute("""
                                SELECT attendance_id 
                                FROM attendance 
                                WHERE personnel_id = %s 
                                AND class_id = %s 
                                AND DATE(timein AT TIME ZONE 'Asia/Manila') = %s
                            """, (personnel_id, class_id, check_date))
                            
                            existing = cursor.fetchone()
                            
                            if not existing:
                                naive_midnight = datetime.combine(check_date, datetime.min.time())
                                absence_timestamp = philippines_tz.localize(naive_midnight)

                                cursor.execute("""
                                    INSERT INTO attendance (
                                        personnel_id, class_id,
                                        attendancestatus, timein, timeout
                                    )
                                    VALUES (%s, %s, %s, %s, NULL)
                                """, (personnel_id, class_id, 'Absent', absence_timestamp))
                                
                                absences_recorded += 1
                                
                                print(f"ABSENCE RECORDED: {lastname}, {firstname} - {subject_code} on {check_date}")

                                try:
                                    cursor.execute("""
                                        SELECT acadcalendar_id FROM schedule WHERE class_id = %s
                                    """, (class_id,))
                                    acadcal_result = cursor.fetchone()
                                    
                                    if acadcal_result:
                                        acadcalendar_id = acadcal_result[0]
                                        
                                        cursor.execute("""
                                            SELECT 
                                                COUNT(*) FILTER (WHERE attendancestatus = 'Present') as present,
                                                COUNT(*) FILTER (WHERE attendancestatus = 'Late') as late,
                                                COUNT(*) FILTER (WHERE attendancestatus = 'Excused') as excused,
                                                COUNT(*) FILTER (WHERE attendancestatus = 'Absent') as absent,
                                                COUNT(*) as total
                                            FROM attendance
                                            WHERE personnel_id = %s AND class_id = %s
                                        """, (personnel_id, class_id))
                                        
                                        stats = cursor.fetchone()
                                        if stats:
                                            present, late, excused, absent, total = stats
                                            
                                            if total > 0:
                                                attendance_rate = ((present + excused + (late * 0.75)) / total) * 100
                                            else:
                                                attendance_rate = 0.0

                                            cursor.execute("""
                                                SELECT attendancereport_id FROM attendancereport
                                                WHERE personnel_id = %s AND class_id = %s AND acadcalendar_id = %s
                                            """, (personnel_id, class_id, acadcalendar_id))
                                            
                                            existing_report = cursor.fetchone()
                                            
                                            if existing_report:
                                                cursor.execute("""
                                                    UPDATE attendancereport 
                                                    SET presentcount = %s, latecount = %s, excusedcount = %s,
                                                        absentcount = %s, totalclasses = %s, attendancerate = %s,
                                                        lastupdated = CURRENT_TIMESTAMP
                                                    WHERE attendancereport_id = %s
                                                """, (present, late, excused, absent, total, attendance_rate, existing_report[0]))
                                            else:
                                                cursor.execute("SELECT COALESCE(MAX(attendancereport_id), 130000) + 1 FROM attendancereport")
                                                new_report_id = cursor.fetchone()[0]
                                                
                                                cursor.execute("""
                                                    INSERT INTO attendancereport (
                                                        attendancereport_id, personnel_id, class_id, acadcalendar_id,
                                                        presentcount, latecount, excusedcount, absentcount,
                                                        totalclasses, attendancerate, lastupdated
                                                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                                                """, (new_report_id, personnel_id, class_id, acadcalendar_id,
                                                      present, late, excused, absent, total, attendance_rate))
                                            
                                            print(f"📊 Updated report after absence: Rate={attendance_rate:.2f}%")
                                            
                                except Exception as e:
                                    print(f"⚠️ Could not update attendance report after absence: {e}")
                        
                        check_date += timedelta(days=7)
            
            if absences_recorded > 0:
                conn.commit()
                print(f"Total absences recorded: {absences_recorded}")
            
            cursor.close()
            db_pool.return_connection(conn)
            
        except Exception as e:
            print(f"Error in absence checker: {e}")
            if conn:
                try:
                    conn.rollback()
                    cursor.close()
                    db_pool.return_connection(conn)
                except:
                    pass
        
        time.sleep(60)


def start_absence_checker():
    """Start the independent absence checker thread"""
    global absence_checker_thread, absence_checker_running
    
    if absence_checker_running:
        print("⚠️ Absence checker already running")
        return
    
    absence_checker_running = True
    absence_checker_thread = threading.Thread(target=check_and_record_absences, daemon=True)
    absence_checker_thread.start()
    print("🚀 Independent absence checker started - runs every 1 minute")

def stop_absence_checker():
    """Stop the absence checker thread"""
    global absence_checker_running
    absence_checker_running = False
    print("🛑 Absence checker stopped")

start_absence_checker()
atexit.register(stop_absence_checker)

# ========== LICENSE EXPIRY CHECKER ==========
license_expiry_checker_running = False
license_expiry_checker_thread = None

def check_license_expiry():
    """Background thread: runs daily and broadcasts alerts for expiring/expired licenses."""
    global license_expiry_checker_running
    philippines_tz = pytz.timezone('Asia/Manila')

    while license_expiry_checker_running:
        conn = None
        cursor = None
        try:
            today = datetime.now(philippines_tz).date()

            conn = db_pool.get_connection()
            cursor = conn.cursor()

            cursor.execute("""
                SELECT lt.tracker_id, lt.personnel_id, lt.license_type, lt.license_number,
                       lt.expiration_date, pe.firstname, pe.lastname
                FROM faculty_license_tracker lt
                JOIN personnel pe ON lt.personnel_id = pe.personnel_id
                WHERE lt.expiration_date IS NOT NULL
                  AND lt.expiration_date <= CURRENT_DATE + INTERVAL '90 days'
            """)
            rows = cursor.fetchall()
            cursor.close()
            db_pool.return_connection(conn)
            conn = None

            for row in rows:
                tracker_id, personnel_id, license_type, license_number, expiration_date, firstname, lastname = row
                days = (expiration_date - today).days

                if days < 0:
                    action = 'expired'
                    msg = (f'{license_type or "License"} license'
                           f'{" (No. " + license_number + ")" if license_number else ""}'
                           f' expired on {expiration_date}.')
                elif days <= 30:
                    action = 'expiring_30'
                    msg = (f'{license_type or "License"} license'
                           f'{" (No. " + license_number + ")" if license_number else ""}'
                           f' expires in {days} day(s) on {expiration_date}.')
                elif days <= 60:
                    action = 'expiring_60'
                    msg = (f'{license_type or "License"} license'
                           f'{" (No. " + license_number + ")" if license_number else ""}'
                           f' expires in {days} day(s) on {expiration_date}.')
                else:
                    action = 'expiring_90'
                    msg = (f'{license_type or "License"} license'
                           f'{" (No. " + license_number + ")" if license_number else ""}'
                           f' expires in {days} day(s) on {expiration_date}.')

                # Dedup: only send each action milestone once per tracker
                try:
                    conn2 = db_pool.get_connection()
                    cur2 = conn2.cursor()
                    cur2.execute("""
                        SELECT 1 FROM notifications
                        WHERE notification_type = 'license'
                          AND action = %s
                          AND rfid_uid = %s
                        LIMIT 1
                    """, (action, str(tracker_id)))
                    already_sent = cur2.fetchone() is not None
                    cur2.close()
                    db_pool.return_connection(conn2)
                except Exception:
                    already_sent = False

                if already_sent:
                    continue

                now_str = datetime.now(philippines_tz).strftime('%A, %B %d, %Y %I:%M %p')
                notification_data = {
                    'notification_type': 'license',
                    'action': action,
                    'personnel_id': personnel_id,
                    'person_name': f"{firstname} {lastname}",
                    'message': msg,
                    # Store license fields in spare DB columns for history retrieval
                    'subject_code': license_type,
                    'subject_name': license_number,
                    'class_section': str(expiration_date),
                    'classroom': str(days),
                    'rfid_uid': str(tracker_id),  # used for dedup
                    'tap_time': now_str,
                    # Extra fields for live SSE rendering
                    'license_type': license_type,
                    'license_number': license_number,
                    'expiration_date': str(expiration_date),
                    'days_until_expiry': days,
                }
                _push_to_queue(personnel_id, notification_data)
                print(f"📋 License expiry notification sent to faculty {personnel_id}: {action} — {msg}")

        except Exception as e:
            print(f"Error in license expiry checker: {e}")
            if conn:
                try:
                    cursor.close()
                    db_pool.return_connection(conn)
                except:
                    pass

        time.sleep(86400)  # re-check once every 24 hours


def start_license_expiry_checker():
    global license_expiry_checker_thread, license_expiry_checker_running
    if license_expiry_checker_running:
        return
    license_expiry_checker_running = True
    license_expiry_checker_thread = threading.Thread(target=check_license_expiry, daemon=True)
    license_expiry_checker_thread.start()


def stop_license_expiry_checker():
    global license_expiry_checker_running
    license_expiry_checker_running = False


start_license_expiry_checker()
atexit.register(stop_license_expiry_checker)

# ========== CAMPUS ATTENDANCE DAILY AUTO-SYNC ==========
campus_sync_running = False
campus_sync_thread = None

def campus_attendance_daily_sync():
    """Background thread: syncs campus attendance once at startup, then every 24 hours."""
    global campus_sync_running
    while campus_sync_running:
        conn = None
        cursor = None
        try:
            conn = db_pool.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                WITH
                active_dates AS (
                    SELECT d::date AS attendance_date
                    FROM generate_series(
                        '2026-01-05'::date,
                        (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Manila')::date,
                        INTERVAL '1 day'
                    ) d
                ),
                all_faculty AS (
                    SELECT personnel_id FROM personnel
                ),
                all_combinations AS (
                    SELECT
                        d.attendance_date,
                        p.personnel_id,
                        s.session,
                        s.window_start,
                        s.window_end,
                        s.late_threshold
                    FROM active_dates d
                    CROSS JOIN all_faculty p
                    CROSS JOIN (VALUES
                        ('Morning',   420,  764, 495),
                        ('Afternoon', 765, 1065, 825)
                    ) AS s(session, window_start, window_end, late_threshold)
                ),
                taps AS (
                    SELECT
                        b.personnel_id,
                        DATE(bl.taptime AT TIME ZONE 'Asia/Manila')                    AS tap_date,
                        bl.taptime AT TIME ZONE 'Asia/Manila'                          AS local_dt,
                        EXTRACT(HOUR  FROM bl.taptime AT TIME ZONE 'Asia/Manila') * 60
                      + EXTRACT(MINUTE FROM bl.taptime AT TIME ZONE 'Asia/Manila')     AS tap_mins
                    FROM biometriclogs bl
                    INNER JOIN biometric b ON bl.biometric_id = b.biometric_id
                    WHERE b.personnel_id IS NOT NULL
                ),
                session_agg AS (
                    SELECT
                        ac.personnel_id,
                        ac.attendance_date,
                        ac.session,
                        ac.late_threshold,
                        MIN(t.local_dt) AS time_in,
                        MAX(t.local_dt) AS time_out
                    FROM all_combinations ac
                    LEFT JOIN taps t
                        ON  t.personnel_id = ac.personnel_id
                        AND t.tap_date     = ac.attendance_date
                        AND t.tap_mins BETWEEN ac.window_start AND ac.window_end
                    GROUP BY ac.personnel_id, ac.attendance_date, ac.session, ac.late_threshold
                )
                INSERT INTO campus_attendance
                    (personnel_id, attendance_date, session, time_in, time_out, status)
                SELECT
                    personnel_id,
                    attendance_date,
                    session,
                    time_in,
                    time_out,
                    CASE
                        WHEN time_in IS NULL THEN 'Absent'
                        WHEN (EXTRACT(HOUR  FROM time_in) * 60
                            + EXTRACT(MINUTE FROM time_in)) <= late_threshold THEN 'Present'
                        ELSE 'Late'
                    END AS status
                FROM session_agg
                ON CONFLICT (personnel_id, attendance_date, session)
                DO UPDATE SET
                    time_in  = EXCLUDED.time_in,
                    time_out = EXCLUDED.time_out,
                    status   = EXCLUDED.status
            """)
            conn.commit()
        except Exception as e:
            print(f"Error in campus attendance auto-sync: {e}")
            if conn:
                try:
                    conn.rollback()
                    cursor.close()
                    db_pool.return_connection(conn)
                except:
                    pass
        else:
            if cursor:
                cursor.close()
            if conn:
                db_pool.return_connection(conn)
        time.sleep(86400)  # re-run every 24 hours


def start_campus_sync():
    global campus_sync_thread, campus_sync_running
    if campus_sync_running:
        return
    campus_sync_running = True
    campus_sync_thread = threading.Thread(target=campus_attendance_daily_sync, daemon=True)
    campus_sync_thread.start()


def stop_campus_sync():
    global campus_sync_running
    campus_sync_running = False


start_campus_sync()
atexit.register(stop_campus_sync)

def get_db_connection():
    return db_pool.get_connection()

def return_db_connection(conn):
    db_pool.return_connection(conn)



# ========== Google Sheets ==========
def _get_gsheets_creds(scopes):
    """Load Google service account credentials from env var or fallback to file."""
    import json as _json
    creds_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
    if creds_json:
        return Credentials.from_service_account_info(_json.loads(creds_json), scopes=scopes)
    return Credentials.from_service_account_file('spcheck-ingest-key.json', scopes=scopes)

def get_students_score_records():
    SERVICE_ACCOUNT_FILE = 'spcheck-ingest-key.json'
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    
    try:
        print("🟡 [SHEETS] Attempting to authorize Google Sheets API...")
        creds = _get_gsheets_creds(SCOPES)
        gc = gspread.authorize(creds)
        print("✅ [SHEETS] Authorization successful.")
        
        url = 'https://docs.google.com/spreadsheets/d/1uWiA1_c5fVqYf1dNwAZgtA5xAaUrgMAHH3BtABIZh-Y/edit'
        SHEET_NAME = 'Students Score'
        
        sh = gc.open_by_url(url)
        print(f"✅ [SHEETS] Opened spreadsheet: {sh.title}")
        
        worksheet = sh.worksheet(SHEET_NAME)
        print(f"✅ [SHEETS] Accessed worksheet: {SHEET_NAME}")
        
        records = worksheet.get_all_records()
        print(f"✅ [SHEETS] Successfully fetched {len(records)} records.")
        return records
    except FileNotFoundError:
        print(f"❌ [SHEETS] ERROR: Service account file '{SERVICE_ACCOUNT_FILE}' not found.")
        raise
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"❌ [SHEETS] ERROR: Spreadsheet URL not found or unauthorized.")
        raise
    except gspread.exceptions.WorksheetNotFound:
        print(f"❌ [SHEETS] ERROR: Worksheet '{SHEET_NAME}' not found.")
        raise
    except Exception as e:
        print(f"❌ [SHEETS] Unhandled error during Sheets fetching: {e}")
        raise

def get_supervisors_score_records():
    """Fetch all score records from the 'Supervisor Score' tab of the Google Sheet."""
    SERVICE_ACCOUNT_FILE = 'spcheck-ingest-key.json'
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    SHEET_NAME = 'Supervisors Score'  
    
    try:
        print(f"🟡 [SHEETS] Attempting to fetch records from: {SHEET_NAME}")
        creds = _get_gsheets_creds(SCOPES)
        gc = gspread.authorize(creds)
        
        url = 'https://docs.google.com/spreadsheets/d/1uWiA1_c5fVqYf1dNwAZgtA5xAaUrgMAHH3BtABIZh-Y/edit'
        sh = gc.open_by_url(url)
        
        worksheet = sh.worksheet(SHEET_NAME)
        records = worksheet.get_all_records()
        print(f"✅ [SHEETS] Successfully fetched {len(records)} records from {SHEET_NAME}.")
        return records
    except FileNotFoundError:
        print(f"❌ [SHEETS] ERROR: Service account file '{SERVICE_ACCOUNT_FILE}' not found for {SHEET_NAME}.")
        raise
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"❌ [SHEETS] ERROR: Spreadsheet URL not found or unauthorized for {SHEET_NAME}.")
        return []
    except gspread.exceptions.WorksheetNotFound:
        print(f"❌ [SHEETS] ERROR: Worksheet '{SHEET_NAME}' not found. Returning empty list.")
        return []
    except Exception as e:
        print(f"❌ [SHEETS] Unhandled error fetching {SHEET_NAME} data: {e}")
        return []

def get_peers_score_records():
    """Fetch all score records from the 'Peer Score' tab of the Google Sheet."""
    SERVICE_ACCOUNT_FILE = 'spcheck-ingest-key.json'
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    SHEET_NAME = 'Peers Score'  
    
    try:
        print(f"🟡 [SHEETS] Attempting to fetch records from: {SHEET_NAME}")
        creds = _get_gsheets_creds(SCOPES)
        gc = gspread.authorize(creds)
        
        url = 'https://docs.google.com/spreadsheets/d/1uWiA1_c5fVqYf1dNwAZgtA5xAaUrgMAHH3BtABIZh-Y/edit'
        sh = gc.open_by_url(url)
        
        worksheet = sh.worksheet(SHEET_NAME)
        records = worksheet.get_all_records()
        print(f"✅ [SHEETS] Successfully fetched {len(records)} records from {SHEET_NAME}.")
        return records
    except FileNotFoundError:
        print(f"❌ [SHEETS] ERROR: Service account file '{SERVICE_ACCOUNT_FILE}' not found for {SHEET_NAME}.")
        raise
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"❌ [SHEETS] ERROR: Spreadsheet URL not found or unauthorized for {SHEET_NAME}.")
        return []
    except gspread.exceptions.WorksheetNotFound:
        print(f"❌ [SHEETS] ERROR: Worksheet '{SHEET_NAME}' not found. Returning empty list.")
        return []
    except Exception as e:
        print(f"❌ [SHEETS] Unhandled error fetching {SHEET_NAME} data: {e}")
        return []


# Helper function for ReportLab 
def getStatusLabel(rating):
    if rating >= 3:
        return 'Above Average'
    elif rating >= 2:
        return 'Average'
    elif rating > 0:
        return 'Below Average'
    else:
        return 'Not Rated'
    
    
def get_current_acadcalendar_info(acadcalendar_id):
    """Fetches semester name, year, and end date for display."""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT semester, acadyear, semesterend
            FROM acadcalendar
            WHERE acadcalendar_id = %s
        """, (acadcalendar_id,))
        
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if result:
            semester, acadyear, semesterend = result
            
            # 1. Clean the semester name (ensure "Semester" is included)
            if 'semester' not in semester.lower():
                semester_display = f"{semester} Semester"
            else:
                semester_display = semester
            
            # 2. Clean the academic year (remove leading 'AY' if present)
            acadyear_clean = acadyear.upper().lstrip('AY').strip()
            
            return {
                'semester_name': semester_display,
                'acad_year': acadyear_clean,
                'deadline': semesterend.strftime('%b %d, %Y'), # e.g., Dec 20, 2025
                # 3. Construct the final display string: "📅 First Semester — AY 2025-2026"
                'display': f"📅 {semester_display} — {acadyear_clean}"
            }
        
    except Exception as e:
        print(f"Error fetching acad calendar info: {e}")
        
    return {
        'semester_name': 'N/A',
        'acad_year': 'N/A',
        'deadline': 'N/A',
        'display': '📅 N/A — AY N/A'
    }



# ========== CACHED DATA ==========
_cache = {}
_cache_lock = threading.Lock()

def get_cached(key, ttl=300):
    """Get cached data if not expired"""
    with _cache_lock:
        if key in _cache:
            data, timestamp = _cache[key]
            if datetime.now().timestamp() - timestamp < ttl:
                return data
    return None

def set_cached(key, data):
    """Set cached data"""
    with _cache_lock:
        _cache[key] = (data, datetime.now().timestamp())

ROLE_REDIRECTS = {
    20001: ('faculty', 'faculty_dashboard'),
    20002: ('dean', 'faculty_dashboard'),
    20003: ('hrmd', 'hr_dashboard'),
    20004: ('vppres', 'vp_promotions')
}

@app.context_processor
def inject_pending_counts():
    """
    Inject role-aware pending counts and is_vpaa into every template context.
    Keeps sidebar badges and VP role distinction consistent across all pages.
    """
    role_id = session.get('user_role')
    if not role_id:
        return {}

    result = {}
    conn = None
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        if role_id == 20003:  # HR
            cursor.execute(
                "SELECT COUNT(*) FROM promotion_application "
                "WHERE current_status = 'hrmd' AND final_decision IS NULL"
            )
            result['pending_hr_promo_count'] = cursor.fetchone()[0] or 0

        elif role_id == 20004:  # VP / President
            # Determine VPAA vs President from the logged-in user's position
            user_id = session.get('user_id')
            if user_id:
                info = get_personnel_info(user_id)
                pos = (info.get('position') or '').lower()
                is_vpaa = 'president' not in pos
            else:
                is_vpaa = True

            result['is_vpaa'] = is_vpaa

            # Count promotions waiting for this specific role's action
            status_key = 'vpa' if is_vpaa else 'pres'
            cursor.execute(
                "SELECT COUNT(*) FROM promotion_application "
                "WHERE current_status = %s AND final_decision IS NULL",
                (status_key,)
            )
            result['pending_vp_promo_count'] = cursor.fetchone()[0] or 0

        cursor.close()
        db_pool.return_connection(conn)
    except Exception:
        if conn:
            try:
                db_pool.return_connection(conn)
            except Exception:
                pass

    return result

def get_personnel_info(user_id):
    """Get personnel information with profile picture - OPTIMIZED with single query"""
    cache_key = f"personnel_info_{user_id}"
    cached = get_cached(cache_key, ttl=600)
    if cached:
        return cached
    
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                p.firstname,
                p.lastname,
                p.honorifics,
                c.collegename,
                p.employee_no,
                r.rolename,
                u.email,
                pr.position,
                pr.employmentstatus,
                p.personnel_id,
                pr.profilepic
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN roles r ON p.role_id = r.role_id
            LEFT JOIN users u ON p.user_id = u.user_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE p.user_id = %s
        """, (user_id,))
        
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus, personnel_id, profilepic = result
            
            full_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            
            profile_image_base64 = None
            if profilepic:
                binary_image = bytes(profilepic)
                profile_image_base64 = f'data:image/jpeg;base64,{base64.b64encode(binary_image).decode("utf-8")}'
            
            info = {
                'personnel_name': full_name,
                'faculty_name': full_name,
                'hr_name': full_name,
                'vp_name': full_name,
                'college': collegename or 'College of Computer Studies',
                'employee_no': employee_no,
                'firstname': firstname,
                'lastname': lastname,
                'honorifics': honorifics,
                'role_name': rolename or 'Staff',
                'email': email or 'email@spc.edu.ph',
                'position': position or 'Full-Time Employee',
                'employment_status': employmentstatus or 'Regular',
                'personnel_id': personnel_id,
                'profile_image_base64': profile_image_base64  # ADD THIS
            }
            
            set_cached(cache_key, info)
            return info
    except Exception as e:
        print(f"Error getting personnel info: {e}")
    
    return {
        'personnel_name': 'Staff Member',
        'faculty_name': 'Prof. Santos',
        'hr_name': 'HR Staff',
        'vp_name': 'VP Admin',
        'college': 'College of Computer Studies',
        'employee_no': None,
        'firstname': 'Staff',
        'lastname': 'Member',
        'honorifics': None,
        'role_name': 'Staff',
        'email': 'email@spc.edu.ph',
        'position': 'Full-Time Employee',
        'employment_status': 'Regular',
        'profile_image_base64': None
    }

def get_faculty_info(user_id):
    """Get faculty information - wrapper for get_personnel_info"""
    return get_personnel_info(user_id)

def require_auth(allowed_roles):
    def decorator(func):
        def wrapper(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))

            user_role = session.get('user_role')
            if user_role not in allowed_roles:
                return redirect(url_for('login'))

            return func(*args, **kwargs)
        wrapper.__name__ = func.__name__
        return wrapper
    return decorator

@app.route('/api/faculty/attendance')
@require_auth([20001, 20002])
def api_faculty_attendance():
    """OPTIMIZED: Single complex query instead of multiple queries"""
    try:
        user_id = session['user_id']
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            WITH current_calendar AS (
                SELECT acadcalendar_id, semesterstart, semesterend
                FROM acadcalendar 
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC
                LIMIT 1
            ),
            faculty_schedule AS (
                SELECT 
                    sch.class_id,
                    sch.classday_1,
                    sch.starttime_1,
                    sch.endtime_1,
                    sch.classday_2,
                    sch.starttime_2,
                    sch.endtime_2,
                    sub.subjectcode,
                    sub.subjectname,
                    sch.classsection,
                    sch.classroom
                FROM schedule sch
                JOIN subjects sub ON sch.subject_id = sub.subject_id
                JOIN personnel p ON sch.personnel_id = p.personnel_id
                CROSS JOIN current_calendar cc
                WHERE p.user_id = %s AND sch.acadcalendar_id = cc.acadcalendar_id
            ),
            faculty_attendance AS (
                SELECT 
                    a.class_id,
                    a.attendancestatus,
                    a.timein,
                    a.timeout,
                    sub.subjectcode,
                    sub.subjectname,
                    sch.classsection,
                    sch.classroom
                FROM attendance a
                JOIN schedule sch ON a.class_id = sch.class_id
                JOIN subjects sub ON sch.subject_id = sub.subject_id
                JOIN personnel p ON a.personnel_id = p.personnel_id
                WHERE p.user_id = %s
            )
            SELECT 
                (SELECT acadcalendar_id FROM current_calendar),
                (SELECT semesterstart FROM current_calendar),
                (SELECT semesterend FROM current_calendar),
                (SELECT json_agg(row_to_json(faculty_schedule)) FROM faculty_schedule),
                (SELECT json_agg(row_to_json(faculty_attendance)) FROM faculty_attendance)
        """, (user_id, user_id))
        
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if not result or result[0] is None:
            return {'success': False, 'error': 'No active academic calendar found'}
        
        acadcalendar_id, semester_start, semester_end, scheduled_classes_json, attendance_records_json = result
        
        scheduled_classes = scheduled_classes_json or []
        attendance_records = attendance_records_json or []
        
        attendance_map = {}
        for record in attendance_records:
            class_id = record['class_id']
            timein = record['timein']
            if timein:
                date_key = f"{class_id}_{timein[:10]}" 
            else:
                date_key = f"{class_id}_absent_{len(attendance_map)}"
            attendance_map[date_key] = record
        
        attendance_logs = []
        class_attendance = []
        status_counts = {'present': 0, 'late': 0, 'absent': 0, 'excused': 0}
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_date = datetime.now(philippines_tz).date()
        
        weekday_map = {
            'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
            'Friday': 4, 'Saturday': 5, 'Sunday': 6
        }
        
        for scheduled_class in scheduled_classes:
            class_id = scheduled_class['class_id']
            subject_code = scheduled_class['subjectcode']
            subject_name = scheduled_class['subjectname']
            class_section = scheduled_class['classsection']
            classroom = scheduled_class['classroom']
            
            class_name = f"{subject_code} - {subject_name}"
            
            for day_key in ['1', '2']:
                day = scheduled_class.get(f'classday_{day_key}')
                if not day:
                    continue
                
                target_weekday = weekday_map.get(day)
                if target_weekday is None:
                    continue
                
                check_date = semester_start
                days_ahead = target_weekday - check_date.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                check_date += timedelta(days=days_ahead)
                
                if check_date < semester_start:
                    check_date += timedelta(days=7)
                
                while check_date <= current_date and check_date <= semester_end:
                    date_key = f"{class_id}_{check_date}"
                    
                    found_record = attendance_map.get(date_key)
                    if not found_record:
                        for key, record in attendance_map.items():
                            if record['class_id'] == class_id and not record['timein']:
                                found_record = record
                                break
                    
                    if found_record:
                        status = found_record['attendancestatus']
                        timein = found_record['timein']
                        timeout = found_record['timeout']
                        
                        time_in_str = timein[11:16] if timein else '—'
                        time_out_str = timeout[11:16] if timeout else '—'
                    else:
                        status = 'Absent'
                        time_in_str = '—'
                        time_out_str = '—'
                    
                    log_entry = {
                        'date': check_date.strftime('%Y-%m-%d'),
                        'time_in': time_in_str,
                        'time_out': time_out_str,
                        'status': status.capitalize(),
                        'class_name': class_name,
                        'class_section': class_section or 'N/A',
                        'classroom': classroom or 'N/A'
                    }
                    attendance_logs.append(log_entry)
                    
                    class_entry = {
                        'class_name': class_name,
                        'class_section': class_section or 'N/A',
                        'classroom': classroom or 'N/A',
                        'date': check_date.strftime('%Y-%m-%d'),
                        'time_in': time_in_str,
                        'status': status.capitalize()
                    }
                    class_attendance.append(class_entry)
                    
                    status_lower = status.lower()
                    if status_lower == 'present':
                        status_counts['present'] += 1
                    elif status_lower == 'late':
                        status_counts['late'] += 1
                    elif status_lower == 'absent':
                        status_counts['absent'] += 1
                    elif status_lower == 'excused':
                        status_counts['excused'] += 1
                    
                    check_date += timedelta(days=7)
        
        attendance_logs.sort(key=lambda x: x['date'], reverse=True)
        class_attendance.sort(key=lambda x: x['date'], reverse=True)
        
        total_classes = len(attendance_logs)
        attendance_percent = round((status_counts['present'] + status_counts['late']) / total_classes * 100, 1) if total_classes > 0 else 0
        
        kpis = {
            'attendance_percent': f'{attendance_percent}%',
            'late_count': status_counts['late'],
            'absence_count': status_counts['absent'],
            'total_classes': total_classes
        }
        
        return {
            'success': True,
            'attendance_logs': attendance_logs,
            'class_attendance': class_attendance,
            'status_breakdown': status_counts,
            'kpis': kpis
        }
        
    except Exception as e:
        print(f"Error fetching attendance data: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/rfid/start', methods=['POST'])
@require_auth([20001, 20002, 20003]) 
def api_rfid_start():
    """Start the RFID reader - accessible by Faculty, Dean, and HR"""
    try:
        state = get_rfid_state()
        if state['is_running']:
            return {
                'success': True, 
                'message': f"RFID reader already running on {state['port']}",
                'port': state['port'],
                'already_running': True
            }
        
        data = request.get_json(silent=True) or {}
        port = data.get('port') 
        
        result = rfid_reader.start_reading(port)
        
        if result.get('success'):
            user_id = session.get('user_id')
            personnel_info = get_personnel_info(user_id)
            started_by = personnel_info.get('personnel_name', 'Unknown')
            
            update_rfid_state(
                is_running=True, 
                port=result.get('port'),
                started_by=started_by
            )
            
            print(f"✅ RFID Reader started by: {started_by}")
        
        return result
        
    except Exception as e:
        print(f"Error starting RFID reader: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/rfid/stop', methods=['POST'])
@require_auth([20001, 20002, 20003]) 
def api_rfid_stop():
    """Stop the RFID reader - accessible by Faculty, Dean, and HR"""
    try:
        result = rfid_reader.stop_reading()
        
        if result.get('success'):
            update_rfid_state(is_running=False, port=None)
            
            user_id = session.get('user_id')
            personnel_info = get_personnel_info(user_id)
            stopped_by = personnel_info.get('personnel_name', 'Unknown')
            print(f"🛑 RFID Reader stopped by: {stopped_by}")
        
        return result
        
    except Exception as e:
        print(f"Error stopping RFID reader: {e}")
        return {'success': False, 'error': str(e)}

@app.route('/api/rfid/status')
@require_auth([20001, 20002, 20003]) 
def api_rfid_status():
    """Get RFID reader status - accessible by Faculty, Dean, and HR"""
    try:
        state = get_rfid_state()
        return {
            'success': True,
            'is_running': state['is_running'],
            'port': state['port'],
            'started_by': state.get('started_by'),
            'started_at': state.get('started_at')
        }
        
    except Exception as e:
        print(f"Error getting RFID status: {e}")
        return {'success': False, 'error': str(e)}

@app.route('/api/rfid/ports')
@require_auth([20001, 20002, 20003])  
def api_rfid_ports():
    """Get available serial ports - accessible by Faculty, Dean, and HR"""
    try:
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        
        port_list = []
        for port in ports:
            port_list.append({
                'device': port.device,
                'description': port.description,
                'hwid': port.hwid
            })
        
        return {
            'success': True,
            'ports': port_list
        }
        
    except Exception as e:
        print(f"Error listing ports: {e}")
        return {'success': False, 'error': str(e)}

# ==================== BIOMETRIC API ENDPOINTS ====================

@app.route('/api/biometric/start', methods=['POST'])
@require_auth([20001, 20002, 20003])
def api_biometric_start():
    """Start the biometric reader - accessible by Faculty, Dean, and HR"""
    try:
        state = get_biometric_state()
        if state['is_running']:
            return {
                'success': True,
                'message': f"Biometric reader already running on {state['port']}",
                'port': state['port'],
                'already_running': True
            }

        data = request.get_json(silent=True) or {}
        port = data.get('port')

        result = biometric_reader.start_reading(port)

        if result.get('success'):
            user_id = session.get('user_id')
            personnel_info = get_personnel_info(user_id)
            started_by = personnel_info.get('personnel_name', 'Unknown')

            update_biometric_state(
                is_running=True,
                port=result.get('port'),
                started_by=started_by
            )

            print(f"✅ Biometrics Reader started by: {started_by}")

        return result

    except Exception as e:
        print(f"Error starting biometric reader: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/biometric/stop', methods=['POST'])
@require_auth([20001, 20002, 20003])
def api_biometric_stop():
    """Stop the biometric reader - accessible by Faculty, Dean, and HR"""
    try:
        result = biometric_reader.stop_reading()

        if result.get('success'):
            update_biometric_state(is_running=False, port=None)

            user_id = session.get('user_id')
            personnel_info = get_personnel_info(user_id)
            stopped_by = personnel_info.get('personnel_name', 'Unknown')
            print(f"🛑 Biometric Reader stopped by: {stopped_by}")

        return result

    except Exception as e:
        print(f"Error stopping biometric reader: {e}")
        return {'success': False, 'error': str(e)}

@app.route('/api/biometric/status')
@require_auth([20001, 20002, 20003])
def api_biometric_status():
    """Get biometric reader status - accessible by Faculty, Dean, and HR"""
    try:
        state = get_biometric_state()
        return {
            'success': True,
            'is_running': state['is_running'],
            'port': state['port'],
            'started_by': state.get('started_by'),
            'started_at': state.get('started_at')
        }

    except Exception as e:
        print(f"Error getting biometric status: {e}")
        return {'success': False, 'error': str(e)}

@app.route('/api/biometric/logs')
@require_auth([20003])
def api_biometric_logs():
    """Get biometric logs - accessible by HR only"""
    conn = None
    cursor = None
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        semester_id = request.args.get('semester_id')
        sem_start = sem_end = None
        if semester_id:
            cursor.execute("SELECT semesterstart, semesterend FROM acadcalendar WHERE acadcalendar_id = %s", (int(semester_id),))
            row = cursor.fetchone()
            if row:
                sem_start, sem_end = row[0], row[1]

        date_filter = "WHERE bl.taptime::date BETWEEN %s AND %s" if (sem_start and sem_end) else ""
        params = [sem_start, sem_end] if (sem_start and sem_end) else []

        cursor.execute("""
            SELECT
                bl.biometriclog_id,
                bl.biometric_id,
                bl.taptime,
                bl.status,
                bl.remarks,
                b.biometric_uid,
                p.firstname,
                p.lastname,
                p.honorifics,
                c.collegename,
                pr.position,
                pr.employmentstatus
            FROM biometriclogs bl
            LEFT JOIN biometric b ON bl.biometric_id = b.biometric_id
            LEFT JOIN personnel p ON b.personnel_id = p.personnel_id
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            """ + date_filter + """
            ORDER BY bl.taptime DESC
            LIMIT 500
        """, params)

        logs = []
        for row in cursor.fetchall():
            firstname, lastname, honorifics = row[6], row[7], row[8]
            collegename, position, employmentstatus = row[9], row[10], row[11]
            if firstname and lastname:
                person_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            else:
                person_name = "Unknown"
            logs.append({
                'log_id': row[0],
                'biometric_id': row[1],
                'tap_time': row[2].isoformat() if row[2] else None,
                'status': row[3],
                'remarks': row[4],
                'biometric_uid': row[5],
                'person_name': person_name,
                'college': collegename or 'N/A',
                'position': position or 'N/A',
                'employment_status': employmentstatus or 'N/A'
            })

        return {'success': True, 'biometric_logs': logs}

    except Exception as e:
        print(f"Error getting biometric logs: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            db_pool.return_connection(conn)

@app.route('/api/hr/campus-attendance')
@require_auth([20003])
def api_campus_attendance():
    """Read campus attendance records from campus_attendance table."""
    conn = None
    cursor = None
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        semester_id = request.args.get('semester_id')
        sem_start = sem_end = None
        if semester_id:
            cursor.execute("SELECT semesterstart, semesterend FROM acadcalendar WHERE acadcalendar_id = %s", (int(semester_id),))
            row = cursor.fetchone()
            if row:
                sem_start, sem_end = row[0], row[1]

        date_filter = "AND ca.attendance_date BETWEEN %s AND %s" if (sem_start and sem_end) else ""
        params = [sem_start, sem_end] if (sem_start and sem_end) else []

        cursor.execute("""
            SELECT
                ca.campus_attendance_id,
                CONCAT(p.lastname, ', ', p.firstname,
                    CASE WHEN p.honorifics IS NOT NULL AND p.honorifics <> ''
                         THEN ', ' || p.honorifics ELSE '' END) AS person_name,
                ca.attendance_date,
                ca.session,
                ca.time_in,
                ca.time_out,
                ca.status,
                c.collegename,
                pr.position,
                pr.employmentstatus
            FROM campus_attendance ca
            INNER JOIN personnel p ON ca.personnel_id = p.personnel_id
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE 1=1
            """ + date_filter + """
            ORDER BY ca.attendance_date DESC, person_name, ca.session
        """, params)

        records = []
        for row in cursor.fetchall():
            records.append({
                'id':                row[0],
                'name':              row[1],
                'date':              row[2].isoformat() if row[2] else None,
                'session':           row[3],
                'time_in':           row[4].isoformat() if row[4] else None,
                'time_out':          row[5].isoformat() if row[5] else None,
                'status':            row[6],
                'college':           row[7] or 'N/A',
                'position':          row[8] or 'N/A',
                'employment_status': row[9] or 'N/A',
            })

        return {'success': True, 'records': records}

    except Exception as e:
        print(f"Error fetching campus attendance: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            db_pool.return_connection(conn)


@app.route('/api/hr/campus-attendance/sync', methods=['POST'])
@require_auth([20003])
def api_sync_campus_attendance():
    """
    Compute morning/afternoon sessions from biometriclogs and upsert into
    campus_attendance. Generates Absent rows for biometric-registered personnel
    on any date where at least one tap was recorded by anyone.
    """
    conn = None
    cursor = None
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            WITH
            -- All dates from earliest biometric log to today (fills gaps with Absent)
            active_dates AS (
                SELECT d::date AS attendance_date
                FROM generate_series(
                    '2026-01-05'::date,
                    (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Manila')::date,
                    INTERVAL '1 day'
                ) d
            ),
            -- All personnel (regardless of teaching load or biometric device)
            all_faculty AS (
                SELECT personnel_id
                FROM personnel
            ),
            -- Every (date, person, session) combination we need to evaluate
            all_combinations AS (
                SELECT
                    d.attendance_date,
                    p.personnel_id,
                    s.session,
                    s.window_start,
                    s.window_end,
                    s.late_threshold
                FROM active_dates d
                CROSS JOIN all_faculty p
                CROSS JOIN (VALUES
                    ('Morning',   420,  764, 495),
                    ('Afternoon', 765, 1065, 825)
                ) AS s(session, window_start, window_end, late_threshold)
            ),
            -- Raw taps with local time in minutes
            taps AS (
                SELECT
                    b.personnel_id,
                    DATE(bl.taptime AT TIME ZONE 'Asia/Manila')                    AS tap_date,
                    bl.taptime AT TIME ZONE 'Asia/Manila'                          AS local_dt,
                    EXTRACT(HOUR  FROM bl.taptime AT TIME ZONE 'Asia/Manila') * 60
                  + EXTRACT(MINUTE FROM bl.taptime AT TIME ZONE 'Asia/Manila')     AS tap_mins
                FROM biometriclogs bl
                INNER JOIN biometric b ON bl.biometric_id = b.biometric_id
                WHERE b.personnel_id IS NOT NULL
            ),
            -- Group first/last tap per (person, date, session)
            session_agg AS (
                SELECT
                    ac.personnel_id,
                    ac.attendance_date,
                    ac.session,
                    ac.late_threshold,
                    MIN(t.local_dt) AS time_in,
                    MAX(t.local_dt) AS time_out
                FROM all_combinations ac
                LEFT JOIN taps t
                    ON  t.personnel_id = ac.personnel_id
                    AND t.tap_date     = ac.attendance_date
                    AND t.tap_mins BETWEEN ac.window_start AND ac.window_end
                GROUP BY ac.personnel_id, ac.attendance_date, ac.session, ac.late_threshold
            )
            INSERT INTO campus_attendance
                (personnel_id, attendance_date, session, time_in, time_out, status)
            SELECT
                personnel_id,
                attendance_date,
                session,
                time_in,
                time_out,
                CASE
                    WHEN time_in IS NULL THEN 'Absent'
                    WHEN (EXTRACT(HOUR  FROM time_in) * 60
                        + EXTRACT(MINUTE FROM time_in)) <= late_threshold THEN 'Present'
                    ELSE 'Late'
                END AS status
            FROM session_agg
            ON CONFLICT (personnel_id, attendance_date, session)
            DO UPDATE SET
                time_in  = EXCLUDED.time_in,
                time_out = EXCLUDED.time_out,
                status   = EXCLUDED.status
        """)

        synced = cursor.rowcount
        conn.commit()
        return {'success': True, 'message': f'Synced {synced} records', 'synced': synced}

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Error syncing campus attendance: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            db_pool.return_connection(conn)

@app.route('/api/hr/campus-attendance-analytics')
@require_auth([20003])
def api_hr_campus_attendance_analytics():
    """Aggregate campus attendance (morning + afternoon) per faculty for a given semester."""
    try:
        from datetime import timedelta as _td
        semester_id = request.args.get('semester_id')
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        # Resolve semester date range
        sem_start = sem_end = None
        date_filter = ""
        params = []
        if semester_id:
            cursor.execute("""
                SELECT semesterstart, semesterend FROM acadcalendar
                WHERE acadcalendar_id = %s
            """, (semester_id,))
            sem = cursor.fetchone()
            if sem and sem[0] and sem[1]:
                sem_start, sem_end = sem[0], sem[1]
                date_filter = "AND ca.attendance_date BETWEEN %s AND %s"
                params = [sem_start, sem_end]

        # ── Overall KPIs (all faculty combined) ──────────────────────────
        kpi_filter = "WHERE ca.attendance_date BETWEEN %s AND %s" if sem_start else ""
        kpi_params = [sem_start, sem_end] if sem_start else []
        cursor.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE ca.status = 'Present') AS present,
                COUNT(*) FILTER (WHERE ca.status = 'Late')    AS late,
                COUNT(*) FILTER (WHERE ca.status = 'Absent')  AS absent,
                COUNT(*) FILTER (WHERE ca.status = 'Excused') AS excused,
                COUNT(*) AS total
            FROM campus_attendance ca
            {kpi_filter}
        """, kpi_params)
        kpi_row = cursor.fetchone() or (0, 0, 0, 0, 0)
        kpi_present, kpi_late, kpi_absent, kpi_excused, kpi_total = (int(x) for x in kpi_row)
        avg_rate = round(((kpi_present + kpi_excused + kpi_late * 0.75) / kpi_total) * 100, 1) if kpi_total > 0 else 0.0

        # ── Weekly trends (all faculty combined) ─────────────────────────
        trends = []
        if sem_start and sem_end:
            cursor.execute("""
                WITH weeks AS (
                    SELECT generate_series(
                        DATE_TRUNC('week', %s::date),
                        DATE_TRUNC('week', %s::date),
                        INTERVAL '1 week'
                    )::date AS week_start
                ),
                agg AS (
                    SELECT
                        DATE_TRUNC('week', ca.attendance_date)::date AS week_start,
                        COUNT(*) FILTER (WHERE ca.status = 'Present') AS present,
                        COUNT(*) FILTER (WHERE ca.status = 'Late')    AS late,
                        COUNT(*) FILTER (WHERE ca.status = 'Absent')  AS absent,
                        COUNT(*) AS total
                    FROM campus_attendance ca
                    WHERE ca.attendance_date BETWEEN %s AND %s
                    GROUP BY 1
                )
                SELECT w.week_start,
                       COALESCE(a.present,0), COALESCE(a.late,0),
                       COALESCE(a.absent,0),  COALESCE(a.total,0)
                FROM weeks w
                LEFT JOIN agg a ON a.week_start = w.week_start
                ORDER BY w.week_start
            """, (sem_start, sem_end, sem_start, sem_end))
        else:
            cursor.execute("""
                SELECT
                    DATE_TRUNC('week', ca.attendance_date)::date AS week_start,
                    COUNT(*) FILTER (WHERE ca.status = 'Present') AS present,
                    COUNT(*) FILTER (WHERE ca.status = 'Late')    AS late,
                    COUNT(*) FILTER (WHERE ca.status = 'Absent')  AS absent,
                    COUNT(*) AS total
                FROM campus_attendance ca
                GROUP BY week_start
                ORDER BY week_start
            """)
        for r in cursor.fetchall():
            wk_start, wp, wl, wa, wt = r
            rate = round(((int(wp) + int(wl) * 0.75) / int(wt)) * 100, 1) if int(wt) > 0 else 0.0
            trends.append({
                'label':         wk_start.strftime('%b %d') if wk_start else '',
                'avg_rate':      rate,
                'total_present': int(wp),
                'total_late':    int(wl),
                'total_absent':  int(wa),
            })

        # ── Per-faculty breakdown ─────────────────────────────────────────
        cursor.execute(f"""
            SELECT
                p.personnel_id,
                CONCAT(p.lastname, ', ', p.firstname,
                       CASE WHEN p.honorifics IS NOT NULL AND p.honorifics <> ''
                            THEN CONCAT(', ', p.honorifics) ELSE '' END) AS faculty_name,
                c.collegename AS college,
                pr.position,
                pr.employmentstatus AS employment_status,
                COALESCE(SUM(CASE WHEN ca.session = 'Morning'   AND ca.status = 'Present' THEN 1 ELSE 0 END), 0) AS morning_present,
                COALESCE(SUM(CASE WHEN ca.session = 'Morning'   AND ca.status = 'Late'    THEN 1 ELSE 0 END), 0) AS morning_late,
                COALESCE(SUM(CASE WHEN ca.session = 'Morning'   AND ca.status = 'Absent'  THEN 1 ELSE 0 END), 0) AS morning_absent,
                COALESCE(SUM(CASE WHEN ca.session = 'Afternoon' AND ca.status = 'Present' THEN 1 ELSE 0 END), 0) AS afternoon_present,
                COALESCE(SUM(CASE WHEN ca.session = 'Afternoon' AND ca.status = 'Late'    THEN 1 ELSE 0 END), 0) AS afternoon_late,
                COALESCE(SUM(CASE WHEN ca.session = 'Afternoon' AND ca.status = 'Absent'  THEN 1 ELSE 0 END), 0) AS afternoon_absent
            FROM personnel p
            LEFT JOIN college c  ON p.college_id    = c.college_id
            LEFT JOIN profile pr ON p.personnel_id  = pr.personnel_id
            LEFT JOIN campus_attendance ca
                ON ca.personnel_id = p.personnel_id {date_filter}
            GROUP BY p.personnel_id, faculty_name, c.collegename, pr.position, pr.employmentstatus
            ORDER BY p.lastname, p.firstname
        """, params)

        rows = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)

        campus_breakdown = []
        for row in rows:
            (personnel_id, faculty_name, college, position, employment_status,
             mp, ml, ma, ap, al, aa) = row
            campus_breakdown.append({
                'personnel_id'     : personnel_id,
                'faculty_name'     : faculty_name,
                'college'          : college or '',
                'position'         : position or '',
                'employment_status': employment_status or '',
                'morning_present'  : int(mp),
                'morning_late'     : int(ml),
                'morning_absent'   : int(ma),
                'afternoon_present': int(ap),
                'afternoon_late'   : int(al),
                'afternoon_absent' : int(aa),
            })

        return jsonify({
            'success': True,
            'kpis': {
                'avg_rate':      avg_rate,
                'total_present': kpi_present,
                'total_late':    kpi_late,
                'total_absent':  kpi_absent,
                'total_excused': kpi_excused,
            },
            'distribution': {
                'present': kpi_present,
                'late':    kpi_late,
                'absent':  kpi_absent,
                'excused': kpi_excused,
            },
            'trends':           trends,
            'campus_breakdown': campus_breakdown,
        })

    except Exception as e:
        print(f"Error fetching campus attendance analytics: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/hr/update-campus-attendance-time', methods=['POST'])
@require_auth([20003])
def api_update_campus_attendance_time():
    """HR manual edit of campus attendance time_in / time_out."""
    try:
        data = request.get_json()
        updates = data.get('updates', [])
        if not updates:
            return jsonify({'success': False, 'error': 'No updates provided'}), 400

        # Session windows and late thresholds (minutes from midnight), matching sync logic
        SESSION_WINDOWS = {
            'Morning':   (420, 764, 495),   # 7:00 AM – 12:44 PM, late after 8:15 AM
            'Afternoon': (765, 1065, 825),  # 12:45 PM – 5:45 PM, late after 1:45 PM
        }

        conn = db_pool.get_connection()
        cursor = conn.cursor()
        updated_count = 0

        for upd in updates:
            campus_attendance_id = upd.get('campus_attendance_id')
            time_in_str  = upd.get('time_in')   # "HH:MM" or "" or absent
            time_out_str = upd.get('time_out')  # "HH:MM" or ""

            # Fetch existing record to get date + session
            cursor.execute("""
                SELECT attendance_date, session, time_in, time_out
                FROM campus_attendance
                WHERE campus_attendance_id = %s
            """, (campus_attendance_id,))
            rec = cursor.fetchone()
            if not rec:
                continue

            att_date, session, existing_timein, existing_timeout = rec
            win_start, win_end, late_threshold = SESSION_WINDOWS.get(session, (420, 764, 495))
            updates_applied = False
            new_status = None

            if 'time_in' in upd:
                if time_in_str == '':
                    # Clear → Absent
                    cursor.execute("""
                        UPDATE campus_attendance
                        SET time_in = NULL, time_out = NULL
                        WHERE campus_attendance_id = %s
                    """, (campus_attendance_id,))
                    new_status = 'Absent'
                    updates_applied = True
                else:
                    h, m = map(int, time_in_str.split(':'))
                    tap_mins = h * 60 + m
                    # Reject times outside the session window
                    if not (win_start <= tap_mins <= win_end):
                        window_label = '7:00 AM – 12:44 PM' if session == 'Morning' else '12:45 PM – 5:45 PM'
                        conn.rollback()
                        cursor.close()
                        db_pool.return_connection(conn)
                        return jsonify({'success': False, 'error': f"Time {time_in_str} is outside the valid {session} window ({window_label})."})
                    new_timein_ts = f"{att_date} {time_in_str}:00"
                    cursor.execute("""
                        UPDATE campus_attendance
                        SET time_in = (%s::timestamp AT TIME ZONE 'Asia/Manila')
                        WHERE campus_attendance_id = %s
                    """, (new_timein_ts, campus_attendance_id))
                    new_status = 'Present' if tap_mins <= late_threshold else 'Late'
                    updates_applied = True

            if 'time_out' in upd:
                if time_out_str == '':
                    cursor.execute("""
                        UPDATE campus_attendance
                        SET time_out = NULL
                        WHERE campus_attendance_id = %s
                    """, (campus_attendance_id,))
                else:
                    new_timeout_ts = f"{att_date} {time_out_str}:00"
                    cursor.execute("""
                        UPDATE campus_attendance
                        SET time_out = (%s::timestamp AT TIME ZONE 'Asia/Manila')
                        WHERE campus_attendance_id = %s
                    """, (new_timeout_ts, campus_attendance_id))
                updates_applied = True

            if updates_applied:
                if new_status is None:
                    # Only time_out changed — re-derive from existing time_in
                    if existing_timein:
                        tz = pytz.timezone('Asia/Manila')
                        if existing_timein.tzinfo is None:
                            local_timein = tz.localize(existing_timein)
                        else:
                            local_timein = existing_timein.astimezone(tz)
                        tap_mins = local_timein.hour * 60 + local_timein.minute
                        new_status = 'Present' if tap_mins <= late_threshold else 'Late'
                    else:
                        new_status = 'Absent'

                cursor.execute("""
                    UPDATE campus_attendance SET status = %s
                    WHERE campus_attendance_id = %s
                """, (new_status, campus_attendance_id))
                updated_count += 1

        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        return jsonify({'success': True, 'updated_count': updated_count})

    except Exception as e:
        print(f"Error updating campus attendance time: {e}")
        import traceback; traceback.print_exc()
        try:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        except Exception:
            pass
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== END BIOMETRIC API ENDPOINTS ====================

notification_queues = {}
notification_lock = threading.Lock()

def broadcast_notification(personnel_id, notification_data):
    """Broadcast notification to specific personnel, HR, and VP/Pres, and persist to DB."""
    notif_type = notification_data.get('notification_type', 'rfid')
    is_scanner_event = notif_type in ('rfid', 'biometric')

    # Persist to DB first so each audience row gets its own notif_id
    faculty_notif_id = None
    if personnel_id and personnel_id > 0:
        faculty_notif_id = save_notification_to_db('faculty', personnel_id, notification_data)
    hr_notif_id = save_notification_to_db('hr', None, notification_data)
    # VP/Pres do not receive biometric or RFID scanner notifications
    vp_notif_id = None if is_scanner_event else save_notification_to_db('vp', None, notification_data)

    with notification_lock:
        if personnel_id and personnel_id > 0 and personnel_id in notification_queues:
            fac_data = dict(notification_data, notif_id=faculty_notif_id)
            for q in notification_queues[personnel_id]:
                try:
                    q.put(fac_data)
                except Exception as e:
                    print(f"Error putting notification in queue: {e}")

        hr_key = 'hr_all_notifications'
        if hr_key in notification_queues:
            hr_data = dict(notification_data, notif_id=hr_notif_id)
            for q in notification_queues[hr_key]:
                try:
                    q.put(hr_data)
                    print(f"✓ Sent notification to HR: {notification_data.get('action', 'unknown')}")
                except Exception as e:
                    print(f"Error putting notification in HR queue: {e}")

        if not is_scanner_event:
            vp_key = 'vp_all_notifications'
            if vp_key in notification_queues:
                vp_data = dict(notification_data, notif_id=vp_notif_id)
                for q in notification_queues[vp_key]:
                    try:
                        q.put(vp_data)
                        print(f"✓ Sent notification to VP: {notification_data.get('action', 'unknown')}")
                    except Exception as e:
                        print(f"Error putting notification in VP queue: {e}")

def handle_rfid_notification(notification_data):
    """Handle RFID notifications from the reader"""
    personnel_id = notification_data.get('personnel_id')
    if personnel_id is not None:
        broadcast_notification(personnel_id, notification_data)

rfid_reader.add_notification_callback(handle_rfid_notification)

def handle_biometric_notification(notification_data):
    """Handle biometric notifications from the reader"""
    notification_data['notification_type'] = 'biometric'
    personnel_id = notification_data.get('personnel_id')  # None for unknown fingerprints
    broadcast_notification(personnel_id, notification_data)

biometric_reader.add_notification_callback(handle_biometric_notification)

def _push_to_queue(queue_key, data):
    """Push a notification to a single named queue without broadcasting to others."""
    if isinstance(queue_key, int):
        notif_id = save_notification_to_db('faculty', queue_key, data)
    elif queue_key == 'hr_all_notifications':
        notif_id = save_notification_to_db('hr', None, data)
    elif queue_key == 'vp_all_notifications':
        notif_id = save_notification_to_db('vp', None, data)
    else:
        notif_id = None
    with notification_lock:
        if queue_key in notification_queues:
            payload = dict(data, notif_id=notif_id)
            for q in notification_queues[queue_key]:
                try:
                    q.put(payload)
                except Exception as e:
                    print(f"Error pushing to queue {queue_key}: {e}")

def trigger_promotion_notification(faculty_id, faculty_name, requested_rank):
    """Notify HR of a new promotion application (HR queue only)."""
    philippines_tz = pytz.timezone('Asia/Manila')
    notification_data = {
        'notification_type': 'promotion',
        'action': 'new_application',
        'personnel_id': faculty_id,
        'person_name': faculty_name,
        'requested_rank': requested_rank,
        'message': f"{faculty_name} submitted a promotion application for {requested_rank}.",
        'tap_time': datetime.now(philippines_tz).strftime('%A, %B %d, %Y %I:%M %p'),
    }
    _push_to_queue('hr_all_notifications', notification_data)
    print(f"📣 Promotion notification sent to HR for {faculty_name}")

def trigger_promotion_forwarded_vpaa(faculty_name, requested_rank):
    """Notify VP/Pres when HR forwards a promotion to VPAA."""
    philippines_tz = pytz.timezone('Asia/Manila')
    notification_data = {
        'notification_type': 'promotion',
        'action': 'forwarded_vpaa',
        'person_name': faculty_name,
        'requested_rank': requested_rank,
        'message': f"{faculty_name}'s promotion application has been forwarded to VPA for review.",
        'tap_time': datetime.now(philippines_tz).strftime('%A, %B %d, %Y %I:%M %p'),
    }
    _push_to_queue('vp_all_notifications', notification_data)
    print(f"📣 Promotion forwarded-to-VPAA notification sent for {faculty_name}")

def trigger_promotion_forwarded_president(faculty_name, requested_rank):
    """Notify VP/Pres when VPAA forwards a promotion to President."""
    philippines_tz = pytz.timezone('Asia/Manila')
    notification_data = {
        'notification_type': 'promotion',
        'action': 'forwarded_president',
        'person_name': faculty_name,
        'requested_rank': requested_rank,
        'message': f"{faculty_name}'s promotion application has been forwarded to the President for approval.",
        'tap_time': datetime.now(philippines_tz).strftime('%A, %B %d, %Y %I:%M %p'),
    }
    _push_to_queue('vp_all_notifications', notification_data)
    print(f"📣 Promotion forwarded-to-President notification sent for {faculty_name}")

@app.route('/api/faculty/current-personnel')
@require_auth([20001, 20002, 20003, 20004])
def api_current_personnel():
    """Get current user's personnel ID"""
    try:
        user_id = session['user_id']
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        if not personnel_id:
            return {'success': False, 'error': 'Personnel record not found'}
        
        return {
            'success': True,
            'personnel_id': personnel_id,
            'position': personnel_info.get('position', '')
        }
    except Exception as e:
        print(f"Error getting current personnel: {e}")
        return {'success': False, 'error': str(e)}

@app.route('/api/rfid/notifications/<int:personnel_id>')
@require_auth([20001, 20002])
def api_rfid_notifications_stream(personnel_id):
    """Server-Sent Events endpoint for RFID notifications"""
    
    user_id = session['user_id']
    personnel_info = get_personnel_info(user_id)
    if personnel_info.get('personnel_id') != personnel_id:
        return {'success': False, 'error': 'Unauthorized'}, 403
    
    def event_stream():
        q = queue.Queue()
        
        with notification_lock:
            if personnel_id not in notification_queues:
                notification_queues[personnel_id] = []
            notification_queues[personnel_id].append(q)
        
        print(f"SSE connection established for personnel {personnel_id}")
        
        try:
            yield f"data: {json.dumps({'connected': True})}\n\n"
            
            while True:
                try:
                    notification = q.get(timeout=30)
                    
                    if notification and notification.get('personnel_id') and notification.get('tap_time'):
                        print(f"Sending notification to personnel {personnel_id}: {notification}")
                        yield f"data: {json.dumps(notification)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            print(f"SSE connection closed for personnel {personnel_id}")
        finally:
            with notification_lock:
                if personnel_id in notification_queues:
                    try:
                        notification_queues[personnel_id].remove(q)
                        if not notification_queues[personnel_id]:
                            del notification_queues[personnel_id]
                        print(f"Cleaned up SSE queue for personnel {personnel_id}")
                    except Exception as e:
                        print(f"Error cleaning up SSE queue: {e}")
    
    return Response(
        event_stream(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

@app.route('/api/hr/rfid-notifications')
@require_auth([20003])
def api_hr_rfid_notifications_stream():
    """Server-Sent Events endpoint for HR to receive ALL RFID notifications"""
    
    def event_stream():
        q = queue.Queue()
        
        with notification_lock:
            hr_key = 'hr_all_notifications'
            if hr_key not in notification_queues:
                notification_queues[hr_key] = []
            notification_queues[hr_key].append(q)
        
        print(f"HR SSE connection established - will receive all RFID notifications")
        
        try:
            yield f"data: {json.dumps({'connected': True})}\n\n"
            
            while True:
                try:
                    notification = q.get(timeout=30)
                    
                    if notification:
                        print(f"HR Sending notification: {notification}")
                        yield f"data: {json.dumps(notification)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            print(f"HR SSE connection closed")
        finally:
            with notification_lock:
                if hr_key in notification_queues:
                    try:
                        notification_queues[hr_key].remove(q)
                        if not notification_queues[hr_key]:
                            del notification_queues[hr_key]
                        print(f"Cleaned up HR SSE queue")
                    except Exception as e:
                        print(f"Error cleaning up HR SSE queue: {e}")
    
    return Response(
        event_stream(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

@app.route('/api/vp/notifications')
@require_auth([20004])
def api_vp_notifications_stream():
    """Server-Sent Events endpoint for VP/Pres to receive ALL notifications"""

    def event_stream():
        q = queue.Queue()
        vp_key = 'vp_all_notifications'

        with notification_lock:
            if vp_key not in notification_queues:
                notification_queues[vp_key] = []
            notification_queues[vp_key].append(q)

        print(f"VP SSE connection established - will receive all notifications")

        try:
            yield f"data: {json.dumps({'connected': True})}\n\n"

            while True:
                try:
                    notification = q.get(timeout=30)
                    if notification:
                        print(f"VP Sending notification: {notification}")
                        yield f"data: {json.dumps(notification)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            print(f"VP SSE connection closed")
        finally:
            with notification_lock:
                if vp_key in notification_queues:
                    try:
                        notification_queues[vp_key].remove(q)
                        if not notification_queues[vp_key]:
                            del notification_queues[vp_key]
                        print(f"Cleaned up VP SSE queue")
                    except Exception as e:
                        print(f"Error cleaning up VP SSE queue: {e}")

    return Response(
        event_stream(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

@app.route('/api/notifications/history')
@require_auth([20001, 20002, 20003, 20004])
def api_notifications_history():
    """Return up to 100 persisted notifications for the current user."""
    try:
        user_id = session['user_id']
        role_id = session.get('user_role')

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        if role_id in [20001, 20002]:
            personnel_info = get_personnel_info(user_id)
            personnel_id = personnel_info.get('personnel_id')
            cursor.execute("""
                SELECT notif_id, target_audience, target_personnel_id, notification_type,
                       person_name, tapped_personnel_id, rfid_uid, biometric_uid, biometric_id,
                       action, status, message, subject_code, subject_name,
                       class_section, classroom, tap_time, is_read, created_at
                FROM notifications
                WHERE target_audience = 'faculty' AND target_personnel_id = %s
                ORDER BY created_at DESC
                LIMIT 100
            """, (personnel_id,))
        elif role_id == 20003:
            cursor.execute("""
                SELECT notif_id, target_audience, target_personnel_id, notification_type,
                       person_name, tapped_personnel_id, rfid_uid, biometric_uid, biometric_id,
                       action, status, message, subject_code, subject_name,
                       class_section, classroom, tap_time, is_read, created_at
                FROM notifications
                WHERE target_audience = 'hr'
                ORDER BY created_at DESC
                LIMIT 100
            """)
        elif role_id == 20004:
            cursor.execute("""
                SELECT notif_id, target_audience, target_personnel_id, notification_type,
                       person_name, tapped_personnel_id, rfid_uid, biometric_uid, biometric_id,
                       action, status, message, subject_code, subject_name,
                       class_section, classroom, tap_time, is_read, created_at
                FROM notifications
                WHERE target_audience = 'vp'
                  AND notification_type NOT IN ('rfid', 'biometric')
                ORDER BY created_at DESC
                LIMIT 100
            """)
        else:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Unknown role'}), 403

        rows = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)

        columns = [
            'notif_id', 'target_audience', 'target_personnel_id', 'notification_type',
            'person_name', 'personnel_id', 'rfid_uid', 'biometric_uid', 'biometric_id',
            'action', 'status', 'message', 'subject_code', 'subject_name',
            'class_section', 'classroom', 'tap_time', 'is_read', 'created_at'
        ]
        notifications = []
        for row in rows:
            n = dict(zip(columns, row))
            if n['created_at']:
                n['created_at'] = n['created_at'].isoformat()
            # Remap spare columns back to license-specific fields for history display
            if n.get('notification_type') == 'license':
                n['license_type'] = n.get('subject_code')
                n['license_number'] = n.get('subject_name')
                n['expiration_date'] = n.get('class_section')
                days_str = n.get('classroom')
                n['days_until_expiry'] = int(days_str) if days_str and days_str.lstrip('-').isdigit() else None
            notifications.append(n)

        return jsonify({'success': True, 'notifications': notifications})
    except Exception as e:
        print(f"Error fetching notification history: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/notifications/mark-read', methods=['POST'])
@require_auth([20001, 20002, 20003, 20004])
def api_notifications_mark_read():
    """Mark all notifications as read for the current user."""
    try:
        user_id = session['user_id']
        role_id = session.get('user_role')

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        if role_id in [20001, 20002]:
            personnel_info = get_personnel_info(user_id)
            personnel_id = personnel_info.get('personnel_id')
            cursor.execute("""
                UPDATE notifications SET is_read = TRUE
                WHERE target_audience = 'faculty' AND target_personnel_id = %s
            """, (personnel_id,))
        elif role_id == 20003:
            cursor.execute("UPDATE notifications SET is_read = TRUE WHERE target_audience = 'hr'")
        elif role_id == 20004:
            cursor.execute("UPDATE notifications SET is_read = TRUE WHERE target_audience = 'vp'")

        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error marking notifications read: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/notifications/clear', methods=['POST'])
@require_auth([20001, 20002, 20003, 20004])
def api_notifications_clear():
    """Delete all notifications for the current user."""
    try:
        user_id = session['user_id']
        role_id = session.get('user_role')

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        if role_id in [20001, 20002]:
            personnel_info = get_personnel_info(user_id)
            personnel_id = personnel_info.get('personnel_id')
            cursor.execute("""
                DELETE FROM notifications
                WHERE target_audience = 'faculty' AND target_personnel_id = %s
            """, (personnel_id,))
        elif role_id == 20003:
            cursor.execute("DELETE FROM notifications WHERE target_audience = 'hr'")
        elif role_id == 20004:
            cursor.execute("DELETE FROM notifications WHERE target_audience = 'vp'")

        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error clearing notifications: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/faculty/semesters')
@require_auth([20001, 20002, 20003])
def api_faculty_semesters():
    """API endpoint to get available semesters - CACHED"""
    cache_key = "all_semesters"
    cached = get_cached(cache_key, ttl=60)  # Short TTL: is_current depends on calendar date
    if cached:
        return cached
    
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                acadcalendar_id,
                semester,
                acadyear,
                semesterstart,
                semesterend,
                CASE WHEN CURRENT_DATE BETWEEN semesterstart AND semesterend THEN 1 ELSE 0 END as is_current,
                -- has_data: 1 if this semester has any evaluation records
                CASE WHEN EXISTS (
                    SELECT 1 FROM faculty_evaluations fe
                    WHERE fe.acadcalendar_id = acadcalendar.acadcalendar_id
                ) THEN 1 ELSE 0 END as has_data
            FROM acadcalendar
            ORDER BY acadyear DESC,
                     CASE
                         WHEN semester LIKE '%First%' THEN 1
                         WHEN semester LIKE '%Second%' THEN 2
                         ELSE 3
                     END
        """)
        
        semesters = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        current_semester_id = None  # calendar-based: today falls between start/end
        data_semester_id = None    # fallback: most recent semester with eval data
        raw_semesters = []

        for sem in semesters:
            acadcalendar_id, semester, acadyear, start_date, end_date, is_current, has_data = sem

            if 'Semester' not in semester:
                if 'First' in semester:
                    semester_display = 'First Semester'
                elif 'Second' in semester:
                    semester_display = 'Second Semester'
                elif 'Summer' in semester:
                    semester_display = 'Summer Semester'
                else:
                    semester_display = semester + ' Semester'
            else:
                semester_display = semester

            if 'AY' in acadyear:
                year_display = acadyear
            else:
                year_display = f'AY {acadyear}'

            if is_current and current_semester_id is None:
                current_semester_id = acadcalendar_id

            if has_data and data_semester_id is None:
                data_semester_id = acadcalendar_id

            raw_semesters.append({
                'id': acadcalendar_id,
                'text': f"{semester_display}, {year_display}",
                'start_date': start_date.isoformat(),
                'end_date': end_date.isoformat()
            })

        # Calendar-active semester takes priority; fall back to most recent with data
        effective_current_id = current_semester_id if current_semester_id else data_semester_id

        # Deduplicate by display text (DB may have duplicate semester rows)
        seen_texts = set()
        semester_options = []
        for item in raw_semesters:
            if item['text'] in seen_texts:
                continue
            seen_texts.add(item['text'])
            semester_options.append({
                'id': item['id'],
                'text': item['text'],
                'is_current': item['id'] == effective_current_id,
                'start_date': item['start_date'],
                'end_date': item['end_date']
            })
        
        result = {
            'success': True,
            'semesters': semester_options,
            'current_semester_id': current_semester_id
        }
        
        set_cached(cache_key, result)
        return result
        
    except Exception as e:
        print(f"Error fetching semesters: {e}")
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/attendance/<int:semester_id>')
@require_auth([20001, 20002, 20003])
def api_faculty_attendance_by_semester(semester_id):
    """Get faculty attendance data with proper date handling for absent records"""
    try:
        user_id = session['user_id']
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            WITH semester_info AS (
                SELECT acadcalendar_id, semester, acadyear, semesterstart, semesterend 
                FROM acadcalendar 
                WHERE acadcalendar_id = %s
            ),
            faculty_schedule AS (
                SELECT
                    sch.class_id,
                    sub.subjectcode,
                    sub.subjectname,
                    sub.units,
                    sch.classsection,
                    sch.classroom,
                    sch.classday_1,
                    sch.starttime_1,
                    sch.endtime_1,
                    sch.classday_2,
                    sch.starttime_2,
                    sch.endtime_2
                FROM schedule sch
                JOIN subjects sub ON sch.subject_id = sub.subject_id
                JOIN personnel p ON sch.personnel_id = p.personnel_id
                WHERE p.user_id = %s AND sch.acadcalendar_id = %s
            ),
            faculty_attendance AS (
                SELECT 
                    a.attendance_id,
                    a.class_id,
                    a.attendancestatus,
                    a.timein,
                    a.timeout,
                    sub.subjectcode,
                    sub.subjectname,
                    sch.classsection,
                    sch.classroom
                FROM attendance a
                JOIN schedule sch ON a.class_id = sch.class_id
                JOIN subjects sub ON sch.subject_id = sub.subject_id
                JOIN personnel p ON a.personnel_id = p.personnel_id
                WHERE p.user_id = %s AND sch.acadcalendar_id = %s
                ORDER BY a.timein DESC
            )
            SELECT 
                (SELECT row_to_json(semester_info) FROM semester_info),
                (SELECT json_agg(row_to_json(faculty_schedule)) FROM faculty_schedule),
                (SELECT json_agg(row_to_json(faculty_attendance)) FROM faculty_attendance)
        """, (semester_id, user_id, semester_id, user_id, semester_id))
        
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if not result or result[0] is None:
            return {'success': False, 'error': 'Academic calendar not found'}
        
        semester_info_json, scheduled_classes_json, attendance_records_json = result
        
        scheduled_classes = scheduled_classes_json or []
        attendance_records = attendance_records_json or []
        
        subject_info = {}
        unique_sections = set()
        total_units = 0
        
        for scheduled_class in scheduled_classes:
            class_id = scheduled_class['class_id']
            subject_code = scheduled_class['subjectcode']
            subject_name = scheduled_class['subjectname']
            units = scheduled_class['units']
            class_section = scheduled_class['classsection']
            classroom = scheduled_class['classroom']
            
            subject_info[class_id] = {
                'class_name': f"{subject_code} - {subject_name}",
                'subject_code': subject_code,
                'class_section': class_section,
                'classroom': classroom
            }
            
            section_key = f"{subject_code}_{class_section}"
            if section_key not in unique_sections:
                unique_sections.add(section_key)
                total_units += units or 3
        
        attendance_logs = []
        class_attendance = []
        status_counts = {'present': 0, 'late': 0, 'absent': 0, 'excused': 0}
        
        for record in attendance_records:
            class_id = record['class_id']
            status = record['attendancestatus']
            timein = record['timein']
            timeout = record['timeout']

            info = subject_info.get(class_id, {})
            class_name = info.get('class_name', f"Class {class_id}")
            class_section = info.get('class_section') or 'N/A'
            classroom = info.get('classroom') or 'N/A'
            
            if timein:
                date_str = timein[:10] 
                time_part = timein[11:19]
                is_absent_record = (status.lower() == 'absent' and time_part == '00:00:00')
                
                if is_absent_record:
                    time_in_str = '—'
                else:
                    time_in_str = timein[11:16]
            else:
                date_str = 'N/A'
                time_in_str = '—'
            
            if status.lower() == 'absent' or not timeout:
                time_out_str = '—'
            else:
                time_out_str = timeout[11:16]
            
            log_entry = {
                'date': date_str,
                'time_in': time_in_str,
                'time_out': time_out_str,
                'status': status.capitalize(),
                'class_name': class_name,
                'class_section': class_section,
                'classroom': classroom
            }
            attendance_logs.append(log_entry)
            
            class_entry = {
                'class_name': class_name,
                'class_section': class_section,
                'classroom': classroom,
                'date': date_str,
                'time_in': time_in_str,
                'status': status.capitalize()
            }
            class_attendance.append(class_entry)
            
            status_lower = status.lower()
            if status_lower == 'present':
                status_counts['present'] += 1
            elif status_lower == 'late':
                status_counts['late'] += 1
            elif status_lower == 'absent':
                status_counts['absent'] += 1
            elif status_lower == 'excused':
                status_counts['excused'] += 1
        
        total_recorded = len(attendance_logs)
        attendance_percent = round((status_counts['present'] + status_counts['late']) / total_recorded * 100, 1) if total_recorded > 0 else 0
        
        kpis = {
            'attendance_percent': f'{attendance_percent}%',
            'late_count': status_counts['late'],
            'absence_count': status_counts['absent'],
            'total_classes': total_recorded,
            'sections_count': len(unique_sections),
            'total_units': total_units
        }
        
        semester_info = {
            'id': semester_info_json['acadcalendar_id'],
            'name': semester_info_json['semester'],
            'year': semester_info_json['acadyear'],
            'display': f"{semester_info_json['semester']}, AY {semester_info_json['acadyear']}",
            'semesterstart': str(semester_info_json.get('semesterstart', '') or ''),
            'semesterend': str(semester_info_json.get('semesterend', '') or '')
        }

        return {
            'success': True,
            'attendance_logs': attendance_logs,
            'class_attendance': class_attendance,
            'schedules': scheduled_classes,
            'status_breakdown': status_counts,
            'kpis': kpis,
            'semester_info': semester_info
        }
        
    except Exception as e:
        print(f"Error fetching attendance data for semester {semester_id}: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/attendance-analytics')
@require_auth([20001, 20002])
def api_faculty_attendance_analytics():
    """Attendance analytics for the logged-in faculty: weekly trends for selected semester + class breakdown"""
    try:
        from datetime import timedelta as _timedelta
        user_id = session['user_id']
        semester_id = request.args.get('semester_id')

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        # Get personnel_id for this user
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        p_row = cursor.fetchone()
        if not p_row:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Personnel not found'}), 404
        personnel_id = p_row[0]

        # Resolve selected semester (fallback: current, then most recent)
        if not semester_id:
            cursor.execute("""
                SELECT acadcalendar_id FROM acadcalendar
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC LIMIT 1
            """)
            result = cursor.fetchone()
            if result:
                semester_id = result[0]
            else:
                cursor.execute("SELECT acadcalendar_id FROM acadcalendar ORDER BY semesterstart DESC LIMIT 1")
                result = cursor.fetchone()
                if result:
                    semester_id = result[0]

        # Get semester date range
        sem_start = sem_end = None
        if semester_id:
            cursor.execute("""
                SELECT semesterstart, semesterend FROM acadcalendar
                WHERE acadcalendar_id = %s
            """, (semester_id,))
            sem_row = cursor.fetchone()
            if sem_row:
                sem_start, sem_end = sem_row

        # Weekly trends filtered to selected semester, all weeks filled
        trends = []
        if semester_id and sem_start and sem_end:
            cursor.execute("""
                SELECT
                    DATE_TRUNC('week', a.timein AT TIME ZONE 'Asia/Manila')::date AS week_start,
                    COUNT(*) FILTER (WHERE a.attendancestatus = 'Present')  AS total_present,
                    COUNT(*) FILTER (WHERE a.attendancestatus = 'Late')     AS total_late,
                    COUNT(*) FILTER (WHERE a.attendancestatus = 'Absent')   AS total_absent,
                    COUNT(*) FILTER (WHERE a.attendancestatus = 'Excused')  AS total_excused,
                    COUNT(*) AS total
                FROM attendance a
                JOIN schedule sch ON a.class_id = sch.class_id
                WHERE a.personnel_id = %s
                  AND sch.acadcalendar_id = %s
                  AND a.timein IS NOT NULL
                GROUP BY DATE_TRUNC('week', a.timein AT TIME ZONE 'Asia/Manila')::date
                ORDER BY week_start ASC
            """, (personnel_id, semester_id))
            week_data = {}
            for row in cursor.fetchall():
                (week_start, tot_p, tot_l, tot_a, tot_e, total) = row
                tot_p = int(tot_p); tot_l = int(tot_l)
                tot_a = int(tot_a); tot_e = int(tot_e); total = int(total)
                avg_r = round(((tot_p + tot_e + tot_l * 0.75) / total) * 100, 2) if total > 0 else 0.0
                label = week_start.strftime('%b %d') if week_start else ''
                week_data[label] = {
                    'label': label,
                    'total_present': tot_p, 'total_late': tot_l,
                    'total_absent': tot_a, 'total_excused': tot_e,
                    'avg_rate': avg_r
                }
            # Fill every week in the semester range (zeros if no data)
            cur_w = sem_start - _timedelta(days=sem_start.weekday())
            while cur_w <= sem_end:
                label = cur_w.strftime('%b %d')
                trends.append(week_data.get(label, {
                    'label': label, 'total_present': 0, 'total_late': 0,
                    'total_absent': 0, 'total_excused': 0, 'avg_rate': 0.0
                }))
                cur_w += _timedelta(weeks=1)

        distribution = {'present': 0, 'late': 0, 'absent': 0, 'excused': 0}
        analytics_kpis = {
            'avg_rate': 0.0, 'total_present': 0, 'total_late': 0,
            'total_absent': 0, 'total_excused': 0
        }
        class_breakdown = []

        if semester_id:
            # KPIs + distribution
            cursor.execute("""
                SELECT
                    COALESCE(SUM(ar.presentcount), 0),
                    COALESCE(SUM(ar.latecount), 0),
                    COALESCE(SUM(ar.absentcount), 0),
                    COALESCE(SUM(ar.excusedcount), 0),
                    CASE
                        WHEN COUNT(ar.attendancereport_id) > 0
                        THEN ROUND(AVG(ar.attendancerate)::numeric, 2)
                        ELSE 0
                    END
                FROM attendancereport ar
                WHERE ar.acadcalendar_id = %s
                  AND ar.personnel_id = %s
            """, (semester_id, personnel_id))
            dist_row = cursor.fetchone()
            if dist_row:
                (tot_p, tot_l, tot_a, tot_e, avg_r) = dist_row
                distribution = {
                    'present': int(tot_p), 'late': int(tot_l),
                    'absent': int(tot_a), 'excused': int(tot_e)
                }
                analytics_kpis = {
                    'avg_rate': float(avg_r),
                    'total_present': int(tot_p), 'total_late': int(tot_l),
                    'total_absent': int(tot_a), 'total_excused': int(tot_e)
                }

            # Per-class breakdown for this faculty
            cursor.execute("""
                SELECT
                    sub.subjectcode, sub.subjectname,
                    sch.classsection,
                    ar.presentcount, ar.latecount, ar.absentcount,
                    ar.excusedcount, ar.totalclasses, ar.attendancerate
                FROM attendancereport ar
                JOIN schedule sch ON ar.class_id = sch.class_id
                JOIN subjects sub ON sch.subject_id = sub.subject_id
                WHERE ar.acadcalendar_id = %s
                  AND ar.personnel_id = %s
                ORDER BY ar.attendancerate ASC, sub.subjectcode
            """, (semester_id, personnel_id))
            for row in cursor.fetchall():
                (scode, sname, section, pres, late, absent, excused, total, rate) = row
                class_breakdown.append({
                    'subject_code': scode,
                    'subject_name': sname,
                    'section': section or '—',
                    'present': int(pres),
                    'late': int(late),
                    'absent': int(absent),
                    'excused': int(excused),
                    'total': int(total),
                    'rate': round(float(rate), 2)
                })

        cursor.close()
        db_pool.return_connection(conn)

        return jsonify({
            'success': True,
            'trends': trends,
            'distribution': distribution,
            'analytics_kpis': analytics_kpis,
            'class_breakdown': class_breakdown,
            'selected_semester_id': int(semester_id) if semester_id else None
        })

    except Exception as e:
        print(f"Error fetching faculty attendance analytics: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hr/excuse-absence-bulk', methods=['POST'])
@require_auth([20003])
def api_excuse_absence_bulk():
    """Bulk excuse all absences for a faculty on a specific date - ONLY UPDATE EXISTING RECORDS"""
    try:
        data = request.get_json()
        personnel_id = data.get('personnel_id')
        date_str = data.get('date')
        reason = data.get('reason', '').strip()
        
        if not personnel_id or not date_str:
            return {'success': False, 'error': 'Faculty and date are required'}
        
        if not reason:
            return {'success': False, 'error': 'Reason is required'}
        
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return {'success': False, 'error': 'Invalid date format. Use YYYY-MM-DD'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT firstname, lastname FROM personnel WHERE personnel_id = %s", (personnel_id,))
        faculty_info = cursor.fetchone()
        if not faculty_info:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Faculty not found'}
        
        faculty_name = f"{faculty_info[0]} {faculty_info[1]}"
        
        cursor.execute("""
            SELECT COUNT(*), 
                   COALESCE(string_agg(attendancestatus, ', '), 'None') as existing_statuses
            FROM attendance 
            WHERE personnel_id = %s 
            AND DATE(timein AT TIME ZONE 'Asia/Manila') = %s
        """, (personnel_id, target_date))
        
        before_result = cursor.fetchone()
        before_count = before_result[0] if before_result else 0
        before_statuses = before_result[1] if before_result else 'None'
        
        if before_count == 0:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': f'No attendance records found for {faculty_name} on {target_date}. Only existing records can be excused.'}
        
        cursor.execute("""
            SELECT attendance_id, class_id, attendancestatus
            FROM attendance 
            WHERE personnel_id = %s 
            AND DATE(timein AT TIME ZONE 'Asia/Manila') = %s
        """, (personnel_id, target_date))
        
        existing_records = cursor.fetchall()
        updated_count = 0
        
        for record in existing_records:
            attendance_id, class_id, current_status = record

            if current_status in ['Absent', 'Present', 'Late']:
                cursor.execute("""
                    UPDATE attendance
                    SET attendancestatus = 'Excused'
                    WHERE attendance_id = %s
                """, (attendance_id,))
                updated_count += 1

        # Also excuse both campus attendance sessions for this faculty on this date
        cursor.execute("""
            UPDATE campus_attendance
            SET status = 'Excused'
            WHERE personnel_id = %s
              AND attendance_date = %s
              AND status IN ('Absent', 'Present', 'Late')
        """, (personnel_id, target_date))

        conn.commit()

        try:
            cursor.execute("""
                SELECT DISTINCT sch.class_id, sch.acadcalendar_id 
                FROM attendance a
                JOIN schedule sch ON a.class_id = sch.class_id
                WHERE a.personnel_id = %s 
                AND DATE(a.timein AT TIME ZONE 'Asia/Manila') = %s
            """, (personnel_id, target_date))
            
            affected_classes = cursor.fetchall()
            for class_id, acadcal_id in affected_classes:
                update_attendance_report(personnel_id, class_id, acadcal_id, conn)
        except Exception as e:
            print(f"Warning: Could not update attendance reports: {e}")
        
        hr_user_id = session['user_id']
        hr_personnel_info = get_personnel_info(hr_user_id)
        hr_personnel_id = hr_personnel_info.get('personnel_id')
        
        before_value = f"Records: {before_count}, Statuses: {before_statuses}"
        after_value = f"Updated to Excused: {updated_count}"
        
        log_audit_action(
            hr_personnel_id,
            "Bulk absence excuse",
            f"HR excused attendance for {faculty_name} on {target_date}\nReason: {reason}",
            before_value=before_value,
            after_value=after_value
        )
        
        cursor.close()
        db_pool.return_connection(conn)
        
        if updated_count == 0:
            return {
                'success': False, 
                'error': f'No records to excuse for {faculty_name} on {target_date}. Only Absent, Present, or Late records can be excused.'
            }
        
        return {
            'success': True, 
            'message': f'Successfully excused {updated_count} classes for {faculty_name} on {target_date}',
            'updated': updated_count
        }
        
    except Exception as e:
        print(f"Error in bulk excuse: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
        return {'success': False, 'error': str(e)}

@app.route('/api/attendance-report/<int:personnel_id>/<int:class_id>')
@require_auth([20001, 20002, 20003, 20004])
def api_get_attendance_report(personnel_id, class_id):
    """Get attendance report for specific personnel and class"""
    try:
        semester_id = request.args.get('semester_id')
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        # Get current semester if not specified
        if not semester_id:
            cursor.execute("""
                SELECT acadcalendar_id FROM acadcalendar 
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC LIMIT 1
            """)
            result = cursor.fetchone()
            semester_id = result[0] if result else None
        
        if not semester_id:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'No active semester found'})
        
        # Get attendance report
        cursor.execute("""
            SELECT 
                ar.presentcount,
                ar.latecount,
                ar.excusedcount,
                ar.absentcount,
                ar.totalclasses,
                ar.attendancerate,
                ar.lastupdated,
                p.firstname,
                p.lastname,
                p.honorifics,
                sub.subjectcode,
                sub.subjectname,
                sch.classsection,
                ac.semester,
                ac.acadyear
            FROM attendancereport ar
            JOIN personnel p ON ar.personnel_id = p.personnel_id
            JOIN schedule sch ON ar.class_id = sch.class_id
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            JOIN acadcalendar ac ON ar.acadcalendar_id = ac.acadcalendar_id
            WHERE ar.personnel_id = %s 
            AND ar.class_id = %s 
            AND ar.acadcalendar_id = %s
        """, (personnel_id, class_id, semester_id))
        
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if not result:
            return jsonify({
                'success': False, 
                'error': 'No attendance report found for this class'
            })
        
        (present, late, excused, absent, total, rate, updated, 
         firstname, lastname, honorifics, subjectcode, subjectname, 
         section, semester, acadyear) = result
        
        faculty_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
        
        return jsonify({
            'success': True,
            'report': {
                'faculty_name': faculty_name,
                'subject_code': subjectcode,
                'subject_name': subjectname,
                'section': section,
                'semester': f"{semester}, {acadyear}",
                'present_count': present,
                'late_count': late,
                'excused_count': excused,
                'absent_count': absent,
                'total_classes': total,
                'attendance_rate': round(float(rate), 2),
                'last_updated': updated.strftime('%Y-%m-%d %H:%M:%S') if updated else 'N/A'
            }
        })
        
    except Exception as e:
        print(f"Error fetching attendance report: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/attendance-reports/personnel/<int:personnel_id>')
@require_auth([20001, 20002, 20003, 20004])
def api_get_personnel_attendance_reports(personnel_id):
    """Get all attendance reports for a personnel across all classes"""
    try:
        semester_id = request.args.get('semester_id')
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        if not semester_id:
            cursor.execute("""
                SELECT acadcalendar_id FROM acadcalendar 
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC LIMIT 1
            """)
            result = cursor.fetchone()
            semester_id = result[0] if result else None
        
        if not semester_id:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'No active semester found'})
        
        cursor.execute("""
            SELECT 
                ar.class_id,
                ar.presentcount,
                ar.latecount,
                ar.excusedcount,
                ar.absentcount,
                ar.totalclasses,
                ar.attendancerate,
                sub.subjectcode,
                sub.subjectname,
                sch.classsection,
                sch.classroom
            FROM attendancereport ar
            JOIN schedule sch ON ar.class_id = sch.class_id
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE ar.personnel_id = %s 
            AND ar.acadcalendar_id = %s
            ORDER BY sub.subjectcode
        """, (personnel_id, semester_id))
        
        results = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        reports = []
        for row in results:
            (class_id, present, late, excused, absent, total, rate,
             subjectcode, subjectname, section, classroom) = row
            
            reports.append({
                'class_id': class_id,
                'subject_code': subjectcode,
                'subject_name': subjectname,
                'section': section,
                'classroom': classroom,
                'present_count': present,
                'late_count': late,
                'excused_count': excused,
                'absent_count': absent,
                'total_classes': total,
                'attendance_rate': round(float(rate), 2)
            })
        
        return jsonify({
            'success': True,
            'reports': reports
        })
        
    except Exception as e:
        print(f"Error fetching personnel attendance reports: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hr/attendance-reports')
@require_auth([20003])
def api_get_all_attendance_reports():
    """Get all attendance reports for HR dashboard"""
    try:
        semester_id = request.args.get('semester_id')
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        if not semester_id:
            cursor.execute("""
                SELECT acadcalendar_id FROM acadcalendar 
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC LIMIT 1
            """)
            result = cursor.fetchone()
            semester_id = result[0] if result else None
        
        if not semester_id:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'No active semester found'})
        
        cursor.execute("""
            SELECT 
                ar.personnel_id,
                ar.class_id,
                ar.presentcount,
                ar.latecount,
                ar.excusedcount,
                ar.absentcount,
                ar.totalclasses,
                ar.attendancerate,
                p.firstname,
                p.lastname,
                p.honorifics,
                sub.subjectcode,
                sub.subjectname,
                sch.classsection,
                sch.classroom
            FROM attendancereport ar
            JOIN personnel p ON ar.personnel_id = p.personnel_id
            JOIN schedule sch ON ar.class_id = sch.class_id
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE ar.acadcalendar_id = %s
            AND p.role_id IN (20001, 20002)
            ORDER BY p.lastname, p.firstname, sub.subjectcode
        """, (semester_id,))
        
        results = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        reports = []
        for row in results:
            (personnel_id, class_id, present, late, excused, absent, total, rate,
             firstname, lastname, honorifics, subjectcode, subjectname, section, classroom) = row
            
            faculty_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            
            reports.append({
                'personnel_id': personnel_id,
                'class_id': class_id,
                'faculty_name': faculty_name,
                'subject_code': subjectcode,
                'subject_name': subjectname,
                'section': section,
                'classroom': classroom,
                'present_count': present,
                'late_count': late,
                'excused_count': excused,
                'absent_count': absent,
                'total_classes': total,
                'attendance_rate': round(float(rate), 2)
            })
        
        return jsonify({
            'success': True,
            'reports': reports
        })
        
    except Exception as e:
        print(f"Error fetching all attendance reports: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/faculty/campus-attendance')
@require_auth([20001, 20002])
def api_faculty_campus_attendance():
    """Return campus attendance (biometric) records for the logged-in faculty."""
    conn = None
    cursor = None
    try:
        user_id = session['user_id']
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        if not personnel_id:
            return jsonify({'success': False, 'error': 'Personnel record not found'}), 403

        semester_id = request.args.get('semester_id')

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        date_filter = ""
        params = [personnel_id]
        if semester_id:
            cursor.execute(
                "SELECT semesterstart, semesterend FROM acadcalendar WHERE acadcalendar_id = %s",
                (semester_id,)
            )
            sem = cursor.fetchone()
            if sem:
                date_filter = "AND ca.attendance_date BETWEEN %s AND %s"
                params += [sem[0], sem[1]]

        cursor.execute(f"""
            SELECT
                ca.campus_attendance_id,
                ca.attendance_date,
                ca.session,
                ca.time_in  AT TIME ZONE 'Asia/Manila' AS time_in_local,
                ca.time_out AT TIME ZONE 'Asia/Manila' AS time_out_local,
                ca.status,
                pr.employmentstatus
            FROM campus_attendance ca
            LEFT JOIN profile pr ON ca.personnel_id = pr.personnel_id
            WHERE ca.personnel_id = %s {date_filter}
            ORDER BY ca.attendance_date DESC, ca.session
        """, params)

        records = []
        for row in cursor.fetchall():
            def fmt_ts(ts):
                if not ts:
                    return None
                return ts.strftime('%H:%M') if hasattr(ts, 'strftime') else str(ts)[:5]
            records.append({
                'id':                row[0],
                'date':              row[1].isoformat() if row[1] else None,
                'session':           row[2],
                'time_in':           fmt_ts(row[3]),
                'time_out':          fmt_ts(row[4]),
                'status':            row[5],
                'employment_status': row[6] or '---',
            })

        return jsonify({'success': True, 'records': records})

    except Exception as e:
        print(f"Error fetching faculty campus attendance: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if conn: db_pool.return_connection(conn)


@app.route('/api/faculty/campus-attendance-analytics')
@require_auth([20001, 20002])
def api_faculty_campus_attendance_analytics():
    """Campus attendance analytics (biometric) for the logged-in faculty."""
    conn = None
    cursor = None
    try:
        from datetime import timedelta as _td
        user_id = session['user_id']
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        if not personnel_id:
            return jsonify({'success': False, 'error': 'Personnel record not found'}), 403

        semester_id = request.args.get('semester_id')
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        # Resolve semester date range
        sem_start = sem_end = None
        if semester_id:
            cursor.execute(
                "SELECT semesterstart, semesterend FROM acadcalendar WHERE acadcalendar_id = %s",
                (semester_id,)
            )
            sem = cursor.fetchone()
            if sem:
                sem_start, sem_end = sem[0], sem[1]

        date_filter = ""
        params_base = [personnel_id]
        if sem_start and sem_end:
            date_filter = "AND ca.attendance_date BETWEEN %s AND %s"
            params_base += [sem_start, sem_end]

        # KPIs
        cursor.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE ca.status = 'Present')  AS present,
                COUNT(*) FILTER (WHERE ca.status = 'Late')     AS late,
                COUNT(*) FILTER (WHERE ca.status = 'Absent')   AS absent,
                COUNT(*) FILTER (WHERE ca.status = 'Excused')  AS excused,
                COUNT(*) AS total
            FROM campus_attendance ca
            WHERE ca.personnel_id = %s {date_filter}
        """, params_base)
        row = cursor.fetchone()
        present, late, absent, excused, total = (row or (0, 0, 0, 0, 0))
        avg_rate = round(((present + excused + late * 0.75) / total) * 100, 1) if total > 0 else 0.0

        # Weekly trend — fill all weeks in range so graph is continuous
        if sem_start and sem_end:
            cursor.execute("""
                WITH weeks AS (
                    SELECT generate_series(
                        DATE_TRUNC('week', %s::date),
                        DATE_TRUNC('week', %s::date),
                        INTERVAL '1 week'
                    )::date AS week_start
                ),
                agg AS (
                    SELECT
                        DATE_TRUNC('week', ca.attendance_date)::date AS week_start,
                        COUNT(*) FILTER (WHERE ca.status = 'Present')  AS present,
                        COUNT(*) FILTER (WHERE ca.status = 'Late')     AS late,
                        COUNT(*) FILTER (WHERE ca.status = 'Absent')   AS absent,
                        COUNT(*) FILTER (WHERE ca.status = 'Excused')  AS excused,
                        COUNT(*) AS total
                    FROM campus_attendance ca
                    WHERE ca.personnel_id = %s
                      AND ca.attendance_date BETWEEN %s AND %s
                    GROUP BY 1
                )
                SELECT w.week_start,
                       COALESCE(a.present,0), COALESCE(a.late,0),
                       COALESCE(a.absent,0),  COALESCE(a.excused,0),
                       COALESCE(a.total,0)
                FROM weeks w
                LEFT JOIN agg a ON a.week_start = w.week_start
                ORDER BY w.week_start
            """, (sem_start, sem_end, personnel_id, sem_start, sem_end))
        else:
            cursor.execute(f"""
                SELECT
                    DATE_TRUNC('week', ca.attendance_date)::date AS week_start,
                    COUNT(*) FILTER (WHERE ca.status = 'Present')  AS present,
                    COUNT(*) FILTER (WHERE ca.status = 'Late')     AS late,
                    COUNT(*) FILTER (WHERE ca.status = 'Absent')   AS absent,
                    COUNT(*) FILTER (WHERE ca.status = 'Excused')  AS excused,
                    COUNT(*) AS total
                FROM campus_attendance ca
                WHERE ca.personnel_id = %s {date_filter}
                GROUP BY week_start
                ORDER BY week_start
            """, params_base)
        trends = []
        for r in cursor.fetchall():
            wk_start, wp, wl, wa, we, wt = r
            rate = round(((wp + we + wl * 0.75) / wt) * 100, 1) if wt > 0 else 0.0
            trends.append({
                'label': wk_start.strftime('%b %d') if wk_start else '',
                'avg_rate': rate,
                'total_present': wp,
                'total_late': wl,
                'total_absent': wa,
            })

        return jsonify({
            'success': True,
            'kpis': {
                'avg_rate':      avg_rate,
                'total_present': present,
                'total_late':    late,
                'total_absent':  absent,
                'total_excused': excused,
            },
            'distribution': {
                'present': present,
                'late':    late,
                'absent':  absent,
                'excused': excused,
            },
            'trends': trends,
        })

    except Exception as e:
        print(f"Error fetching faculty campus attendance analytics: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if conn: db_pool.return_connection(conn)


@app.route('/api/faculty/teaching-schedule/<int:semester_id>')
@require_auth([20001, 20002, 20003])
def api_faculty_teaching_schedule(semester_id):
    """Get faculty teaching schedule with 15-minute grid precision"""
    try:
        viewing_personnel_id = session.get('viewing_personnel_id')
        if viewing_personnel_id:
            personnel_id = viewing_personnel_id
        else:
            user_id = session['user_id']
            personnel_info = get_personnel_info(user_id)
            personnel_id = personnel_info.get('personnel_id')
            
            if not personnel_id:
                return {'success': False, 'error': 'Personnel record not found'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT 
                ac.acadcalendar_id,
                ac.semester,
                ac.acadyear,
                sch.classday_1,
                sch.starttime_1,
                sch.endtime_1,
                sch.classday_2,
                sch.starttime_2,
                sch.endtime_2,
                sub.subjectcode,
                sch.classroom,
                sch.classsection
            FROM acadcalendar ac
            LEFT JOIN schedule sch ON sch.acadcalendar_id = ac.acadcalendar_id AND sch.personnel_id = %s
            LEFT JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE ac.acadcalendar_id = %s
        """, (personnel_id, semester_id))
        
        results = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        if not results:
            return {'success': False, 'error': 'Academic calendar not found'}

        acadcalendar_id = results[0][0]
        semester_name = results[0][1]
        acad_year = results[0][2]
        
        def time_to_minutes(time_val):
            """Convert time to minutes since 7:00 AM"""
            if not time_val:
                return None
            
            if isinstance(time_val, str):
                time_str = time_val[:8]  
            elif hasattr(time_val, 'strftime'):
                time_str = time_val.strftime('%H:%M:%S')
            else:
                time_str = str(time_val)[:8]
            
            try:
                hours, minutes, _ = map(int, time_str.split(':'))
                return (hours - 7) * 60 + minutes
            except:
                return None
        
        def format_time_12hr(time_val):
            """Format time in 12-hour format"""
            if not time_val:
                return None
            
            if isinstance(time_val, str):
                time_str = time_val[:5]
            elif hasattr(time_val, 'strftime'):
                time_str = time_val.strftime('%H:%M')
            else:
                time_str = str(time_val)[:5]
            
            try:
                hours, minutes = map(int, time_str.split(':'))
                period = 'AM' if hours < 12 else 'PM'
                display_hour = hours if hours <= 12 else hours - 12
                display_hour = 12 if display_hour == 0 else display_hour
                return f"{display_hour}:{minutes:02d} {period}"
            except:
                return time_str
        
        schedule_classes = []
        
        for row in results:
            if not row[9]: 
                continue
            
            (_, _, _, classday_1, starttime_1, endtime_1, 
             classday_2, starttime_2, endtime_2,
             subject_code, classroom, class_section) = row
            
            if classday_1 and starttime_1 and endtime_1:
                start_minutes = time_to_minutes(starttime_1)
                end_minutes = time_to_minutes(endtime_1)
                
                if start_minutes is not None and end_minutes is not None:
                    schedule_classes.append({
                        'day': classday_1,
                        'subject_code': subject_code,
                        'classroom': classroom or 'TBA',
                        'section': class_section or 'N/A',
                        'start_minutes': start_minutes,
                        'end_minutes': end_minutes,
                        'start_time': format_time_12hr(starttime_1),
                        'end_time': format_time_12hr(endtime_1),
                        'duration_minutes': end_minutes - start_minutes
                    })
            
            if classday_2 and starttime_2 and endtime_2:
                start_minutes = time_to_minutes(starttime_2)
                end_minutes = time_to_minutes(endtime_2)
                
                if start_minutes is not None and end_minutes is not None:
                    schedule_classes.append({
                        'day': classday_2,
                        'subject_code': subject_code,
                        'classroom': classroom or 'TBA',
                        'section': class_section or 'N/A',
                        'start_minutes': start_minutes,
                        'end_minutes': end_minutes,
                        'start_time': format_time_12hr(starttime_2),
                        'end_time': format_time_12hr(endtime_2),
                        'duration_minutes': end_minutes - start_minutes
                    })
        
        semester_info = {
            'id': acadcalendar_id,
            'name': semester_name,
            'year': acad_year,
            'display': f"{semester_name}, {acad_year}"
        }
        
        return {
            'success': True,
            'schedule_classes': schedule_classes,
            'semester_info': semester_info
        }
        
    except Exception as e:
        print(f"Error fetching teaching schedule for semester {semester_id}: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/dashboard')
@require_auth([20001, 20002])
def api_faculty_dashboard():
    """OPTIMIZED: Get faculty dashboard data with single query"""
    try:
        user_id = session['user_id']
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            WITH current_semester AS (
                SELECT acadcalendar_id, semester, acadyear, semesterstart, semesterend 
                FROM acadcalendar 
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC
                LIMIT 1
            ),
            personnel_data AS (
                SELECT personnel_id FROM personnel WHERE user_id = %s
            ),
            schedule_data AS (
                SELECT 
                    sch.class_id,
                    sch.classday_1,
                    sch.starttime_1,
                    sch.endtime_1,
                    sch.classday_2,
                    sch.starttime_2,
                    sch.endtime_2,
                    sch.classroom,
                    sch.classsection,
                    sub.subjectcode,
                    sub.subjectname,
                    sub.units
                FROM schedule sch
                JOIN subjects sub ON sch.subject_id = sub.subject_id
                CROSS JOIN personnel_data pd
                CROSS JOIN current_semester cs
                WHERE sch.personnel_id = pd.personnel_id 
                AND sch.acadcalendar_id = cs.acadcalendar_id
            ),
            attendance_data AS (
                SELECT 
                    a.class_id,
                    a.attendancestatus,
                    a.timein
                FROM attendance a
                JOIN schedule sch ON a.class_id = sch.class_id
                CROSS JOIN personnel_data pd
                CROSS JOIN current_semester cs
                WHERE a.personnel_id = pd.personnel_id 
                AND sch.acadcalendar_id = cs.acadcalendar_id
            ),
            evaluation_data AS (
                -- Calculate Overall Weighted Evaluation Score (55/35/10)
                SELECT
                    COALESCE(
                        SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) +
                        SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) +
                        SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END),
                    0) AS overall_average
                FROM faculty_evaluations fe
                CROSS JOIN personnel_data pd
                CROSS JOIN current_semester cs
                WHERE fe.personnel_id = pd.personnel_id AND fe.acadcalendar_id = cs.acadcalendar_id
            )
            SELECT 
                (SELECT row_to_json(current_semester) FROM current_semester),
                (SELECT json_agg(row_to_json(schedule_data)) FROM schedule_data),
                (SELECT json_agg(row_to_json(attendance_data)) FROM attendance_data),
                (SELECT COALESCE(SUM(units), 0) FROM schedule_data),
                (SELECT overall_average FROM evaluation_data LIMIT 1) 
        """, (user_id,))
        
        result = cursor.fetchone()

        if not result or result[0] is None:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'No active academic calendar found'}

        semester_info_json, schedule_json, attendance_json, teaching_load, overall_eval_score = result

        # Fetch regularization data for the dashboard
        cursor.execute("""
            SELECT p.hiredate, pr.employmentstatus, pr.has_aligned_master,
                   COALESCE(pr.probationary_start_date, p.hiredate) as prob_start
            FROM personnel p
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE p.user_id = %s
        """, (user_id,))
        reg_row = cursor.fetchone()

        cursor.close()
        db_pool.return_connection(conn)

        # Compute regularization status
        from datetime import date as _date_cls
        _today = _date_cls.today()
        reg_status = 'Unknown'
        reg_percent = 0
        reg_message = ''
        if reg_row:
            _hire_date, _emp_status, _has_master, _prob_start = reg_row
            if _emp_status in ('Regular', 'Tenured'):
                reg_status = _emp_status
                reg_percent = 100
                reg_message = 'Regular status achieved.'
            elif not _has_master:
                reg_status = 'Contractual'
                reg_percent = 0
                reg_message = "Status: Contractual. An aligned Master's Degree is required to begin the 3-year probationary period."
            else:
                reg_status = 'Probationary'
                _prob_days = max(0, (_today - _prob_start).days) if _prob_start else 0
                reg_percent = min(round((_prob_days / 1095) * 100, 1), 100)
                reg_message = f"Probationary Progress: {_prob_days} days completed out of 1,095 ({reg_percent}%)."

        semester_start = date.fromisoformat(semester_info_json['semesterstart'])
        semester_end = date.fromisoformat(semester_info_json['semesterend'])
        
        # CRITICAL FIX 1: Ensure overall_eval_score is cast safely before use
        overall_eval_score = float(overall_eval_score) if overall_eval_score is not None else 0.0
        
        # [Existing logic for calculating attendance rate, etc., remains here]
        
        scheduled_classes = schedule_json or []
        attendance_records = attendance_json or []
        
        attendance_map = {}
        for record in attendance_records:
            class_id = record['class_id']
            timein = record['timein']
            if timein:
                date_key = f"{class_id}_{timein[:10]}"
                attendance_map[date_key] = record['attendancestatus']
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_date = datetime.now(philippines_tz).date()
        
        total_classes = 0
        present_late_count = 0
        
        weekday_map = {
            'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
            'Friday': 4, 'Saturday': 5, 'Sunday': 6
        }
        
        for scheduled_class in scheduled_classes:
            class_id = scheduled_class['class_id']
            
            for day_key in ['1', '2']:
                day = scheduled_class.get(f'classday_{day_key}')
                if not day:
                    continue
                
                target_weekday = weekday_map.get(day)
                if target_weekday is None:
                    continue
                
                check_date = semester_start
                days_ahead = target_weekday - check_date.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                check_date += timedelta(days=days_ahead)
                
                if check_date < semester_start:
                    check_date += timedelta(days=7)
                
                end_date = min(current_date, semester_end)
                while check_date <= end_date:
                    total_classes += 1
                    
                    date_key = f"{class_id}_{check_date}"
                    if date_key in attendance_map:
                        status = attendance_map[date_key].lower()
                        if status in ['present', 'late']:
                            present_late_count += 1
                    
                    check_date += timedelta(days=7)
        
        attendance_rate = {
            'percentage': round((present_late_count / total_classes) * 100) if total_classes > 0 else 0,
            'total_classes': total_classes,
            'present_late': present_late_count
        }
        
        current_day = current_date.strftime('%A')
        current_weekday = current_date.weekday()
        
        day_order = {'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3, 'Friday': 4, 'Saturday': 5, 'Sunday': 6}
        
        def format_time_ampm(time_val):
            if not time_val:
                return 'N/A'
            
            if isinstance(time_val, str):
                time_str = time_val[:5]
            elif hasattr(time_val, 'strftime'):
                time_str = time_val.strftime('%H:%M')
            else:
                time_str = str(time_val)[:5]
            
            try:
                hours, minutes = map(int, time_str.split(':'))
                period = 'AM' if hours < 12 else 'PM'
                display_hour = hours if hours <= 12 else hours - 12
                display_hour = 12 if display_hour == 0 else display_hour
                return f"{display_hour}:{minutes:02d} {period}"
            except:
                return time_str
        
        weekly_schedule = []
        
        for scheduled_class in scheduled_classes:
            subject_code = scheduled_class['subjectcode']
            subject_name = scheduled_class['subjectname']
            section = scheduled_class.get('classsection')
            classroom = scheduled_class.get('classroom')
            
            class_name = f"{subject_code} - {subject_name}"
            
            for day_key in ['1', '2']:
                day = scheduled_class.get(f'classday_{day_key}')
                start_time = scheduled_class.get(f'starttime_{day_key}')
                end_time = scheduled_class.get(f'endtime_{day_key}')
                
                if day and start_time and end_time:
                    start_str = format_time_ampm(start_time)
                    end_str = format_time_ampm(end_time)
                    
                    day_num = day_order.get(day, -1)
                    is_this_week = current_weekday <= day_num <= 5
                    
                    weekly_schedule.append({
                        'class_name': class_name,
                        'subject_code': subject_code,
                        'subject_name': subject_name,
                        'section': section or 'N/A',
                        'day': day,
                        'start_time': start_str,
                        'end_time': end_str,
                        'time_display': f"{start_str}-{end_str}",
                        'classroom': classroom or 'N/A',
                        'is_today': day == current_day,
                        'is_this_week': is_this_week
                    })
        
        weekly_schedule.sort(key=lambda x: (day_order.get(x['day'], 8), x['start_time']))
        
        return {
            'success': True,
            'attendance_rate': attendance_rate,
            'overall_eval_score': overall_eval_score,
            'class_schedule': weekly_schedule,
            'teaching_load': int(teaching_load) if teaching_load else 0,
            'semester_info': {
                'name': semester_info_json['semester'],
                'year': semester_info_json['acadyear'],
                'display': f"{semester_info_json['semester']}, AY {semester_info_json['acadyear']}"
            },
            'regularization_status': reg_status,
            'regularization_percentage': reg_percent,
            'regularization_message': reg_message,
        }
        
    except Exception as e:
        print(f"Error fetching dashboard data: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/today-classes-attendance')
@require_auth([20003])
def api_hr_today_classes_attendance():
    """Get today's attendance statistics based on CLASSES scheduled today"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        philippines_tz = pytz.timezone('Asia/Manila')
        today = datetime.now(philippines_tz).date()
        today_str = today.strftime('%Y-%m-%d')
        current_day = today.strftime('%A')  
        
        cursor.execute("""
            WITH current_calendar AS (
                SELECT acadcalendar_id 
                FROM acadcalendar 
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC
                LIMIT 1
            ),
            classes_today AS (
                SELECT 
                    sch.class_id,
                    sch.personnel_id
                FROM schedule sch
                CROSS JOIN current_calendar cc
                WHERE sch.acadcalendar_id = cc.acadcalendar_id
                AND (
                    (sch.classday_1 = %s AND sch.starttime_1 IS NOT NULL) OR
                    (sch.classday_2 = %s AND sch.starttime_2 IS NOT NULL)
                )
            )
            SELECT COUNT(*) 
            FROM classes_today
        """, (current_day, current_day))
        
        total_classes_today = cursor.fetchone()[0] or 0
        
        if total_classes_today == 0:
            cursor.close()
            db_pool.return_connection(conn)
            return {
                'success': True,
                'attendance_percentage': 0,
                'present_classes': 0,
                'total_classes_today': 0,
                'today_date': today_str,
                'note': 'No classes scheduled today'
            }
        
        cursor.execute("""
            WITH current_calendar AS (
                SELECT acadcalendar_id 
                FROM acadcalendar 
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC
                LIMIT 1
            ),
            classes_today AS (
                SELECT 
                    sch.class_id,
                    sch.personnel_id
                FROM schedule sch
                CROSS JOIN current_calendar cc
                WHERE sch.acadcalendar_id = cc.acadcalendar_id
                AND (
                    (sch.classday_1 = %s AND sch.starttime_1 IS NOT NULL) OR
                    (sch.classday_2 = %s AND sch.starttime_2 IS NOT NULL)
                )
            )
            SELECT COUNT(DISTINCT a.class_id)
            FROM attendance a
            JOIN classes_today ct ON a.class_id = ct.class_id
            WHERE DATE(a.timein AT TIME ZONE 'Asia/Manila') = %s
            AND a.attendancestatus IN ('Present', 'Late', 'Excused')
            -- Removed the timein filter to include excused classes that don't have actual time-in
        """, (current_day, current_day, today))
        
        present_classes = cursor.fetchone()[0] or 0
        
        cursor.close()
        db_pool.return_connection(conn)
        
        attendance_percentage = round((present_classes / total_classes_today) * 100) if total_classes_today > 0 else 0
        
        return {
            'success': True,
            'attendance_percentage': attendance_percentage,
            'present_classes': present_classes,
            'total_classes_today': total_classes_today,
            'today_date': today_str,
            'note': f'Classes scheduled on {current_day}'
        }
        
    except Exception as e:
        print(f"Error fetching today's classes attendance stats: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/auditlogs')
@require_auth([20003])  
def api_audit_logs():
    """Get audit logs for HR view"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                al.audit_id,
                al.personnel_id,
                al.action,
                al.details,
                al.created_at,
                p.firstname,
                p.lastname,
                p.honorifics
            FROM auditlogs al
            LEFT JOIN personnel p ON al.personnel_id = p.personnel_id
            ORDER BY al.created_at DESC
        """)
        
        logs = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        audit_logs = []
        for log in logs:
            (audit_id, personnel_id, action, details, created_at, 
             firstname, lastname, honorifics) = log
            
            if personnel_id and firstname and lastname:
                personnel_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            else:
                personnel_name = "System"
            
            if created_at:
                created_dt = created_at.astimezone(pytz.timezone('Asia/Manila'))
                date_str = created_dt.strftime('%Y-%m-%d %H:%M:%S')
            else:
                date_str = "N/A"
            
            audit_logs.append({
                'audit_id': audit_id,
                'personnel_name': personnel_name,
                'action': action,
                'details': details or "",
                'date': date_str
            })
        
        return {
            'success': True,
            'audit_logs': audit_logs
        }
        
    except Exception as e:
        print(f"Error fetching audit logs: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

def log_audit_action(personnel_id, action, details=None, before_value=None, after_value=None, evidence=None):
    """Log audit actions to the auditlogs table with improved formatting"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        def clean_value(value):
            if value is None or value == '' or value == '00:00:00':
                return 'None'
            return str(value)
        
        formatted_details = details or ""
        
        if before_value is not None:
            formatted_details += f"\nBefore: {clean_value(before_value)}"
        
        if after_value is not None:
            formatted_details += f"\nAfter: {clean_value(after_value)}"
        
        cursor.execute("""
            INSERT INTO auditlogs (personnel_id, action, details, created_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        """, (personnel_id, action, formatted_details))
        
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        
        print(f"📝 Audit logged: {action} by personnel_id {personnel_id}")
        return True
        
    except Exception as e:
        print(f"❌ Error logging audit action: {e}")
        return False
    
@app.route('/api/faculty/profile')
@require_auth([20001, 20002, 20003, 20004])
def api_get_faculty_profile():
    """OPTIMIZED: Get faculty profile data - AUTO CREATES PROFILE IF NOT EXISTS"""
    try:
        viewing_personnel_id = session.get('viewing_personnel_id')
        
        if viewing_personnel_id:
            personnel_id = viewing_personnel_id
        else:
            personnel_info = get_personnel_info(session['user_id'])
            personnel_id = personnel_info.get('personnel_id')
            
            if not personnel_id:
                return {'success': False, 'error': 'Personnel record not found'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                pe.phone,
                pr.bio, 
                pr.profilepic, 
                pr.licenses, 
                pr.degrees, 
                pr.certificates, 
                pr.publications, 
                pr.awards,
                pr.licensesname, 
                pr.degreesname, 
                pr.certificatesname, 
                pr.publicationsname, 
                pr.awardsname
            FROM personnel pe
            LEFT JOIN profile pr ON pe.personnel_id = pr.personnel_id
            WHERE pe.personnel_id = %s
        """, (personnel_id,))
        
        profile_result = cursor.fetchone()
        
        if not profile_result or profile_result[1] is None:
            if not viewing_personnel_id:
                print(f"Profile not found for personnel_id: {personnel_id}. Creating new profile...")
                
                cursor.execute("SELECT COALESCE(MAX(profile_id), 90000) FROM profile")
                max_profile_id = cursor.fetchone()[0]
                new_profile_id = max_profile_id + 1
                
                cursor.execute("""
                    INSERT INTO profile (
                        profile_id, personnel_id, bio, profilepic, 
                        licenses, degrees, certificates, publications, awards,
                        licensesname, degreesname, certificatesname,
                        publicationsname, awardsname,
                        employmentstatus, position
                    )
                    VALUES (%s, %s, '', NULL, 
                            ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[],
                            ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[],
                            'Regular', 'Full-Time Employee')
                """, (new_profile_id, personnel_id))
                conn.commit()
                
                cursor.execute("""
                    SELECT 
                        pe.phone,
                        pr.bio, 
                        pr.profilepic, 
                        pr.licenses, 
                        pr.degrees, 
                        pr.certificates, 
                        pr.publications, 
                        pr.awards,
                        pr.licensesname, 
                        pr.degreesname, 
                        pr.certificatesname, 
                        pr.publicationsname, 
                        pr.awardsname
                    FROM personnel pe
                    LEFT JOIN profile pr ON pe.personnel_id = pr.personnel_id
                    WHERE pe.personnel_id = %s
                """, (personnel_id,))
                profile_result = cursor.fetchone()
                
                print(f"New profile created with ID: {new_profile_id} for personnel_id: {personnel_id}")
        
        (phone, bio, profilepic, licenses, degrees, certificates, publications, awards,
         licenses_fn, degrees_fn, certificates_fn, publications_fn, awards_fn) = profile_result
        
        import base64
        profile_data = {
            'phone': str(phone) if phone else '',
            'bio': bio or '',
            'profilepic': None,
            'licenses': [],
            'degrees': [],
            'certificates': [],
            'publications': [],
            'awards': [],
            'licenses_filename': licenses_fn or [],
            'degrees_filename': degrees_fn or [],
            'certificates_filename': certificates_fn or [],
            'publications_filename': publications_fn or [],
            'awards_filename': awards_fn or []
        }
        
        if profilepic:
            profile_data['profilepic'] = base64.b64encode(bytes(profilepic)).decode('utf-8')
        
        for doc_type in ['licenses', 'degrees', 'certificates', 'publications', 'awards']:
            doc_array = locals()[doc_type]
            if doc_array and len(doc_array) > 0:
                profile_data[doc_type] = [base64.b64encode(bytes(doc)).decode('utf-8') for doc in doc_array]

        cursor.execute("""
            SELECT tracker_id, license_type, license_number, expiration_date, date_uploaded
            FROM faculty_license_tracker
            WHERE personnel_id = %s
            ORDER BY date_uploaded ASC
        """, (personnel_id,))
        tracker_rows = cursor.fetchall()
        profile_data['licenses_tracker'] = [
            {
                'tracker_id': row[0],
                'license_type': str(row[1]) if row[1] else '',
                'license_number': str(row[2]) if row[2] else '',
                'expiration_date': str(row[3]) if row[3] else '',
                'date_uploaded': str(row[4]) if row[4] else ''
            }
            for row in tracker_rows
        ]

        cursor.close()
        db_pool.return_connection(conn)

        return {'success': True, 'profile': profile_data}
        
    except Exception as e:
        print(f"Error fetching profile data: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/license-alerts')
@require_auth([20001, 20002, 20003, 20004])
def api_faculty_license_alerts():
    """Return all license_tracker records for the logged-in faculty with computed expiry status."""
    try:
        personnel_info = get_personnel_info(session['user_id'])
        personnel_id = personnel_info.get('personnel_id')
        if not personnel_id:
            return {'success': False, 'error': 'Personnel record not found'}, 401

        conn = db_pool.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT tracker_id, license_type, license_number, expiration_date, date_uploaded
            FROM faculty_license_tracker
            WHERE personnel_id = %s
            ORDER BY expiration_date ASC NULLS LAST
        """, (personnel_id,))
        rows = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)

        from datetime import date as _date
        today = _date.today()
        licenses = []
        for row in rows:
            tracker_id, license_type, license_number, expiration_date, date_uploaded = row
            days = None
            if expiration_date:
                days = (expiration_date - today).days
                if days < 0:
                    status = 'expired'
                elif days <= 30:
                    status = 'expiring_30'
                elif days <= 60:
                    status = 'expiring_60'
                elif days <= 90:
                    status = 'expiring_90'
                else:
                    status = 'valid'
            else:
                status = 'unknown'

            licenses.append({
                'tracker_id': tracker_id,
                'license_type': str(license_type) if license_type else '',
                'license_number': str(license_number) if license_number else '',
                'expiration_date': str(expiration_date) if expiration_date else '',
                'date_uploaded': str(date_uploaded) if date_uploaded else '',
                'status': status,
                'days_until_expiry': days,
            })

        return {'success': True, 'licenses': licenses}

    except Exception as e:
        print(f"Error in api_faculty_license_alerts: {e}")
        return {'success': False, 'error': str(e)}, 500


@app.route('/api/faculty/profile/stats')
@require_auth([20001, 20002, 20003, 20004])
def api_get_profile_stats():
    """OPTIMIZED: Get profile statistics with single query - INCLUDES ATTENDANCE RATING and EVALUATION SCORE"""
    try:
        viewing_personnel_id = session.get('viewing_personnel_id')
        if viewing_personnel_id:
            personnel_id = viewing_personnel_id
        else:
            personnel_info = get_personnel_info(session['user_id'])
            personnel_id = personnel_info.get('personnel_id')
            
            if not personnel_id:
                return {'success': False, 'error': 'Personnel record not found'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT acadcalendar_id FROM acadcalendar 
            WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
            ORDER BY semesterstart DESC LIMIT 1
        """)
        current_semester_result = cursor.fetchone()
        current_semester_id = current_semester_result[0] if current_semester_result else None
        
        cursor.execute("""
            WITH evaluation_data AS (
                -- Calculate Overall Weighted Evaluation Score (55/35/10)
                SELECT
                    COALESCE(
                        SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) +
                        SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) +
                        SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END),
                    0) AS overall_eval_score
                FROM faculty_evaluations fe
                WHERE fe.personnel_id = %s AND fe.acadcalendar_id = %s
            )
            SELECT 
                pe.hiredate,
                COALESCE(array_length(pr.certificates, 1), 0) as certificates_count,
                COALESCE(array_length(pr.publications, 1), 0) as publications_count,
                COALESCE(array_length(pr.awards, 1), 0) as awards_count,
                -- Get average attendance rate across all classes for current semester
                COALESCE(
                    (SELECT AVG(ar.attendancerate) 
                     FROM attendancereport ar 
                     WHERE ar.personnel_id = %s 
                     AND ar.acadcalendar_id = %s),
                    0
                ) as avg_attendance_rate,
                (SELECT overall_eval_score FROM evaluation_data) as overall_eval_score
            FROM personnel pe
            LEFT JOIN profile pr ON pe.personnel_id = pr.personnel_id
            WHERE pe.personnel_id = %s
        """, (personnel_id, current_semester_id, personnel_id, current_semester_id, personnel_id))
        
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if not result:
            return {'success': False, 'error': 'Personnel record not found'}
        
        hire_date, certificates_count, publications_count, awards_count, avg_attendance_rate, overall_eval_score = result
        
        years_of_service = 0
        if hire_date:
            today = datetime.now().date()
            years_of_service = today.year - hire_date.year
            if today.month < hire_date.month or (today.month == hire_date.month and today.day < hire_date.day):
                years_of_service -= 1
        
        stats = {
            'years_of_service': years_of_service,
            'professional_certifications': certificates_count,
            'research_publications': publications_count,
            'awards_count': awards_count,
            'attendance_rating': round(float(avg_attendance_rate), 2) if avg_attendance_rate else 0,
            'overall_eval_score': round(float(overall_eval_score), 2) if overall_eval_score else 0.0
        }
        
        return {'success': True, 'stats': stats}
        
    except Exception as e:
        print(f"Error fetching profile stats: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}


@app.route('/api/faculty/profile/personal', methods=['POST'])
@require_auth([20001, 20002, 20003, 20004])
def api_update_personal_info():
    """API endpoint to update personal information"""
    try:
        user_id = session['user_id']
        data = request.get_json()
        
        phone_str = data.get('phone', '').strip()
        bio = data.get('bio', '').strip()
        
        phone = None
        if phone_str and phone_str != '+63 ' and phone_str != '+63':
            phone_clean = phone_str.replace(' ', '')
            
            if phone_clean.startswith('+63'):
                phone_digits = phone_clean[3:]
                
                if len(phone_digits) == 10 and phone_digits[0] == '9' and phone_digits.isdigit():
                    phone = '+63 ' + phone_digits
                else:
                    return {'success': False, 'error': 'Phone number must be +63 followed by 10 digits starting with 9'}
            else:
                return {'success': False, 'error': 'Phone number must start with +63'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        if not personnel_id:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Personnel record not found'}
        
        cursor.execute("SELECT phone FROM personnel WHERE personnel_id = %s", (personnel_id,))
        current_phone_result = cursor.fetchone()
        current_phone = current_phone_result[0] if current_phone_result else None
        
        cursor.execute("SELECT bio FROM profile WHERE personnel_id = %s", (personnel_id,))
        current_bio_result = cursor.fetchone()
        current_bio = current_bio_result[0] if current_bio_result else None
        
        cursor.execute("""
            UPDATE personnel SET phone = %s WHERE personnel_id = %s
        """, (phone, personnel_id))
        
        cursor.execute("""
            SELECT profile_id FROM profile WHERE personnel_id = %s
        """, (personnel_id,))
        profile_exists = cursor.fetchone()
        
        if profile_exists:
            cursor.execute("""
                UPDATE profile SET bio = %s WHERE personnel_id = %s
            """, (bio, personnel_id))
        else:
            cursor.execute("SELECT COALESCE(MAX(profile_id), 90000) FROM profile")
            max_profile_id = cursor.fetchone()[0]
            new_profile_id = max_profile_id + 1
            
            cursor.execute("""
                INSERT INTO profile (
                    profile_id, personnel_id, bio, profilepic, 
                    licenses, degrees, certificates, publications, awards,
                    licensesname, degreesname, certificatesname, publicationsname, awardsname,
                    employmentstatus, position
                )
                VALUES (%s, %s, %s, NULL, 
                        ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[],
                        ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[],
                        'Regular', 'Full-Time Employee')
            """, (new_profile_id, personnel_id, bio))
        
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        
        cache_key = f"personnel_info_{user_id}"
        with _cache_lock:
            _cache.pop(cache_key, None)
        
        changes_logged = []
        
        if str(current_phone) != str(phone):
            changes_logged.append("phone")
            log_audit_action(
                personnel_id,
                "Phone number updated", 
                "User updated their phone number",
                before_value=f"Phone: {current_phone}" if current_phone else "Phone: Not set",
                after_value=f"Phone: {phone}" if phone else "Phone: Removed"
            )
        
        if current_bio != bio:
            changes_logged.append("bio")
            
            log_audit_action(
                personnel_id,
                "Bio updated", 
                "User updated their bio information",
                before_value=f"Bio: {current_bio}" if current_bio else "Bio: Not set",
                after_value=f"Bio: {bio}" if bio else "Bio: Removed"
            )
        
        if changes_logged:
            print(f"Personal info updated for personnel_id: {personnel_id}, changes: {', '.join(changes_logged)}")
        
        return {'success': True, 'message': 'Personal information updated successfully'}
        
    except Exception as e:
        print(f"Error updating personal info: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/profile/documents', methods=['POST'])
@require_auth([20001, 20002, 20003, 20004])
def api_update_documents():
    """API endpoint to update document uploads - FIXED VERSION"""
    try:
        user_id = session['user_id']
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        if not personnel_id:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Personnel record not found'}
        
        cursor.execute("SELECT profile_id FROM profile WHERE personnel_id = %s", (personnel_id,))
        profile_exists = cursor.fetchone()
        
        if not profile_exists:
            cursor.execute("SELECT COALESCE(MAX(profile_id), 90000) FROM profile")
            max_profile_id = cursor.fetchone()[0]
            new_profile_id = max_profile_id + 1
            
            cursor.execute("""
                INSERT INTO profile (
                    profile_id, personnel_id, bio, profilepic, 
                    licenses, degrees, certificates, publications, awards,
                    licensesname, degreesname, certificatesname, publicationsname, awardsname,
                    employmentstatus, position
                )
                VALUES (%s, %s, '', NULL,
                        ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[],
                        ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[],
                        'Regular', 'Full-Time Employee')
            """, (new_profile_id, personnel_id))
            conn.commit()
            print(f"Created new profile with ID: {new_profile_id} for personnel_id: {personnel_id}")
        
        uploaded_docs = []
        
        if 'profilepic' in request.files:
            file = request.files['profilepic']
            if file and file.filename:
                profilepic_data = file.read()
                new_profilepic_filename = file.filename
                
                cursor.execute("SELECT profilepic FROM profile WHERE personnel_id = %s", (personnel_id,))
                existing_profilepic = cursor.fetchone()
                
                before_filename = "profile_picture.jpg" if existing_profilepic and existing_profilepic[0] else "None"
                after_filename = new_profilepic_filename
                
                cursor.execute("""
                    UPDATE profile SET profilepic = %s WHERE personnel_id = %s
                """, (profilepic_data, personnel_id))
                conn.commit()
                
                action = "Profile picture updated" if existing_profilepic and existing_profilepic[0] else "Profile picture uploaded"
                uploaded_docs.append("profile picture")

                log_audit_action(
                    personnel_id,
                    action,
                    f"User {'updated' if existing_profilepic and existing_profilepic[0] else 'uploaded'} profile picture",
                    before_value=before_filename,
                    after_value=after_filename
                )
        
        column_mapping = {
            'licenses': 'licensesname',
            'degrees': 'degreesname',
            'certificates': 'certificatesname',
            'publications': 'publicationsname',
            'awards': 'awardsname'
        }
        
        license_type = request.form.get('license_type', '').strip()
        license_number = request.form.get('license_number', '').strip()
        license_expiration_date = request.form.get('license_expiration_date', '').strip() or None

        for doc_type in ['licenses', 'degrees', 'certificates', 'publications', 'awards']:
            files = request.files.getlist(doc_type)

            if files and any(f.filename for f in files):
                filename_col = column_mapping[doc_type]
                cursor.execute(f"""
                    SELECT {doc_type}, {filename_col}
                    FROM profile
                    WHERE personnel_id = %s
                """, (personnel_id,))
                existing_result = cursor.fetchone()

                existing_docs = list(existing_result[0]) if existing_result and existing_result[0] else []
                existing_filenames = list(existing_result[1]) if existing_result and existing_result[1] else []

                new_docs = []
                new_filenames = []

                for f in files:
                    if f.filename:
                        file_data = f.read()
                        new_docs.append(file_data)
                        new_filenames.append(f.filename)
                        uploaded_docs.append(f"{doc_type}: {f.filename}")

                if new_docs:
                    before_filenames = existing_filenames if existing_filenames else ["None"]

                    combined_docs = existing_docs + new_docs
                    combined_filenames = existing_filenames + new_filenames

                    after_filenames = combined_filenames if combined_filenames else ["None"]

                    cursor.execute(f"""
                        UPDATE profile
                        SET {doc_type} = %s, {filename_col} = %s
                        WHERE personnel_id = %s
                    """, (combined_docs, combined_filenames, personnel_id))
                    conn.commit()

                    if doc_type == 'licenses':
                        for _ in new_docs:
                            cursor.execute("""
                                INSERT INTO faculty_license_tracker (personnel_id, license_type, license_number, expiration_date, date_uploaded)
                                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                            """, (personnel_id, license_type or None, license_number or None, license_expiration_date))
                        conn.commit()

                    before_text = ", ".join(before_filenames) if before_filenames != ["None"] else "None"
                    after_text = ", ".join(after_filenames) if after_filenames != ["None"] else "None"

                    log_audit_action(
                        personnel_id,
                        f"{doc_type.capitalize()} uploaded",
                        f"User uploaded {doc_type} document(s)",
                        before_value=before_text,
                        after_value=after_text
                    )
        
        cursor.close()
        db_pool.return_connection(conn)
        
        return {'success': True, 'message': 'Documents updated successfully'}
        
    except Exception as e:
        print(f"Error updating documents: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}


@app.route('/api/faculty/submit-degree', methods=['POST'])
@require_auth([20001, 20002])
def api_submit_degree():
    """Faculty submits a degree document for HR review with metadata."""
    try:
        user_id = session['user_id']
        degree_level = request.form.get('degree_level', '').strip()
        institution = request.form.get('institution', '').strip()
        date_obtained = request.form.get('date_obtained', '').strip() or None

        if not degree_level:
            return jsonify({'success': False, 'error': 'Degree level is required'}), 400
        if 'degree_file' not in request.files or not request.files['degree_file'].filename:
            return jsonify({'success': False, 'error': 'A degree document file is required'}), 400

        degree_file = request.files['degree_file']
        file_data = degree_file.read()
        filename = degree_file.filename

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT p.personnel_id, p.firstname, p.lastname
            FROM personnel p WHERE p.user_id = %s
        """, (user_id,))
        result = cursor.fetchone()
        if not result:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Faculty not found'}), 404

        personnel_id, firstname, lastname = result
        faculty_name = f"{lastname}, {firstname}"

        # Append degree file to profile
        cursor.execute("""
            SELECT degrees, degreesname FROM profile WHERE personnel_id = %s
        """, (personnel_id,))
        profile_result = cursor.fetchone()
        existing_docs = list(profile_result[0]) if profile_result and profile_result[0] else []
        existing_names = list(profile_result[1]) if profile_result and profile_result[1] else []

        cursor.execute("""
            UPDATE profile SET degrees = %s, degreesname = %s WHERE personnel_id = %s
        """, (existing_docs + [file_data], existing_names + [filename], personnel_id))
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)

        # Build notification message
        details = degree_level
        if institution:
            details += f" from {institution}"
        if date_obtained:
            details += f" (obtained {date_obtained})"

        notif_data = {
            'notification_type': 'degree_submission',
            'person_name': faculty_name,
            'personnel_id': personnel_id,
            'action': 'degree_submitted',
            'status': 'pending',
            'message': f"{faculty_name} submitted a {details} degree document for HR review.",
            'tap_time': datetime.now(pytz.timezone('Asia/Manila')).isoformat()
        }
        save_notification_to_db('hr', None, notif_data)
        _push_to_queue('hr_all_notifications', notif_data)

        log_audit_action(
            personnel_id,
            'Degree document submitted',
            f"Faculty submitted {degree_level} degree document for HR review",
            after_value=f"{filename} | institution={institution or 'N/A'} | date={date_obtained or 'N/A'}"
        )

        return jsonify({'success': True, 'message': 'Degree submitted successfully. HR has been notified.'})

    except Exception as e:
        print(f"Error submitting degree: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/faculty/profile/password', methods=['POST'])
@require_auth([20001, 20002, 20003, 20004])
def api_update_password():
    """API endpoint to update password"""
    try:
        user_id = session['user_id']
        data = request.get_json()
        
        current_password = data.get('current_password', '')
        new_password = data.get('new_password', '')
        confirm_password = data.get('confirm_password', '')
        
        if not current_password or not new_password or not confirm_password:
            return {'success': False, 'error': 'All password fields are required'}
        
        if new_password != confirm_password:
            return {'success': False, 'error': 'New passwords do not match'}
        
        if len(new_password) < 6:
            return {'success': False, 'error': 'Password must be at least 6 characters long'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT password FROM users WHERE user_id = %s", (user_id,))
        current_pass_result = cursor.fetchone()
        
        if not current_pass_result or current_pass_result[0] != current_password:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Current password is incorrect'}
        
        if current_password == new_password:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'New password cannot be the same as current password'}
        
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        cursor.execute("""
            UPDATE users SET password = %s WHERE user_id = %s
        """, (new_password, user_id))
        
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        
        if personnel_id:
            log_audit_action(
                personnel_id,
                "Password changed", 
                "User changed their password",
                before_value="[HIDDEN]",
                after_value="[HIDDEN]"
            )
        
        return {'success': True, 'message': 'Password updated successfully'}
        
    except Exception as e:
        print(f"Error updating password: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/profile/document/<doc_type>/<int:index>', methods=['DELETE'])
@require_auth([20001, 20002, 20003, 20004])
def api_delete_document(doc_type, index):
    """API endpoint to delete a specific document from an array - FIXED VERSION"""
    try:
        user_id = session['user_id']
        
        if doc_type not in ['licenses', 'degrees', 'certificates', 'publications', 'awards']:
            return {'success': False, 'error': 'Invalid document type'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        if not personnel_id:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Personnel record not found'}
        
        column_mapping = {
            'licenses': 'licensesname',
            'degrees': 'degreesname',
            'certificates': 'certificatesname',
            'publications': 'publicationsname',
            'awards': 'awardsname'
        }
        
        filename_col = column_mapping[doc_type]
        
        cursor.execute(f"""
            SELECT {doc_type}, {filename_col} 
            FROM profile 
            WHERE personnel_id = %s
        """, (personnel_id,))
        doc_result = cursor.fetchone()
        
        if not doc_result or not doc_result[0] or len(doc_result[0]) == 0:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'No documents found'}
        
        doc_array = list(doc_result[0])
        filenames = list(doc_result[1]) if doc_result[1] else []
        
        if index < 0 or index >= len(doc_array):
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Invalid document index'}
        
        deleted_filename = filenames[index] if index < len(filenames) else f"Document_{index+1}"
        before_filenames = filenames.copy() if filenames else ["None"]
        
        doc_array.pop(index)
        if index < len(filenames):
            filenames.pop(index)
        
        after_filenames = filenames if filenames else ["None"]
        
        cursor.execute(f"""
            UPDATE profile 
            SET {doc_type} = %s, {filename_col} = %s 
            WHERE personnel_id = %s
        """, (doc_array, filenames, personnel_id))
        
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        
        before_text = ", ".join(before_filenames) if before_filenames != ["None"] else "None"
        after_text = ", ".join(after_filenames) if after_filenames != ["None"] else "None"
        
        log_audit_action(
            personnel_id,
            f"{doc_type.capitalize()} deleted",
            f"User deleted {doc_type} document: {deleted_filename}",
            before_value=before_text,
            after_value=after_text
        )
        
        return {'success': True, 'message': 'Document deleted successfully'}
        
    except Exception as e:
        print(f"Error deleting document: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/faculty-attendance')
@require_auth([20003])
def api_hr_faculty_attendance():
    """SIMPLE: Get all faculty attendance data - SHOW ONLY WHAT'S IN DATABASE"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        philippines_tz = pytz.timezone('Asia/Manila')
        today = datetime.now(philippines_tz).date()

        semester_id = request.args.get('semester_id')
        sem_start = sem_end = None
        if semester_id:
            cursor.execute("SELECT semesterstart, semesterend FROM acadcalendar WHERE acadcalendar_id = %s", (int(semester_id),))
            row = cursor.fetchone()
            if row:
                sem_start, sem_end = row[0], row[1]

        date_filter = "AND a.timein::date BETWEEN %s AND %s" if (sem_start and sem_end) else ""
        params = [sem_start, sem_end] if (sem_start and sem_end) else []

        cursor.execute("""
            SELECT
                p.firstname,
                p.lastname,
                p.honorifics,
                a.attendancestatus,
                a.timein,
                a.timeout,
                sch.classroom,
                sub.subjectcode,
                sub.subjectname,
                sch.classsection,
                c.collegename,
                pr.position,
                pr.employmentstatus
            FROM attendance a
            JOIN personnel p ON a.personnel_id = p.personnel_id
            JOIN schedule sch ON a.class_id = sch.class_id
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE p.role_id IN (20001, 20002)
            """ + date_filter + """
            ORDER BY a.timein DESC, p.lastname, p.firstname
        """, params)
        
        attendance_records = cursor.fetchall()
        
        cursor.execute("SELECT COUNT(*) FROM personnel WHERE role_id IN (20001, 20002)")
        total_faculty = cursor.fetchone()[0]
        
        cursor.close()
        db_pool.return_connection(conn)
        
        attendance_logs = []
        status_counts = {'present': 0, 'late': 0, 'absent': 0, 'excused': 0}
        today_counts = {'present': 0, 'late': 0, 'absent': 0, 'excused': 0}
        
        seen_records = set()
        
        for record in attendance_records:
            (firstname, lastname, honorifics, status, timein, timeout,
             classroom, subject_code, subject_name, class_section,
             collegename, position, employmentstatus) = record
            
            faculty_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            class_name = f"{subject_code} - {subject_name}"
            
            date_str = "N/A"
            time_in_str = '—'
            time_out_str = '—'
            
            if timein:
                if timein.tzinfo:
                    timein_ph = timein.astimezone(philippines_tz)
                else:
                    timein_ph = philippines_tz.localize(timein)
                
                date_str = timein_ph.strftime('%Y-%m-%d')
                is_absent_record = (status.lower() == 'absent' and 
                                  timein_ph.hour == 0 and 
                                  timein_ph.minute == 0 and 
                                  timein_ph.second == 0)
                
                if not is_absent_record:
                    time_in_str = timein_ph.strftime('%H:%M')
            
            if timeout and status.lower() != 'absent':
                if timeout.tzinfo:
                    timeout_ph = timeout.astimezone(philippines_tz)
                else:
                    timeout_ph = philippines_tz.localize(timeout)
                time_out_str = timeout_ph.strftime('%H:%M')
            
            record_key = f"{faculty_name}_{date_str}_{class_name}_{class_section}_{time_in_str}_{time_out_str}"
            
            if record_key not in seen_records:
                seen_records.add(record_key)
                
                log_entry = {
                    'name': faculty_name,
                    'date': date_str,
                    'class_name': class_name,
                    'class_section': class_section or 'N/A',
                    'room': classroom or 'N/A',
                    'time_in': time_in_str,
                    'time_out': time_out_str,
                    'status': status.capitalize(),
                    'college': collegename or 'N/A',
                    'position': position or 'N/A',
                    'employment_status': employmentstatus or 'N/A'
                }
                attendance_logs.append(log_entry)
                status_lower = status.lower()
                if status_lower in status_counts:
                    status_counts[status_lower] += 1
                
                if date_str == today.strftime('%Y-%m-%d') and status_lower in today_counts:
                    today_counts[status_lower] += 1
                
                # print(f"✅ Database record: {faculty_name} - {date_str} - {class_name}")
            else:
                print(f"🚨 DUPLICATE SKIPPED: {record_key}")
        
        # print(f"📊 Database records: {len(attendance_records)}, Displayed: {len(attendance_logs)}")
        
        kpis = {
            'total_faculty': total_faculty or 0,
            'present_today': today_counts['present'] or 0,
            'late_today': today_counts['late'] or 0,
            'absent_today': today_counts['absent'] or 0
        }
        
        return {
            'success': True,
            'attendance_logs': attendance_logs,
            'status_breakdown': status_counts,
            'kpis': kpis
        }
        
    except Exception as e:
        print(f"Error fetching HR faculty attendance data: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/attendance-analytics')
@require_auth([20003])
def api_hr_attendance_analytics():
    """Attendance analytics: trends by month for selected semester + distribution/breakdown"""
    try:
        semester_id = request.args.get('semester_id')

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        # --- 1. Resolve semester first ---
        if not semester_id:
            cursor.execute("""
                SELECT acadcalendar_id FROM acadcalendar
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC LIMIT 1
            """)
            result = cursor.fetchone()
            if result:
                semester_id = result[0]
            else:
                cursor.execute("""
                    SELECT acadcalendar_id FROM acadcalendar
                    ORDER BY semesterstart DESC LIMIT 1
                """)
                result = cursor.fetchone()
                if result:
                    semester_id = result[0]

        # --- 2. Get semester date range ---
        sem_start = sem_end = None
        if semester_id:
            cursor.execute("""
                SELECT semesterstart, semesterend FROM acadcalendar
                WHERE acadcalendar_id = %s
            """, (semester_id,))
            sem_row = cursor.fetchone()
            if sem_row:
                sem_start, sem_end = sem_row

        # --- 3. Trends: by week for the selected semester ---
        trends = []
        if semester_id and sem_start and sem_end:
            cursor.execute("""
                SELECT
                    DATE_TRUNC('week', a.timein AT TIME ZONE 'Asia/Manila')::date AS week_start,
                    COUNT(*) FILTER (WHERE a.attendancestatus = 'Present')  AS total_present,
                    COUNT(*) FILTER (WHERE a.attendancestatus = 'Late')     AS total_late,
                    COUNT(*) FILTER (WHERE a.attendancestatus = 'Absent')   AS total_absent,
                    COUNT(*) FILTER (WHERE a.attendancestatus = 'Excused')  AS total_excused,
                    COUNT(*)                                                 AS total
                FROM attendance a
                JOIN personnel p ON a.personnel_id = p.personnel_id
                JOIN schedule sch ON a.class_id = sch.class_id
                WHERE p.role_id IN (20001, 20002)
                  AND a.timein IS NOT NULL
                  AND sch.acadcalendar_id = %s
                GROUP BY DATE_TRUNC('week', a.timein AT TIME ZONE 'Asia/Manila')::date
                ORDER BY week_start ASC
            """, (semester_id,))

            week_data = {}
            for row in cursor.fetchall():
                (week_start, tot_p, tot_l, tot_a, tot_e, total) = row
                tot_p = int(tot_p); tot_l = int(tot_l)
                tot_a = int(tot_a); tot_e = int(tot_e); total = int(total)
                avg_r = round(((tot_p + tot_e + tot_l * 0.75) / total) * 100, 2) if total > 0 else 0.0
                label = week_start.strftime('%b %d') if week_start else ''
                week_data[label] = {
                    'label': label,
                    'total_present': tot_p,
                    'total_late': tot_l,
                    'total_absent': tot_a,
                    'total_excused': tot_e,
                    'avg_rate': avg_r
                }

            # Fill every week in the semester range (zeros if no data)
            from datetime import date as _date, timedelta as _timedelta
            # Snap to Monday of the semester start week
            cur_w = sem_start - _timedelta(days=sem_start.weekday())
            while cur_w <= sem_end:
                label = cur_w.strftime('%b %d')
                trends.append(week_data.get(label, {
                    'label': label,
                    'total_present': 0,
                    'total_late': 0,
                    'total_absent': 0,
                    'total_excused': 0,
                    'avg_rate': 0.0
                }))
                cur_w += _timedelta(weeks=1)

        distribution = {'present': 0, 'late': 0, 'absent': 0, 'excused': 0}
        analytics_kpis = {
            'avg_rate': 0.0, 'total_present': 0, 'total_late': 0,
            'total_absent': 0, 'total_excused': 0, 'faculty_count': 0
        }
        faculty_breakdown = []

        if semester_id:
            # Distribution totals for selected semester
            cursor.execute("""
                SELECT
                    COALESCE(SUM(ar.presentcount), 0),
                    COALESCE(SUM(ar.latecount), 0),
                    COALESCE(SUM(ar.absentcount), 0),
                    COALESCE(SUM(ar.excusedcount), 0),
                    COUNT(DISTINCT ar.personnel_id),
                    CASE
                        WHEN COUNT(ar.attendancereport_id) > 0
                        THEN ROUND(AVG(ar.attendancerate)::numeric, 2)
                        ELSE 0
                    END
                FROM attendancereport ar
                JOIN personnel p ON ar.personnel_id = p.personnel_id
                WHERE ar.acadcalendar_id = %s
                  AND p.role_id IN (20001, 20002)
            """, (semester_id,))
            dist_row = cursor.fetchone()
            if dist_row:
                (tot_p, tot_l, tot_a, tot_e, fac_cnt, avg_r) = dist_row
                distribution = {
                    'present': int(tot_p), 'late': int(tot_l),
                    'absent': int(tot_a), 'excused': int(tot_e)
                }
                analytics_kpis = {
                    'avg_rate': float(avg_r),
                    'total_present': int(tot_p), 'total_late': int(tot_l),
                    'total_absent': int(tot_a), 'total_excused': int(tot_e),
                    'faculty_count': int(fac_cnt)
                }

            # Per-faculty breakdown sorted by attendance rate ascending
            cursor.execute("""
                SELECT
                    p.firstname, p.lastname, p.honorifics,
                    sub.subjectcode, sub.subjectname,
                    sch.classsection,
                    ar.presentcount, ar.latecount, ar.absentcount,
                    ar.excusedcount, ar.totalclasses, ar.attendancerate,
                    c.collegename, pr.position, pr.employmentstatus,
                    ar.personnel_id, ar.class_id
                FROM attendancereport ar
                JOIN personnel p  ON ar.personnel_id = p.personnel_id
                JOIN schedule sch ON ar.class_id     = sch.class_id
                JOIN subjects sub ON sch.subject_id  = sub.subject_id
                LEFT JOIN college c ON p.college_id = c.college_id
                LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
                WHERE ar.acadcalendar_id = %s
                  AND p.role_id IN (20001, 20002)
                ORDER BY ar.attendancerate ASC, p.lastname, p.firstname
            """, (semester_id,))
            for row in cursor.fetchall():
                (fn, ln, hon, scode, sname, section, pres, late, absent, excused, total, rate,
                 collegename, position, employmentstatus, pid, cid) = row
                name = f"{ln}, {fn}, {hon}" if hon else f"{ln}, {fn}"
                faculty_breakdown.append({
                    'faculty_name': name,
                    'subject_code': scode,
                    'subject_name': sname,
                    'section': section or '—',
                    'present': int(pres),
                    'late': int(late),
                    'absent': int(absent),
                    'excused': int(excused),
                    'total': int(total),
                    'rate': round(float(rate), 2),
                    'college': collegename or 'N/A',
                    'position': position or 'N/A',
                    'employment_status': employmentstatus or 'N/A',
                    'personnel_id': pid,
                    'class_id': cid
                })

        cursor.close()
        db_pool.return_connection(conn)

        return jsonify({
            'success': True,
            'trends': trends,
            'distribution': distribution,
            'analytics_kpis': analytics_kpis,
            'faculty_breakdown': faculty_breakdown,
            'selected_semester_id': int(semester_id) if semester_id else None
        })

    except Exception as e:
        print(f"Error fetching attendance analytics: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/hr/update-attendance-time', methods=['POST'])
@require_auth([20003])
def api_update_attendance_time():
    """Update time in/out for attendance records with exact RFID validation rules"""
    try:
        data = request.get_json()
        updates = data.get('updates', [])
        
        print(f"📥 Received update request with {len(updates)} updates")
        
        if not updates:
            return {'success': False, 'error': 'No updates provided'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        user_id = session['user_id']
        personnel_info = get_personnel_info(user_id)
        hr_personnel_id = personnel_info.get('personnel_id')
        
        updated_count = 0
        
        for update in updates:
            name = update.get('name')
            date = update.get('date')
            class_name = update.get('class_name')
            time_in = update.get('time_in', '')  
            time_out = update.get('time_out', '')  
            
            print(f"🔧 Processing update for {name} on {date}, class: {class_name}")
            print(f"   Time-in: '{time_in}', Time-out: '{time_out}'")
            
            if name and date and class_name:
                # Name format from API is "Lastname, Firstname" or "Lastname, Firstname, Honorifics"
                name_parts = name.split(', ')
                if len(name_parts) >= 2:
                    lastname = name_parts[0].strip()
                    firstname = name_parts[1].strip()

                    print(f"   Looking for: {lastname}, {firstname}, date: {date}, class: {class_name}")
                    
                    date_obj = datetime.strptime(date, '%Y-%m-%d')
                    day_of_week = date_obj.strftime('%A')  
                    print(f"   Day of week: {day_of_week}")
                    subject_code = class_name.split(' - ')[0] if ' - ' in class_name else class_name
                    
                    cursor.execute("""
                        SELECT 
                            a.attendance_id, 
                            a.timein, 
                            a.timeout, 
                            a.attendancestatus,
                            p.personnel_id,
                            sch.class_id,
                            sch.classday_1,
                            sch.starttime_1,
                            sch.endtime_1,
                            sch.classday_2, 
                            sch.starttime_2,
                            sch.endtime_2,
                            sub.subjectcode,
                            sub.subjectname,
                            sch.classsection,
                            sch.classroom
                        FROM attendance a
                        JOIN personnel p ON a.personnel_id = p.personnel_id
                        JOIN schedule sch ON a.class_id = sch.class_id
                        JOIN subjects sub ON sch.subject_id = sub.subject_id
                        WHERE p.firstname = %s 
                        AND p.lastname = %s 
                        AND DATE(a.timein AT TIME ZONE 'Asia/Manila') = %s
                        AND sub.subjectcode = %s
                        ORDER BY 
                            CASE 
                                WHEN (sch.classday_1 = %s) THEN 1
                                WHEN (sch.classday_2 = %s) THEN 2
                                ELSE 3
                            END,
                            a.timein DESC
                    """, (firstname, lastname, date, subject_code, day_of_week, day_of_week))
                    
                    attendance_results = cursor.fetchall()
                    
                    print(f"   Found {len(attendance_results)} matching records")
                    
                    if attendance_results:
                        target_record = None
                        exact_match_found = False
                        
                        for attendance_result in attendance_results:
                            (attendance_id, current_timein, current_timeout, current_status, 
                             personnel_id, class_id, classday_1, starttime_1, endtime_1, classday_2, starttime_2, endtime_2,
                             subject_code, subject_name, class_section, classroom) = attendance_result
                            
                            day1_matches = (classday_1 == day_of_week)
                            day2_matches = (classday_2 == day_of_week)
                            
                            print(f"   Checking record {attendance_id}:")
                            print(f"     timein={current_timein}, timeout={current_timeout}")
                            print(f"     schedule: {classday_1} {starttime_1}-{endtime_1} / {classday_2} {starttime_2}-{endtime_2}")
                            print(f"     matches {day_of_week}: Day1={day1_matches}, Day2={day2_matches}")
                            
                            if day1_matches or day2_matches:
                                if day1_matches:
                                    scheduled_day = classday_1
                                    scheduled_start = starttime_1
                                    scheduled_end = endtime_1
                                    day_type = "Day1"
                                else:
                                    scheduled_day = classday_2  
                                    scheduled_start = starttime_2
                                    scheduled_end = endtime_2
                                    day_type = "Day2"
                                
                                print(f"   ✅ Exact day match found: {day_type} - {scheduled_day} {scheduled_start}-{scheduled_end}")
                                
                                target_record = attendance_result
                                exact_match_found = True
                                print(f"   ✅ Found PERFECT match on {day_type}: {attendance_id}")
                                break
                        
                        if not exact_match_found and attendance_results:
                            target_record = attendance_results[0]
                            print(f"   ⚠️ Using first record as fallback: {target_record[0]}")
                        
                        if target_record:
                            (attendance_id, current_timein, current_timeout, current_status, 
                             personnel_id, class_id, classday_1, starttime_1, endtime_1, classday_2, starttime_2, endtime_2,
                             subject_code, subject_name, class_section, classroom) = target_record
                            
                            print(f"   🎯 Updating record: ID={attendance_id}")
                            print(f"   Schedule: {classday_1} {starttime_1}-{endtime_1} / {classday_2} {starttime_2}-{endtime_2}")
                            
                            if day1_matches:
                                class_start = starttime_1
                                class_end = endtime_1
                                validation_day = classday_1
                            elif day2_matches:
                                class_start = starttime_2
                                class_end = endtime_2
                                validation_day = classday_2
                            else:
                                class_start = starttime_1 or starttime_2
                                class_end = endtime_1 or endtime_2
                                validation_day = classday_1 or classday_2
                            
                            print(f"   Using schedule for validation: {validation_day} {class_start}-{class_end}")
                            
                            # === VALIDATION LOGIC ===
                            changes_made = []
                            updates_applied = False
                            
                            original_timein = current_timein
                            original_timeout = current_timeout
                            original_status = current_status
                            
                            # Convert class times to time objects
                            if isinstance(class_start, str):
                                class_start_time = datetime.strptime(class_start[:8], '%H:%M:%S').time()
                            else:
                                class_start_time = class_start
                            
                            if isinstance(class_end, str):
                                class_end_time = datetime.strptime(class_end[:8], '%H:%M:%S').time()
                            else:
                                class_end_time = class_end
                            
                            # Define validation windows
                            class_start_dt = datetime.combine(date_obj, class_start_time)
                            class_end_dt = datetime.combine(date_obj, class_end_time)
                            
                            # Time-in window: 15 mins before start to 15 mins before end
                            timein_window_start = (class_start_dt - timedelta(minutes=15)).time()
                            timein_window_end = (class_end_dt - timedelta(minutes=15)).time()
                            
                            # Late threshold: 15 mins after start
                            late_threshold = (class_start_dt + timedelta(minutes=15)).time()
                            
                            # Time-out window: 15 mins before end to 15 mins after end
                            timeout_window_start = (class_end_dt - timedelta(minutes=15)).time()
                            timeout_window_end = (class_end_dt + timedelta(minutes=15)).time()
                            
                            print(f"   📊 Validation Windows:")
                            print(f"     Time-in: {timein_window_start} to {timein_window_end}")
                            print(f"     Late threshold: {late_threshold}")
                            print(f"     Time-out: {timeout_window_start} to {timeout_window_end}")
                            
                            # === VALIDATE & UPDATE TIME-IN (RFID logic) ===
                            validated_timein_time = None  # track for status calc
                            if 'time_in' in update:
                                if time_in == '':
                                    # Clear time-in → marks as absent
                                    midnight_time = f"{date} 00:00:00"
                                    cursor.execute("""
                                        UPDATE attendance
                                        SET timein = %s::timestamp AT TIME ZONE 'Asia/Manila'
                                        WHERE attendance_id = %s
                                    """, (midnight_time, attendance_id))
                                    changes_made.append("deleted time-in")
                                    updates_applied = True
                                else:
                                    time_in_24hr = convert_to_24hour(time_in)
                                    new_timein_time = datetime.strptime(time_in_24hr, '%H:%M:%S').time()

                                    # Validate: must be within time-in window (same as RFID)
                                    if timein_window_start <= new_timein_time <= timein_window_end:
                                        new_timein = f"{date} {time_in_24hr}"
                                        cursor.execute("""
                                            UPDATE attendance
                                            SET timein = %s::timestamp AT TIME ZONE 'Asia/Manila'
                                            WHERE attendance_id = %s
                                        """, (new_timein, attendance_id))
                                        changes_made.append(f"set time-in to {time_in}")
                                        updates_applied = True
                                        validated_timein_time = new_timein_time
                                    else:
                                        conn.rollback()
                                        cursor.close()
                                        db_pool.return_connection(conn)
                                        return {
                                            'success': False,
                                            'error': (
                                                f"Time-in {time_in} is outside the valid window "
                                                f"({timein_window_start.strftime('%H:%M')} – "
                                                f"{timein_window_end.strftime('%H:%M')}) for this class."
                                            )
                                        }

                            # === VALIDATE & UPDATE TIME-OUT (RFID logic) ===
                            if 'time_out' in update:
                                if time_out == '':
                                    cursor.execute("""
                                        UPDATE attendance
                                        SET timeout = NULL
                                        WHERE attendance_id = %s
                                    """, (attendance_id,))
                                    changes_made.append("deleted time-out")
                                    updates_applied = True
                                else:
                                    # Require a valid time-in
                                    current_timein_check = current_timein
                                    if 'time_in' in update and time_in != '':
                                        time_in_24hr = convert_to_24hour(time_in)
                                        current_timein_check = datetime.strptime(f"{date} {time_in_24hr}", "%Y-%m-%d %H:%M:%S")

                                    if current_timein_check and current_timein_check.strftime('%H:%M:%S') != '00:00:00':
                                        time_out_24hr = convert_to_24hour(time_out)
                                        new_timeout_time = datetime.strptime(time_out_24hr, '%H:%M:%S').time()

                                        # Validate: must be within time-out window (same as RFID)
                                        if timeout_window_start <= new_timeout_time <= timeout_window_end:
                                            new_timeout = f"{date} {time_out_24hr}"
                                            cursor.execute("""
                                                UPDATE attendance
                                                SET timeout = %s::timestamp AT TIME ZONE 'Asia/Manila'
                                                WHERE attendance_id = %s
                                            """, (new_timeout, attendance_id))
                                            changes_made.append(f"set time-out to {time_out}")
                                            updates_applied = True
                                        else:
                                            conn.rollback()
                                            cursor.close()
                                            db_pool.return_connection(conn)
                                            return {
                                                'success': False,
                                                'error': (
                                                    f"Time-out {time_out} is outside the valid window "
                                                    f"({timeout_window_start.strftime('%H:%M')} – "
                                                    f"{timeout_window_end.strftime('%H:%M')}) for this class."
                                                )
                                            }
                                    else:
                                        print(f"   ⚠️ Cannot add timeout - no valid timein exists")
                                        updates_applied = False

                            # === UPDATE STATUS (RFID logic: Present / Late / Absent) ===
                            if updates_applied:
                                new_status = None

                                if validated_timein_time is not None:
                                    # Time-in was just set — use RFID status logic
                                    if validated_timein_time <= late_threshold:
                                        new_status = "Present"
                                    else:
                                        new_status = "Late"
                                elif 'time_in' in update and time_in == '':
                                    # Time-in was cleared
                                    new_status = "Absent"
                                else:
                                    # Only time-out changed — re-derive status from existing time-in
                                    cursor.execute("""
                                        SELECT timein FROM attendance WHERE attendance_id = %s
                                    """, (attendance_id,))
                                    ti_row = cursor.fetchone()
                                    if ti_row and ti_row[0] and ti_row[0].strftime('%H:%M:%S') != '00:00:00':
                                        ti = ti_row[0].astimezone(pytz.timezone('Asia/Manila')).replace(tzinfo=None)
                                        if ti.time() <= late_threshold:
                                            new_status = "Present"
                                        else:
                                            new_status = "Late"
                                    else:
                                        new_status = "Absent"

                                # Write the determined status to the DB (for ALL cases above)
                                if new_status:
                                    cursor.execute("""
                                        UPDATE attendance
                                        SET attendancestatus = %s
                                        WHERE attendance_id = %s
                                    """, (new_status, attendance_id))

                                updated_count += 1

                                schedule_info = ""
                                if day1_matches and classday_1 and starttime_1 and endtime_1:
                                    start1_str = str(starttime_1)[:8] if starttime_1 else 'N/A'
                                    end1_str = str(endtime_1)[:8] if endtime_1 else 'N/A'
                                    schedule_info = f"{classday_1} {start1_str} - {end1_str}"
                                elif day2_matches and classday_2 and starttime_2 and endtime_2:
                                    start2_str = str(starttime_2)[:8] if starttime_2 else 'N/A'
                                    end2_str = str(endtime_2)[:8] if endtime_2 else 'N/A'
                                    schedule_info = f"{classday_2} {start2_str} - {end2_str}"
                                else:
                                    schedule_parts = []
                                    if classday_1 and starttime_1 and endtime_1:
                                        start1_str = str(starttime_1)[:8] if starttime_1 else 'N/A'
                                        end1_str = str(endtime_1)[:8] if endtime_1 else 'N/A'
                                        schedule_parts.append(f"{classday_1} {start1_str}-{end1_str}")
                                    if classday_2 and starttime_2 and endtime_2:
                                        start2_str = str(starttime_2)[:8] if starttime_2 else 'N/A'
                                        end2_str = str(endtime_2)[:8] if endtime_2 else 'N/A'
                                        schedule_parts.append(f"{classday_2} {start2_str}-{end2_str}")
                                    schedule_info = " / ".join(schedule_parts) if schedule_parts else "N/A"

                                def clean_value(value):
                                    if value is None or value == '' or value == '00:00:00':
                                        return 'None'
                                    return str(value)

                                before_timein_str = clean_value(original_timein.strftime('%H:%M:%S') if original_timein and original_timein.strftime('%H:%M:%S') != '00:00:00' else None)
                                before_timeout_str = clean_value(original_timeout.strftime('%H:%M:%S') if original_timeout and original_timeout.strftime('%H:%M:%S') != '00:00:00' else None)

                                after_timein_str = "None"
                                if 'time_in' in update:
                                    if time_in == '':
                                        after_timein_str = "None"
                                    else:
                                        time_in_24hr = convert_to_24hour(time_in)
                                        after_timein_str = time_in_24hr if time_in_24hr else "None"
                                else:
                                    after_timein_str = before_timein_str

                                after_timeout_str = "None"
                                if 'time_out' in update:
                                    if time_out == '':
                                        after_timeout_str = "None"
                                    else:
                                        time_out_24hr = convert_to_24hour(time_out)
                                        after_timeout_str = time_out_24hr if time_out_24hr else "None"
                                else:
                                    after_timeout_str = before_timeout_str

                                class_name_clean = class_name.replace('\n', ' ').replace('\r', ' ').strip()
                                audit_details = f"HR updated attendance for {name}\nClass: {class_name_clean}\nDate: {date}\nSchedule: {schedule_info}\nSection: {class_section}\nClassroom: {classroom}"

                                log_audit_action(
                                    hr_personnel_id,
                                    "Attendance time updated",
                                    audit_details,
                                    before_value=f"Time-in: {before_timein_str}, Time-out: {before_timeout_str}, Status: {original_status}",
                                    after_value=f"Time-in: {after_timein_str}, Time-out: {after_timeout_str}, Status: {new_status}"
                                )

                                try:
                                    cursor.execute("SELECT acadcalendar_id FROM schedule WHERE class_id = %s", (class_id,))
                                    acadcal_result = cursor.fetchone()
                                    if acadcal_result:
                                        update_attendance_report(personnel_id, class_id, acadcal_result[0], conn)
                                except Exception as e:
                                    print(f"Warning: Could not update attendance report: {e}")
                                    
                                print(f"   ✅ Successfully updated attendance_id: {attendance_id}")
                    else:
                        print(f"   ❌ No matching attendance record found!")
        
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        
        print(f"✅ Successfully processed {updated_count} attendance record updates")
        
        return {
            'success': True,
            'message': f'Successfully updated {updated_count} attendance record(s)',
            'updated_count': updated_count
        }
        
    except Exception as e:
        print(f"❌ Error updating attendance time: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/update-rfid-remarks', methods=['POST'])
@require_auth([20003])
def api_update_rfid_remarks():
    """Update remarks for multiple RFID logs"""
    try:
        data = request.get_json()
        updates = data.get('updates', [])
        
        if not updates:
            return {'success': False, 'error': 'No updates provided'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        user_id = session['user_id']
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        updated_count = 0
        
        for update in updates:
            log_id = update.get('log_id')
            remarks = update.get('remarks', '')
            
            if log_id:
                cursor.execute("SELECT remarks FROM rfidlogs WHERE log_id = %s", (log_id,))
                current_remarks_result = cursor.fetchone()
                current_remarks = current_remarks_result[0] if current_remarks_result else ""
                
                cursor.execute("""
                    UPDATE rfidlogs 
                    SET remarks = %s 
                    WHERE log_id = %s
                """, (remarks, log_id))
                updated_count += 1
                
                log_audit_action(
                    personnel_id,
                    "RFID remarks updated",
                    f"HR updated remarks for RFID log",
                    before_value=f"Remarks: {current_remarks}" if current_remarks else "Remarks: Not set",
                    after_value=f"Remarks: {remarks}" if remarks else "Remarks: Cleared"
                )
        
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        
        return {
            'success': True,
            'message': f'Successfully updated {updated_count} remark(s)',
            'updated_count': updated_count
        }
        
    except Exception as e:
        print(f"Error updating RFID remarks: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/update-biometric-remarks', methods=['POST'])
@require_auth([20003])
def api_update_biometric_remarks():
    """Update remarks for multiple biometric logs"""
    try:
        data = request.get_json()
        updates = data.get('updates', [])

        if not updates:
            return {'success': False, 'error': 'No updates provided'}

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        user_id = session['user_id']
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')

        updated_count = 0

        for update in updates:
            log_id = update.get('log_id')
            remarks = update.get('remarks', '')

            if log_id:
                cursor.execute("SELECT remarks FROM biometriclogs WHERE biometriclog_id = %s", (log_id,))
                current_remarks_result = cursor.fetchone()
                current_remarks = current_remarks_result[0] if current_remarks_result else ""

                cursor.execute("""
                    UPDATE biometriclogs
                    SET remarks = %s
                    WHERE biometriclog_id = %s
                """, (remarks, log_id))
                updated_count += 1

                log_audit_action(
                    personnel_id,
                    "Biometric remarks updated",
                    f"HR updated remarks for biometric log",
                    before_value=f"Remarks: {current_remarks}" if current_remarks else "Remarks: Not set",
                    after_value=f"Remarks: {remarks}" if remarks else "Remarks: Cleared"
                )

        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)

        return {
            'success': True,
            'message': f'Successfully updated {updated_count} remark(s)',
            'updated_count': updated_count
        }

    except Exception as e:
        print(f"Error updating biometric remarks: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/rfid-logs')
@require_auth([20003])
def api_hr_rfid_logs():
    """Get all RFID logs for HR view with milliseconds"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        semester_id = request.args.get('semester_id')
        sem_start = sem_end = None
        if semester_id:
            cursor.execute("SELECT semesterstart, semesterend FROM acadcalendar WHERE acadcalendar_id = %s", (int(semester_id),))
            row = cursor.fetchone()
            if row:
                sem_start, sem_end = row[0], row[1]

        date_filter = "WHERE rl.taptime::date BETWEEN %s AND %s" if (sem_start and sem_end) else ""
        params = [sem_start, sem_end] if (sem_start and sem_end) else []

        cursor.execute("""
            SELECT
                rl.log_id,
                rl.taptime,
                rl.personnel_id,
                rl.remarks,
                p.firstname,
                p.lastname,
                p.honorifics,
                c.collegename,
                pr.position,
                pr.employmentstatus
            FROM rfidlogs rl
            LEFT JOIN personnel p ON rl.personnel_id = p.personnel_id
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            """ + date_filter + """
            ORDER BY rl.taptime DESC
        """, params)
        
        logs = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        rfid_logs = []
        for log in logs:
            (log_id, taptime, personnel_id, remarks, firstname, lastname, honorifics,
             collegename, position, employmentstatus) = log

            if personnel_id and firstname and lastname:
                personnel_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            else:
                personnel_name = "Unknown RFID"

            if taptime:
                tap_datetime = taptime.astimezone(pytz.timezone('Asia/Manila'))
                date_str = tap_datetime.strftime('%Y-%m-%d')
                time_str = tap_datetime.strftime('%H:%M:%S.%f')[:12]
            else:
                date_str = "N/A"
                time_str = "N/A"

            rfid_logs.append({
                'log_id': log_id,
                'personnel_name': personnel_name,
                'date': date_str,
                'tap_time': time_str,
                'remarks': remarks or "",
                'college': collegename or 'N/A',
                'position': position or 'N/A',
                'employment_status': employmentstatus or 'N/A'
            })
        
        return {
            'success': True,
            'rfid_logs': rfid_logs
        }
        
    except Exception as e:
        print(f"Error fetching RFID logs: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/faculty-list')
@require_auth([20003])
def api_hr_faculty_list():
    """OPTIMIZED: Get all faculty and dean list with teaching load"""
    semester_id = request.args.get('semester_id')
    cache_key = f"hr_faculty_list_{semester_id or 'current'}"
    cached = get_cached(cache_key, ttl=300)
    if cached:
        return cached

    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        if semester_id:
            cal_cte = "SELECT %s::int AS acadcalendar_id"
            params = [int(semester_id)]
        else:
            cal_cte = """SELECT acadcalendar_id FROM acadcalendar
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC LIMIT 1"""
            params = []

        cursor.execute("""
            WITH current_calendar AS (""" + cal_cte + """)
            SELECT
                p.personnel_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                p.role_id,
                c.collegename,
                COALESCE(SUM(sub.units), 0) as total_units,
                pr.position,
                pr.employmentstatus
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            LEFT JOIN schedule sch ON p.personnel_id = sch.personnel_id
                AND sch.acadcalendar_id = (SELECT acadcalendar_id FROM current_calendar)
            LEFT JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE p.role_id IN (20001, 20002)
            GROUP BY p.personnel_id, p.firstname, p.lastname, p.honorifics, p.role_id, c.collegename, pr.position, pr.employmentstatus
            ORDER BY p.lastname, p.firstname
        """, params)
        
        faculty_records = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        faculty_list = []
        for record in faculty_records:
            personnel_id, firstname, lastname, honorifics, role_id, collegename, total_units, position, employmentstatus = record

            faculty_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"

            faculty_list.append({
                'personnel_id': personnel_id,
                'name': faculty_name,
                'college': collegename or 'N/A',
                'teaching_load': int(total_units),
                'role_id': role_id,
                'position': position or 'N/A',
                'employment_status': employmentstatus or 'N/A'
            })
        
        result = {
            'success': True,
            'faculty_list': faculty_list
        }
        
        set_cached(cache_key, result)
        return result
        
    except Exception as e:
        print(f"Error fetching faculty list: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/faculty-schedule/<int:personnel_id>')
@require_auth([20003])
def api_hr_faculty_schedule(personnel_id):
    """HR view: Get faculty teaching schedule with 15-minute grid precision"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            WITH current_calendar AS (
                SELECT acadcalendar_id, semester, acadyear
                FROM acadcalendar 
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC
                LIMIT 1
            )
            SELECT 
                cc.acadcalendar_id,
                cc.semester,
                cc.acadyear,
                sch.classday_1,
                sch.starttime_1,
                sch.endtime_1,
                sch.classday_2,
                sch.starttime_2,
                sch.endtime_2,
                sub.subjectcode,
                sch.classroom,
                sch.classsection
            FROM current_calendar cc
            LEFT JOIN schedule sch ON sch.acadcalendar_id = cc.acadcalendar_id AND sch.personnel_id = %s
            LEFT JOIN subjects sub ON sch.subject_id = sub.subject_id
        """, (personnel_id,))
        
        results = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        if not results:
            return {'success': False, 'error': 'Academic calendar not found'}
        
        acadcalendar_id = results[0][0]
        semester_name = results[0][1]
        acad_year = results[0][2]
        
        def time_to_minutes(time_val):
            """Convert time to minutes since 7:00 AM"""
            if not time_val:
                return None
            
            if isinstance(time_val, str):
                time_str = time_val[:8]
            elif hasattr(time_val, 'strftime'):
                time_str = time_val.strftime('%H:%M:%S')
            else:
                time_str = str(time_val)[:8]
            
            try:
                hours, minutes, _ = map(int, time_str.split(':'))
                return (hours - 7) * 60 + minutes
            except:
                return None
        
        def format_time_12hr(time_val):
            """Format time in 12-hour format"""
            if not time_val:
                return None
            
            if isinstance(time_val, str):
                time_str = time_val[:5]
            elif hasattr(time_val, 'strftime'):
                time_str = time_val.strftime('%H:%M')
            else:
                time_str = str(time_val)[:5]
            
            try:
                hours, minutes = map(int, time_str.split(':'))
                period = 'AM' if hours < 12 else 'PM'
                display_hour = hours if hours <= 12 else hours - 12
                display_hour = 12 if display_hour == 0 else display_hour
                return f"{display_hour}:{minutes:02d} {period}"
            except:
                return time_str
        
        schedule_classes = []
        
        for row in results:
            if not row[9]:
                continue
            
            (_, _, _, classday_1, starttime_1, endtime_1, 
             classday_2, starttime_2, endtime_2,
             subject_code, classroom, class_section) = row
            
            if classday_1 and starttime_1 and endtime_1:
                start_minutes = time_to_minutes(starttime_1)
                end_minutes = time_to_minutes(endtime_1)
                
                if start_minutes is not None and end_minutes is not None:
                    schedule_classes.append({
                        'day': classday_1,
                        'subject_code': subject_code,
                        'classroom': classroom or 'TBA',
                        'section': class_section or 'N/A',
                        'start_minutes': start_minutes,
                        'end_minutes': end_minutes,
                        'start_time': format_time_12hr(starttime_1),
                        'end_time': format_time_12hr(endtime_1),
                        'duration_minutes': end_minutes - start_minutes
                    })
            
            if classday_2 and starttime_2 and endtime_2:
                start_minutes = time_to_minutes(starttime_2)
                end_minutes = time_to_minutes(endtime_2)
                
                if start_minutes is not None and end_minutes is not None:
                    schedule_classes.append({
                        'day': classday_2,
                        'subject_code': subject_code,
                        'classroom': classroom or 'TBA',
                        'section': class_section or 'N/A',
                        'start_minutes': start_minutes,
                        'end_minutes': end_minutes,
                        'start_time': format_time_12hr(starttime_2),
                        'end_time': format_time_12hr(endtime_2),
                        'duration_minutes': end_minutes - start_minutes
                    })
        
        semester_info = {
            'id': acadcalendar_id,
            'name': semester_name,
            'year': acad_year,
            'display': f"{semester_name}, {acad_year}"
        }
        
        return {
            'success': True,
            'schedule_classes': schedule_classes,
            'semester_info': semester_info
        }
        
    except Exception as e:
        print(f"Error fetching faculty schedule for personnel {personnel_id}: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/evaluations-data')
@require_auth([20001, 20002])
def api_faculty_evaluations_data():
    """
    Fetches evaluation breakdown data for charts and comparison, INCLUDING qualitative feedback.
    (MODIFIED to include 'chart_data' for the dashboard bar chart.)
    """
    try:
        user_id = session.get('user_id')
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        if not personnel_id:
            return jsonify({'success': False, 'error': 'Personnel record not found'}), 404
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        # 1. Determine the term to use (caller may pass ?term_id=)
        term_id_param = request.args.get('term_id')
        current_term_id = None
        if term_id_param:
            try:
                current_term_id = int(term_id_param)
            except (ValueError, TypeError):
                current_term_id = None
        if not current_term_id:
            cursor.execute("""
                SELECT acadcalendar_id
                FROM acadcalendar
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC LIMIT 1
            """)
            r = cursor.fetchone()
            current_term_id = r[0] if r else 80001

        # 2. Fetch scores, comparison data, AND feedback
        cursor.execute("""
            WITH current_term AS (
                SELECT CAST(%s AS integer) AS acadcalendar_id
            ),
            faculty_scores AS (
                -- Personal scores (Average score per type)
                SELECT
                    fe.evaluator_type,
                    COALESCE(AVG(fe.score), 0) AS average_score
                FROM faculty_evaluations fe
                WHERE fe.personnel_id = %s AND fe.acadcalendar_id = (SELECT acadcalendar_id FROM current_term)
                GROUP BY fe.evaluator_type 
            ),
            -- NEW CTE: Fetch all qualitative feedback for the current term
            feedback_data AS (
                SELECT
                    evaluator_type,
                    qualitative_feedback
                FROM faculty_evaluations fe
                WHERE fe.personnel_id = %s AND fe.acadcalendar_id = (SELECT acadcalendar_id FROM current_term)
                AND fe.qualitative_feedback IS NOT NULL AND fe.qualitative_feedback != ''
            )
            SELECT 
                -- Individual Averages
                COALESCE(SUM(CASE WHEN evaluator_type = 'student' THEN average_score ELSE 0 END), 0) AS student_avg,
                COALESCE(SUM(CASE WHEN evaluator_type = 'peer' THEN average_score ELSE 0 END), 0) AS peer_avg,
                COALESCE(SUM(CASE WHEN evaluator_type = 'supervisor' THEN average_score ELSE 0 END), 0) AS supervisor_avg,
                
                -- Comparison Data 
                (SELECT c.collegename FROM personnel p JOIN college c ON p.college_id = c.college_id WHERE p.personnel_id = %s) AS faculty_college_name,
                
                (SELECT AVG(overall_score) FROM (
                    SELECT 
                        COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) +
                                 SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) +
                                 SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END), 0) AS overall_score
                        FROM personnel p
                        LEFT JOIN faculty_evaluations fe ON fe.personnel_id = p.personnel_id AND fe.acadcalendar_id = (SELECT acadcalendar_id FROM current_term)
                        WHERE p.role_id IN (20001, 20002) AND fe.score IS NOT NULL
                        GROUP BY p.personnel_id
                ) AS comparison_scores) AS college_wide_avg,
                
                (SELECT AVG(cb.overall_score) 
                 FROM (
                     SELECT 
                        fe.personnel_id,
                        p.college_id,
                        COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) +
                                 SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) +
                                 SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END), 0) AS overall_score
                     FROM faculty_evaluations fe
                     JOIN personnel p ON fe.personnel_id = p.personnel_id
                     WHERE fe.acadcalendar_id = (SELECT acadcalendar_id FROM current_term) AND p.role_id IN (20001, 20002) AND fe.score IS NOT NULL
                     GROUP BY fe.personnel_id, p.college_id
                 ) AS cb
                 WHERE cb.college_id = (SELECT college_id FROM personnel WHERE personnel_id = %s)
                ) AS department_avg,
                
                -- NEW: Aggregate all feedback data into a JSON array
                (SELECT json_agg(row_to_json(fd)) FROM feedback_data fd) AS recent_feedback
            
            FROM faculty_scores
        """, (current_term_id, personnel_id, personnel_id, personnel_id, personnel_id))

        result = cursor.fetchone()

        # 3. Fetch peer feedback (from peer_evaluation_submissions)
        cursor.execute("""
            SELECT strengths, growth, comments
            FROM peer_evaluation_submissions
            WHERE evaluatee_id = %s AND acadcalendar_id = %s
              AND (strengths IS NOT NULL OR growth IS NOT NULL OR comments IS NOT NULL)
            ORDER BY date_submitted DESC
        """, (personnel_id, current_term_id))
        peer_feedback = [
            {'strengths': r[0], 'growth': r[1], 'comments': r[2]}
            for r in cursor.fetchall()
        ]

        cursor.close()
        db_pool.return_connection(conn)

        if not result or result[0] is None:
            return jsonify({'success': False, 'error': 'No personnel or active term found.'}), 404
        
        # Unpack 7 fields now
        (student_avg, peer_avg, supervisor_avg, faculty_college_name, 
         college_wide_avg, department_avg, recent_feedback) = result 
        
        
        # Calculate scores and ensure they are floats
        student_avg = float(student_avg) if student_avg is not None else 0.0
        peer_avg = float(peer_avg) if peer_avg is not None else 0.0
        supervisor_avg = float(supervisor_avg) if supervisor_avg is not None else 0.0

        # Calculate Overall Weighted Average (55% Student, 35% Supervisor, 10% Peer)
        your_overall_avg = (student_avg * 0.55) + (supervisor_avg * 0.35) + (peer_avg * 0.10)
        
        # Prepare comparison scores
        dept_avg = float(department_avg) if department_avg is not None else your_overall_avg
        college_avg = float(college_wide_avg) if college_wide_avg is not None else your_overall_avg
        
        # NEW: Prepare data array for the Dashboard Bar Chart
        chart_data_for_dashboard = [
            student_avg, 
            peer_avg, 
            supervisor_avg
        ]


        return jsonify({
            'success': True,
            'current_term_id': current_term_id,
            'kpis': { 
                'overall_average': your_overall_avg,
                'student_score': student_avg,
                'peer_score': peer_avg,
                'supervisor_score': supervisor_avg
            },
            'breakdown_chart': { 
                'labels': ['Student (55%)', 'Supervisor (35%)', 'Peer (10%)'],
                # Using weighted components for the chart slice sizes
                'data': [student_avg * 0.55, supervisor_avg * 0.35, peer_avg * 0.10]
            },
            'comparison': {
                'your_avg': your_overall_avg,
                'dept_avg': dept_avg,
                'college_avg': college_avg,
                'college_name': faculty_college_name
            },
            'recent_feedback': recent_feedback or [],
            'peer_feedback': peer_feedback,
            'chart_data': chart_data_for_dashboard
        })
        
    except Exception as e:
        print(f"Error fetching faculty evaluation data: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/hr/employees-list')
@require_auth([20003])
def api_hr_employees_list():
    """OPTIMIZED: Get all employees data for HR directory"""
    # cache_key = "hr_employees_list"
    # cached = get_cached(cache_key, ttl=300)
    # if cached:
    #     return cached
    
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                p.personnel_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                p.employee_no,
                p.phone,
                c.collegename,
                r.rolename,
                pr.position,
                pr.employmentstatus,
                u.email
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN roles r ON p.role_id = r.role_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            LEFT JOIN users u ON p.user_id = u.user_id
            ORDER BY p.lastname, p.firstname
        """)
        
        employees = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        employees_list = []
        for emp in employees:
            (personnel_id, firstname, lastname, honorifics, employee_no, 
             phone, collegename, rolename, position, employmentstatus, email) = emp
            
            full_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            
            phone_formatted = str(phone) if phone else "N/A"
            if phone_formatted.startswith('+63 ') and len(phone_formatted) > 4:
                digits = phone_formatted[4:]
                if len(digits) == 10:
                    phone_formatted = f"+63 {digits[:3]} {digits[3:6]} {digits[6:]}"
            
            role_display = rolename or 'N/A'
            if role_display == 'hrmd':
                role_display = 'HR'
            elif role_display == 'faculty':
                role_display = 'Faculty'
            elif role_display == 'dean':
                role_display = 'Dean'
            elif role_display == 'vppres':
                role_display = 'VP/Pres'
            
            employees_list.append({
                'personnel_id': personnel_id,
                'name': full_name,
                'college': collegename or 'N/A',
                'role': role_display,
                'position': position or 'N/A',
                'status': employmentstatus or 'N/A',
                'email': email or 'N/A',
                'phone': phone_formatted,
                'employee_no': employee_no or 'N/A'
            })
        
        result = {
            'success': True,
            'employees': employees_list
        }
        
        # set_cached(cache_key, result)
        return result
        
    except Exception as e:
        print(f"Error fetching employees list: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/faculty-degree-info/<int:personnel_id>')
@require_auth([20003])
def api_hr_faculty_degree_info(personnel_id):
    """Get current academic degree flags for a faculty member."""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT has_aligned_master, has_doctorate, highest_degree_level, probationary_start_date
            FROM profile
            WHERE personnel_id = %s
        """, (personnel_id,))
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)

        if not result:
            return jsonify({'success': False, 'error': 'Profile not found'}), 404

        has_aligned_master, has_doctorate, highest_degree_level, probationary_start_date = result
        return jsonify({
            'success': True,
            'has_aligned_master': bool(has_aligned_master),
            'has_doctorate': bool(has_doctorate),
            'highest_degree_level': highest_degree_level or '',
            'probationary_start_date': probationary_start_date.isoformat() if probationary_start_date else ''
        })
    except Exception as e:
        print(f"Error fetching degree info: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hr/update-degree-info', methods=['POST'])
@require_auth([20003])
def api_hr_update_degree_info():
    """Update academic degree flags for a faculty member and set probationary_start_date."""
    try:
        data = request.get_json()
        personnel_id = data.get('personnel_id')
        has_aligned_master = bool(data.get('has_aligned_master', False))
        has_doctorate = bool(data.get('has_doctorate', False))
        highest_degree_level = data.get('highest_degree_level', '')
        probationary_start_date = data.get('probationary_start_date') or None

        if not personnel_id:
            return jsonify({'success': False, 'error': 'personnel_id is required'}), 400

        if has_aligned_master and not probationary_start_date:
            return jsonify({'success': False, 'error': 'Date Master\'s Degree was obtained is required when Aligned Master\'s is checked'}), 400

        if not has_aligned_master:
            probationary_start_date = None

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT has_aligned_master, has_doctorate, highest_degree_level, probationary_start_date
            FROM profile WHERE personnel_id = %s
        """, (personnel_id,))
        before = cursor.fetchone()
        before_value = (
            f"aligned_master={before[0]}, doctorate={before[1]}, "
            f"degree_level={before[2]}, prob_start={before[3]}"
        ) if before else "N/A"

        cursor.execute("""
            UPDATE profile
            SET has_aligned_master = %s,
                has_doctorate = %s,
                highest_degree_level = %s,
                probationary_start_date = %s
            WHERE personnel_id = %s
        """, (has_aligned_master, has_doctorate, highest_degree_level, probationary_start_date, personnel_id))
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)

        after_value = (
            f"aligned_master={has_aligned_master}, doctorate={has_doctorate}, "
            f"degree_level={highest_degree_level}, prob_start={probationary_start_date}"
        )

        hr_personnel_info = get_personnel_info(session['user_id'])
        hr_personnel_id = hr_personnel_info.get('personnel_id')
        log_audit_action(
            hr_personnel_id,
            "Degree info updated",
            f"HR updated academic degree info for personnel_id {personnel_id}",
            before_value=before_value,
            after_value=after_value
        )

        return jsonify({'success': True, 'message': 'Degree information updated successfully'})

    except Exception as e:
        print(f"Error updating degree info: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hr/add-employee', methods=['POST'])
@require_auth([20003])
def api_hr_add_employee():
    """Add new employee with user account, personnel, and profile records"""
    try:
        data = request.get_json()
        
        email = data.get('email')
        password = data.get('password')
        employee_no = data.get('employee_no')  
        firstname = data.get('firstname')
        lastname = data.get('lastname')
        honorifics = data.get('honorifics')
        phone = data.get('phone')
        hire_date = data.get('hire_date')
        college_id = data.get('college_id')
        role_id = data.get('role_id')
        employment_status = data.get('employment_status')
        position = data.get('position')
        
        if not all([email, password, employee_no, firstname, lastname, hire_date, college_id, role_id, employment_status, position, phone]):
            return {'success': False, 'error': 'All required fields must be filled'}
        
        import re
        phone_pattern = r'^\+63\s[0-9]{3}\s[0-9]{3}\s[0-9]{4}$'
        if not re.match(phone_pattern, phone):
            return {'success': False, 'error': 'Phone number must be in format: +63 912 345 6789'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT user_id FROM users WHERE email = %s", (email,))
        if cursor.fetchone():
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Email already exists'}
        
        cursor.execute("SELECT personnel_id FROM personnel WHERE employee_no = %s", (employee_no,))
        if cursor.fetchone():
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Employee number already exists'}
        
        cursor.execute("SELECT COALESCE(MAX(user_id), 10000) FROM users")
        max_user_id = cursor.fetchone()[0]
        new_user_id = max_user_id + 1
        
        cursor.execute("SELECT COALESCE(MAX(personnel_id), 30000) FROM personnel")
        max_personnel_id = cursor.fetchone()[0]
        new_personnel_id = max_personnel_id + 1
        
        cursor.execute("SELECT COALESCE(MAX(profile_id), 90000) FROM profile")
        max_profile_id = cursor.fetchone()[0]
        new_profile_id = max_profile_id + 1
        
        cursor.execute("""
            INSERT INTO users (user_id, email, password, role_id, lastlogin, lastlogout)
            VALUES (%s, %s, %s, %s, NULL, NULL)
        """, (new_user_id, email, password, role_id))
        
        cursor.execute("""
            INSERT INTO personnel (personnel_id, employee_no, firstname, lastname, phone, hiredate, college_id, user_id, role_id, honorifics)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (new_personnel_id, employee_no, firstname, lastname, phone, hire_date, college_id, new_user_id, role_id, honorifics))
        
        cursor.execute("""
            INSERT INTO profile (profile_id, personnel_id, bio, employmentstatus, position, profilepic, 
                               licenses, degrees, certificates, publications, awards,
                               licensesname, degreesname, certificatesname, publicationsname, awardsname)
            VALUES (%s, %s, '', %s, %s, NULL, 
                    ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[],
                    ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[])
        """, (new_profile_id, new_personnel_id, employment_status, position))
        
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        
        hr_user_id = session['user_id']
        hr_personnel_info = get_personnel_info(hr_user_id)
        hr_personnel_id = hr_personnel_info.get('personnel_id')

        cursor = conn.cursor()
        cursor.execute("SELECT collegename FROM college WHERE college_id = %s", (college_id,))
        college_result = cursor.fetchone()
        college_name = college_result[0] if college_result else "Unknown College"
        
        cursor.execute("SELECT rolename FROM roles WHERE role_id = %s", (role_id,))
        role_result = cursor.fetchone()
        role_name = role_result[0] if role_result else "Unknown Role"
        
        cursor.close()
        
        employee_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
        audit_details = f"HR added new employee: {employee_name}\nEmail: {email}\nRole: {role_name}\nEmployee Number: {employee_no}\nCollege: {college_name}\nEmployment Status: {employment_status}\nPosition: {position}"
        
        log_audit_action(
            hr_personnel_id,
            "Employee added",
            audit_details
        )
        
        return {'success': True, 'message': 'Employee added successfully'}
        
    except Exception as e:
        print(f"Error adding employee: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/delete-employee', methods=['POST'])
@require_auth([20003])
def api_hr_delete_employee():
    """Delete employee and all related data except audit logs and RFID logs"""
    try:
        data = request.get_json()
        personnel_id = data.get('personnel_id')
        employee_name = data.get('employee_name', 'Unknown Employee')
        
        if not personnel_id:
            return {'success': False, 'error': 'Personnel ID is required'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM personnel WHERE personnel_id = %s", (personnel_id,))
        user_result = cursor.fetchone()
        
        if not user_result:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Employee not found'}
        
        user_id = user_result[0]
        cursor.execute("BEGIN")
        cursor.execute("DELETE FROM profile WHERE personnel_id = %s", (personnel_id,))
        cursor.execute("DELETE FROM schedule WHERE personnel_id = %s", (personnel_id,))
        cursor.execute("DELETE FROM personnel WHERE personnel_id = %s", (personnel_id,))
        cursor.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
        conn.commit()
        hr_user_id = session['user_id']
        hr_personnel_info = get_personnel_info(hr_user_id)
        hr_personnel_id = hr_personnel_info.get('personnel_id')
        
        log_audit_action(
            hr_personnel_id,
            "Employee deleted",
            f"HR deleted employee: {employee_name} (Personnel ID: {personnel_id})",
            before_value=f"Employee existed in system",
            after_value="Employee records deleted from users, personnel, profile, and schedule tables"
        )
        
        cursor.close()
        db_pool.return_connection(conn)
        
        return {
            'success': True, 
            'message': f'Employee {employee_name} deleted successfully'
        }
        
    except Exception as e:
        print(f"Error deleting employee: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/subjects-list')
@require_auth([20003])
def api_hr_subjects_list():
    """Get all subjects for schedule dropdown"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT subject_id, subjectcode, subjectname, units 
            FROM subjects 
            ORDER BY subjectcode
        """)
        
        subjects = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        subjects_list = []
        for subject in subjects:
            subject_id, subjectcode, subjectname, units = subject
            subjects_list.append({
                'subject_id': subject_id,
                'subjectcode': subjectcode,
                'subjectname': subjectname,
                'units': units
            })
        
        return {
            'success': True,
            'subjects': subjects_list
        }
        
    except Exception as e:
        print(f"Error fetching subjects list: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/subjects-by-faculty/<int:personnel_id>')
@require_auth([20003])
def api_hr_subjects_by_faculty(personnel_id):
    """Get subjects filtered by faculty's college"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT college_id FROM personnel WHERE personnel_id = %s", (personnel_id,))
        faculty_college = cursor.fetchone()
        
        if not faculty_college or not faculty_college[0]:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Faculty college not found'}
        
        college_id = faculty_college[0]
        
        cursor.execute("""
            SELECT subject_id, subjectcode, subjectname, units 
            FROM subjects 
            WHERE college_id = %s
            ORDER BY subjectcode
        """, (college_id,))
        
        subjects = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        subjects_list = []
        for subject in subjects:
            subject_id, subjectcode, subjectname, units = subject
            subjects_list.append({
                'subject_id': subject_id,
                'subjectcode': subjectcode,
                'subjectname': subjectname,
                'units': units
            })
        
        return {
            'success': True,
            'subjects': subjects_list
        }
        
    except Exception as e:
        print(f"Error fetching subjects by faculty: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}
    

@app.route('/api/hr/acadcalendar')
@require_auth([20003])
def api_hr_get_acadcalendar():
    """Get all academic calendar records."""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                acadcalendar_id, 
                semester, 
                acadyear, 
                semesterstart, 
                semesterend,
                CASE WHEN CURRENT_DATE BETWEEN semesterstart AND semesterend THEN TRUE ELSE FALSE END as is_current
            FROM acadcalendar 
            ORDER BY acadyear DESC, semesterstart DESC
        """)
        
        records = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        calendar_list = []
        for rec in records:
            (id, semester, year, start, end, is_current) = rec
            
            # Format semester display
            semester_display = f"{semester}, AY {year}"
            
            calendar_list.append({
                'id': id,
                'semester': semester,
                'year': year,
                'start_date': start.strftime('%Y-%m-%d'),
                'end_date': end.strftime('%Y-%m-%d'),
                'is_current': is_current,
                'display': semester_display
            })
        
        return {'success': True, 'calendar_records': calendar_list}
        
    except Exception as e:
        print(f"Error fetching acad calendar records: {e}")
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/acadcalendar', methods=['POST'])
@require_auth([20003])
def api_hr_add_acadcalendar():
    """Add a new academic calendar record."""
    try:
        data = request.get_json()
        semester = data.get('semester')
        acadyear = data.get('acadyear')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        
        if not all([semester, acadyear, start_date, end_date]):
            return {'success': False, 'error': 'All fields are required.'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COALESCE(MAX(acadcalendar_id), 80000) FROM acadcalendar")
        new_id = cursor.fetchone()[0] + 1
        
        cursor.execute("""
            INSERT INTO acadcalendar (acadcalendar_id, semester, acadyear, semesterstart, semesterend)
            VALUES (%s, %s, %s, %s, %s)
        """, (new_id, semester, acadyear, start_date, end_date))
        
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)

        # Log the action
        hr_personnel_info = get_personnel_info(session['user_id'])
        log_audit_action(
            hr_personnel_info.get('personnel_id'),
            "Academic Calendar Added",
            f"Added new term: {semester}, AY {acadyear} ({start_date} to {end_date})",
            after_value=f"New ID: {new_id}"
        )
        
        # Clear semester cache for faculty dropdowns
        with _cache_lock:
            _cache.pop("all_semesters", None)

        return {'success': True, 'message': 'Academic calendar record added successfully.', 'new_id': new_id}
        
    except Exception as e:
        print(f"Error adding acad calendar record: {e}")
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/acadcalendar/<int:id>', methods=['PUT'])
@require_auth([20003])
def api_hr_update_acadcalendar(id):
    """Update an existing academic calendar record."""
    try:
        data = request.get_json()
        semester = data.get('semester')
        acadyear = data.get('acadyear')
        start_date = data.get('start_date')
        end_date = data.get('end_date')

        if not all([semester, acadyear, start_date, end_date]):
            return {'success': False, 'error': 'All fields are required.'}

        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        # Fetch current data for logging
        cursor.execute("""
            SELECT semester, acadyear, semesterstart, semesterend
            FROM acadcalendar WHERE acadcalendar_id = %s
        """, (id,))
        current_data = cursor.fetchone()
        
        if not current_data:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Record not found.'}
        
        current_semester, current_year, current_start, current_end = current_data

        cursor.execute("""
            UPDATE acadcalendar 
            SET semester = %s, acadyear = %s, semesterstart = %s, semesterend = %s
            WHERE acadcalendar_id = %s
        """, (semester, acadyear, start_date, end_date, id))
        
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)

        # Log the action
        hr_personnel_info = get_personnel_info(session['user_id'])
        log_audit_action(
            hr_personnel_info.get('personnel_id'),
            "Academic Calendar Updated",
            f"Updated term ID {id}: {current_semester}, AY {current_year} to {semester}, AY {acadyear}",
            before_value=f"Start: {current_start}, End: {current_end}",
            after_value=f"Start: {start_date}, End: {end_date}"
        )
        
        # Clear semester cache for faculty dropdowns
        with _cache_lock:
            _cache.pop("all_semesters", None)

        return {'success': True, 'message': 'Academic calendar record updated successfully.'}
        
    except Exception as e:
        print(f"Error updating acad calendar record: {e}")
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/acadcalendar/<int:id>', methods=['DELETE'])
@require_auth([20003])
def api_hr_delete_acadcalendar(id):
    """Delete an academic calendar record."""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        # Check for dependencies before deleting
        cursor.execute("SELECT COUNT(*) FROM schedule WHERE acadcalendar_id = %s", (id,))
        if cursor.fetchone()[0] > 0:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Cannot delete. Schedule records are linked to this term.'}

        cursor.execute("SELECT semester, acadyear FROM acadcalendar WHERE acadcalendar_id = %s", (id,))
        term_info = cursor.fetchone()

        cursor.execute("DELETE FROM acadcalendar WHERE acadcalendar_id = %s", (id,))
        deleted_count = cursor.rowcount
        
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)

        if deleted_count > 0:
            hr_personnel_info = get_personnel_info(session['user_id'])
            log_audit_action(
                hr_personnel_info.get('personnel_id'),
                "Academic Calendar Deleted",
                f"Deleted term ID {id}: {term_info[0]}, AY {term_info[1]}"
            )
            
            # Clear semester cache for faculty dropdowns
            with _cache_lock:
                _cache.pop("all_semesters", None)

            return {'success': True, 'message': 'Academic calendar record deleted successfully.'}
        else:
            return {'success': False, 'error': 'Record not found.'}
        
    except Exception as e:
        print(f"Error deleting acad calendar record: {e}")
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return {'success': False, 'error': str(e)}


def check_internal_schedule_conflicts(schedule_data):
    """Check for conflicts within the same schedule (Day 1 vs Day 2)"""
    classday_1 = schedule_data.get('classday_1')
    starttime_1 = schedule_data.get('starttime_1')
    endtime_1 = schedule_data.get('endtime_1')
    classday_2 = schedule_data.get('classday_2')
    starttime_2 = schedule_data.get('starttime_2')
    endtime_2 = schedule_data.get('endtime_2')
    
    if (classday_1 and classday_2 and classday_1 == classday_2 and
        starttime_1 and endtime_1 and starttime_2 and endtime_2):
        
        def time_to_minutes(time_str):
            """Convert time string to minutes since midnight"""
            if not time_str:
                return 0
            try:
                time_part = time_str.split('+')[0] 
                hours, minutes, seconds = map(int, time_part.split(':'))
                return hours * 60 + minutes
            except:
                return 0
        
        start1 = time_to_minutes(starttime_1)
        end1 = time_to_minutes(endtime_1)
        start2 = time_to_minutes(starttime_2)
        end2 = time_to_minutes(endtime_2)
        
        if (start1 < end2 and end1 > start2) or (start2 < end1 and end2 > start1):
            def format_time_ampm(time_str):
                """Format time in 12-hour format"""
                if not time_str:
                    return "N/A"
                try:
                    time_part = time_str.split('+')[0]
                    hours, minutes, seconds = map(int, time_part.split(':'))
                    period = 'AM' if hours < 12 else 'PM'
                    display_hour = hours if hours <= 12 else hours - 12
                    if display_hour == 0:
                        display_hour = 12
                    return f"{display_hour}:{minutes:02d} {period}"
                except:
                    return time_str
            
            start1_display = format_time_ampm(starttime_1)
            end1_display = format_time_ampm(endtime_1)
            start2_display = format_time_ampm(starttime_2)
            end2_display = format_time_ampm(endtime_2)
            
            return {
                'success': False,
                'error': f"❌ INTERNAL SCHEDULE CONFLICT DETECTED:\n\nBoth Day 1 and Day 2 are on {classday_1} with overlapping times:\n   📅 Day 1: {start1_display} - {end1_display}\n   📅 Day 2: {start2_display} - {end2_display}\n\nPlease choose different days or non-overlapping time slots."
            }
    
    return {'success': True}

@app.route('/api/hr/check-schedule-conflicts', methods=['POST'])
@require_auth([20003])
def api_hr_check_schedule_conflicts():
    """Check for schedule conflicts before adding new schedule"""
    try:
        data = request.get_json()
        
        internal_conflict = check_internal_schedule_conflicts(data)
        if not internal_conflict['success']:
            return internal_conflict
        
        semester_id = data.get('semester_id')
        personnel_id = data.get('personnel_id')
        classday_1 = data.get('classday_1')
        starttime_1 = data.get('starttime_1')
        endtime_1 = data.get('endtime_1')
        classday_2 = data.get('classday_2')
        starttime_2 = data.get('starttime_2')
        endtime_2 = data.get('endtime_2')
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        def format_time_ampm(time_str):
            if not time_str:
                return "N/A"
            try:
                time_part = time_str.split('+')[0]  
                hours, minutes, seconds = map(int, time_part.split(':'))
                period = 'AM' if hours < 12 else 'PM'
                display_hour = hours if hours <= 12 else hours - 12
                if display_hour == 0:
                    display_hour = 12
                return f"{display_hour}:{minutes:02d} {period}"
            except:
                return time_str
        
        conflicts = []
        
        if classday_1 and starttime_1 and endtime_1:
            cursor.execute("""
                SELECT s.class_id, sub.subjectcode, sub.subjectname, s.classday_1, s.starttime_1, s.endtime_1, 
                       s.classday_2, s.starttime_2, s.endtime_2, s.classsection, s.classroom
                FROM schedule s
                JOIN subjects sub ON s.subject_id = sub.subject_id
                WHERE s.personnel_id = %s 
                AND s.acadcalendar_id = %s
                AND s.classday_1 = %s
                AND (
                    (s.starttime_1 < %s AND s.endtime_1 > %s) OR
                    (s.starttime_1 < %s AND s.endtime_1 > %s) OR
                    (%s < s.starttime_1 AND %s > s.endtime_1) OR
                    (s.starttime_1 = %s AND s.endtime_1 = %s)
                )
            """, (personnel_id, semester_id, classday_1, starttime_1, starttime_1, endtime_1, endtime_1, starttime_1, endtime_1, starttime_1, endtime_1))
            
            day1_conflicts = cursor.fetchall()
            for conflict in day1_conflicts:
                start_time_formatted = format_time_ampm(str(conflict[4]))
                end_time_formatted = format_time_ampm(str(conflict[5]))
                conflicts.append(f"🚫 CONFLICT with {conflict[1]} - {conflict[2]}\n   📅 Day: {conflict[3]}\n   ⏰ Time: {start_time_formatted} - {end_time_formatted}\n   🏫 Room: {conflict[10]}\n   👥 Section: {conflict[9]}")
        
        if classday_1 and starttime_1 and endtime_1:
            cursor.execute("""
                SELECT s.class_id, sub.subjectcode, sub.subjectname, s.classday_1, s.starttime_1, s.endtime_1, 
                       s.classday_2, s.starttime_2, s.endtime_2, s.classsection, s.classroom
                FROM schedule s
                JOIN subjects sub ON s.subject_id = sub.subject_id
                WHERE s.personnel_id = %s 
                AND s.acadcalendar_id = %s
                AND s.classday_2 = %s
                AND (
                    (s.starttime_2 < %s AND s.endtime_2 > %s) OR
                    (s.starttime_2 < %s AND s.endtime_2 > %s) OR
                    (%s < s.starttime_2 AND %s > s.endtime_2) OR
                    (s.starttime_2 = %s AND s.endtime_2 = %s)
                )
            """, (personnel_id, semester_id, classday_1, starttime_1, starttime_1, endtime_1, endtime_1, starttime_1, endtime_1, starttime_1, endtime_1))
            
            day1_day2_conflicts = cursor.fetchall()
            for conflict in day1_day2_conflicts:
                start_time_formatted = format_time_ampm(str(conflict[7]))
                end_time_formatted = format_time_ampm(str(conflict[8]))
                conflict_info = f"🚫 CONFLICT with {conflict[1]} - {conflict[2]}\n   📅 Day: {conflict[6]}\n   ⏰ Time: {start_time_formatted} - {end_time_formatted}\n   🏫 Room: {conflict[10]}\n   👥 Section: {conflict[9]}"
                if conflict_info not in [c.split('\n')[0] for c in conflicts]:  # Check if already added
                    conflicts.append(conflict_info)
        
        if classday_2 and starttime_2 and endtime_2:
            cursor.execute("""
                SELECT s.class_id, sub.subjectcode, sub.subjectname, s.classday_1, s.starttime_1, s.endtime_1, 
                       s.classday_2, s.starttime_2, s.endtime_2, s.classsection, s.classroom
                FROM schedule s
                JOIN subjects sub ON s.subject_id = sub.subject_id
                WHERE s.personnel_id = %s 
                AND s.acadcalendar_id = %s
                AND (
                    (s.classday_1 = %s AND (
                        (s.starttime_1 < %s AND s.endtime_1 > %s) OR
                        (s.starttime_1 < %s AND s.endtime_1 > %s) OR
                        (%s < s.starttime_1 AND %s > s.endtime_1) OR
                        (s.starttime_1 = %s AND s.endtime_1 = %s)
                    )) OR
                    (s.classday_2 = %s AND (
                        (s.starttime_2 < %s AND s.endtime_2 > %s) OR
                        (s.starttime_2 < %s AND s.endtime_2 > %s) OR
                        (%s < s.starttime_2 AND %s > s.endtime_2) OR
                        (s.starttime_2 = %s AND s.endtime_2 = %s)
                    ))
                )
            """, (personnel_id, semester_id, classday_2, starttime_2, starttime_2, endtime_2, endtime_2, starttime_2, endtime_2, starttime_2, endtime_2,
                  classday_2, starttime_2, starttime_2, endtime_2, endtime_2, starttime_2, endtime_2, starttime_2, endtime_2))
            
            day2_conflicts = cursor.fetchall()
            for conflict in day2_conflicts:
                if conflict[6]:  
                    start_time_formatted = format_time_ampm(str(conflict[7]))
                    end_time_formatted = format_time_ampm(str(conflict[8]))
                    conflict_info = f"🚫 CONFLICT with {conflict[1]} - {conflict[2]}\n   📅 Day: {conflict[6]}\n   ⏰ Time: {start_time_formatted} - {end_time_formatted}\n   🏫 Room: {conflict[10]}\n   👥 Section: {conflict[9]}"
                else:
                    start_time_formatted = format_time_ampm(str(conflict[4]))
                    end_time_formatted = format_time_ampm(str(conflict[5]))
                    conflict_info = f"🚫 CONFLICT with {conflict[1]} - {conflict[2]}\n   📅 Day: {conflict[3]}\n   ⏰ Time: {start_time_formatted} - {end_time_formatted}\n   🏫 Room: {conflict[10]}\n   👥 Section: {conflict[9]}"
                
                if conflict_info not in [c.split('\n')[0] for c in conflicts]:
                    conflicts.append(conflict_info)
        
        cursor.close()
        db_pool.return_connection(conn)
        
        if conflicts:
            conflict_message = "❌ SCHEDULE CONFLICTS DETECTED:\n\n" + "\n\n".join(conflicts) + "\n\nPlease choose a different time slot."
            return {
                'success': False,
                'error': conflict_message
            }
        else:
            return {'success': True, 'message': 'No schedule conflicts found'}
        
    except Exception as e:
        print(f"Error checking schedule conflicts: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': f'Error checking schedule conflicts: {str(e)}'}

@app.route('/api/hr/add-schedule', methods=['POST'])
@require_auth([20003])
def api_hr_add_schedule():
    """Add new schedule"""
    try:
        data = request.get_json()
        
        semester_id = data.get('semester_id')
        personnel_id = data.get('personnel_id')
        subject_id = data.get('subject_id')
        units = data.get('units')
        classday_1 = data.get('classday_1')
        starttime_1 = data.get('starttime_1')
        endtime_1 = data.get('endtime_1')
        classday_2 = data.get('classday_2')
        starttime_2 = data.get('starttime_2')
        endtime_2 = data.get('endtime_2')
        classroom = data.get('classroom')
        classsection = data.get('classsection')
        student_count = data.get('student_count')

        required_fields = ['semester_id', 'personnel_id', 'subject_id', 'units',
                          'classday_1', 'starttime_1', 'endtime_1', 'classroom', 'classsection', 'student_count']
        
        for field in required_fields:
            if not data.get(field):
                return {'success': False, 'error': f'All required fields must be filled. Missing: {field}'}
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        conflict_check = api_hr_check_schedule_conflicts()
        if not conflict_check.get('success'):
            return conflict_check
        
        cursor.execute("SELECT COALESCE(MAX(class_id), 60000) FROM schedule")
        max_class_id = cursor.fetchone()[0]
        new_class_id = max_class_id + 1
        
        cursor.execute("SELECT acadcalendar_id FROM acadcalendar WHERE acadcalendar_id = %s", (semester_id,))
        semester_info = cursor.fetchone()

        if not semester_info:
            cursor.close()
            db_pool.return_connection(conn)
            return {'success': False, 'error': 'Invalid semester selected'}

        cursor.execute("""
            INSERT INTO schedule (
                class_id, personnel_id, subject_id,
                classday_1, starttime_1, endtime_1,
                classday_2, starttime_2, endtime_2,
                classroom, acadcalendar_id, classsection, student_count
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            new_class_id, personnel_id, subject_id,
            classday_1, starttime_1, endtime_1,
            classday_2, starttime_2, endtime_2,
            classroom, semester_id, classsection, student_count
        ))

        create_initial_attendance_report(personnel_id, new_class_id, semester_id, conn)

        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        
        hr_user_id = session['user_id']
        hr_personnel_info = get_personnel_info(hr_user_id)
        hr_personnel_id = hr_personnel_info.get('personnel_id')
        
        cursor = conn.cursor()
        cursor.execute("SELECT firstname, lastname, honorifics FROM personnel WHERE personnel_id = %s", (personnel_id,))
        faculty_info = cursor.fetchone()
        faculty_name = f"{faculty_info[0]} {faculty_info[1]}, {faculty_info[2]}" if faculty_info and faculty_info[2] else f"{faculty_info[0]} {faculty_info[1]}" if faculty_info else "Unknown"
        
        cursor.execute("SELECT subjectcode, subjectname, units FROM subjects WHERE subject_id = %s", (subject_id,))
        subject_info = cursor.fetchone()
        subject_name = f"{subject_info[0]} - {subject_info[1]}" if subject_info else "Unknown"
        subject_units = subject_info[2] if subject_info else units
        
        cursor.close()
        
        schedule_info = f"{classday_1} {starttime_1}-{endtime_1}"
        if classday_2 and starttime_2 and endtime_2:
            schedule_info += f" & {classday_2} {starttime_2}-{endtime_2}"
        
        audit_details = f"HR added new schedule for {faculty_name}\nSubject: {subject_name}\nUnits: {subject_units}\nSchedule: {schedule_info}\nSection: {classsection}"
        
        log_audit_action(
            hr_personnel_id,
            "Schedule added",
            audit_details
        )
        
        return {'success': True, 'message': 'Schedule added successfully'}
        
    except Exception as e:
        print(f"Error adding schedule: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/import-schedule-csv/template', methods=['GET'])
@require_auth([20003])
def api_hr_download_schedule_template():
    """Return a per-faculty CSV template for schedule import"""
    import csv as csv_module
    import io
    output = io.StringIO()
    writer = csv_module.writer(output)
    # Faculty header section
    writer.writerow(['Last Name', 'Dela Cruz'])
    writer.writerow(['First Name', 'Juan'])
    writer.writerow([])   # blank separator
    # Schedule table
    writer.writerow(['Subject Code', 'Day 1', 'Start Time 1', 'End Time 1',
                     'Day 2', 'Start Time 2', 'End Time 2', 'Room', 'Section', 'Student Count'])
    writer.writerow(['ELS 100', 'Monday',    '07:30', '09:00', 'Wednesday', '07:30', '09:00', 'LR1',  '51001', '35'])
    writer.writerow(['ELS 102', 'Tuesday',   '10:00', '13:00', '',          '',      '',      'LR2',  '51002', '28'])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=schedule_import_template.csv'}
    )


@app.route('/api/hr/validate-schedule-csv', methods=['POST'])
@require_auth([20003])
def api_hr_validate_schedule_csv():
    """
    Validate a per-faculty CSV for schedule import.
    Format:
        Last Name,<value>
        First Name,<value>
        (blank row)
        Subject Code,Day 1,Start Time 1,End Time 1,Day 2,Start Time 2,End Time 2,Room,Section
        <data rows...>
    """
    import csv as csv_module
    import io as io_module
    conn = None
    try:
        semester_id = request.form.get('semester_id')
        file = request.files.get('csv_file')

        if not semester_id:
            return jsonify({'success': False, 'error': 'Semester is required'})
        if not file or file.filename == '':
            return jsonify({'success': False, 'error': 'No CSV file uploaded'})
        if not file.filename.lower().endswith('.csv'):
            return jsonify({'success': False, 'error': 'File must be a .csv file'})

        content = file.read().decode('utf-8-sig')
        all_raw = list(csv_module.reader(io_module.StringIO(content)))

        # ── Parse faculty header (Last Name / First Name) ──────────────────
        last_name = first_name = ''
        header_row_index = None  # index of the "Subject Code,..." row

        SCHED_HEADER = ['subject code', 'day 1', 'start time 1', 'end time 1']
        # docstring reference: full header is Subject Code,Day 1,Start Time 1,End Time 1,Day 2,Start Time 2,End Time 2,Room,Section,Student Count

        for i, row in enumerate(all_raw):
            if not row:
                continue
            first_cell = row[0].strip().lower().rstrip(':')
            if first_cell == 'last name' and len(row) >= 2:
                last_name = row[1].strip()
            elif first_cell == 'first name' and len(row) >= 2:
                first_name = row[1].strip()
            elif [c.strip().lower() for c in row[:4]] == SCHED_HEADER:
                header_row_index = i
                break

        if not last_name or not first_name:
            return jsonify({'success': False,
                            'error': 'Could not find "Last Name" and "First Name" rows in the file.'})
        if header_row_index is None:
            return jsonify({'success': False,
                            'error': 'Could not find the schedule header row '
                                     '("Subject Code, Day 1, Start Time 1, End Time 1, …").'})

        # ── Schedule data rows ──────────────────────────────────────────────
        data_rows = [r for r in all_raw[header_row_index + 1:] if any(c.strip() for c in r)]
        if not data_rows:
            return jsonify({'success': False, 'error': 'No schedule rows found below the header.'})

        # ── Helpers ─────────────────────────────────────────────────────────
        valid_days = {'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'}

        # ── Valid section ranges by college ──────────────────────────────────
        VALID_SECTION_RANGES = [
            (51000, 51200),   # College of Arts and Sciences
            (31000, 31160),   # College of Business Administration
            (83000, 83100),   # College of Computer Studies
            (45000, 45100),   # College of Criminology
            (72000, 72100),   # College of Education
            (21000, 26000),   # College of Engineering
        ]

        def is_valid_section(s):
            try:
                n = int(s)
                return any(lo <= n <= hi for lo, hi in VALID_SECTION_RANGES)
            except (ValueError, TypeError):
                return False

        # ── Valid rooms by group ─────────────────────────────────────────────
        def _expand_rooms():
            rooms = set()
            # CAS / CBA / CCS / COD shared rooms
            for i in range(1, 33):   rooms.add(f'VR{i}')
            for i in range(1, 6):    rooms.add(f'LR{i}')
            for i in range(1, 4):    rooms.add(f'A20{i}')
            for i in range(1, 6):    rooms.add(f'E10{i}')
            for i in range(1, 7):    rooms.add(f'E20{i}')
            for i in range(1, 7):    rooms.add(f'E30{i}')
            for i in range(1, 3):    rooms.add(f'X10{i}')
            for i in range(1, 5):    rooms.add(f'HS20{i}')
            rooms.update({'SPEECHLAB', 'HL-RM'})
            # COC rooms
            rooms.update({'C-CL', 'C-AVR', 'C-MC', 'C-LWA',
                          'CRIM 1-1', 'CRIM 1-2', 'CRIM 1-3',
                          'CRIM 2-1', 'CRIM 2-2',
                          'Chem Lab', 'KR'})
            # COE (Engineering) rooms
            rooms.update({'ALAB', 'EL1', 'EL2', 'EL3', 'SHOP'})
            return rooms

        VALID_ROOMS = _expand_rooms()

        def parse_time(t):
            if not t or not t.strip():
                return None, None
            t = t.strip()
            try:
                parts = t.split(':')
                if len(parts) == 2:
                    h, m = int(parts[0]), int(parts[1])
                    if 0 <= h <= 23 and 0 <= m <= 59:
                        return f'{h:02d}:{m:02d}:00', None
                return None, f"Invalid time '{t}' — use HH:MM (e.g. 07:30)"
            except Exception:
                return None, f"Invalid time '{t}' — use HH:MM (e.g. 07:30)"

        def to_minutes(t):
            if not t:
                return 0
            try:
                parts = t.split('+')[0].split(':')
                return int(parts[0]) * 60 + int(parts[1])
            except Exception:
                return 0

        def fmt_time(t):
            """Format a DB time value (e.g. '07:30:00+08') to '7:30 AM'."""
            if not t:
                return str(t)
            try:
                h, m = int(str(t).split('+')[0].split(':')[0]), int(str(t).split('+')[0].split(':')[1])
                suffix = 'AM' if h < 12 else 'PM'
                h12 = h % 12 or 12
                return f'{h12}:{m:02d} {suffix}'
            except Exception:
                return str(t)

        def overlaps(d_a, s_a, e_a, d_b, s_b, e_b):
            return (d_a and d_b and d_a == d_b and s_a and e_a and s_b and e_b and
                    to_minutes(s_a) < to_minutes(e_b) and to_minutes(e_a) > to_minutes(s_b))

        # ── DB lookup: faculty by name ───────────────────────────────────────
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT p.personnel_id, p.firstname || ' ' || p.lastname, "
            "p.college_id, c.collegename "
            "FROM personnel p LEFT JOIN college c ON p.college_id = c.college_id "
            "WHERE LOWER(p.lastname) = LOWER(%s) AND LOWER(p.firstname) = LOWER(%s)",
            (last_name, first_name))
        person = cursor.fetchone()
        if not person:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False,
                            'error': f'Faculty "{first_name} {last_name}" was not found in the system. '
                                     f'Check the spelling of the name.'})
        personnel_id = person[0]
        faculty_display = person[1]
        faculty_college_id = person[2]
        faculty_college_name = person[3] or 'Unknown College'

        # ── Subject cache ────────────────────────────────────────────────────
        subject_cache = {}

        def lookup_subject(code):
            if code in subject_cache:
                return subject_cache[code]
            cursor.execute(
                "SELECT s.subject_id, s.units, s.subjectname, s.college_id, c.collegename "
                "FROM subjects s LEFT JOIN college c ON s.college_id = c.college_id "
                "WHERE s.subjectcode = %s",
                (code,))
            row = cursor.fetchone()
            subject_cache[code] = row  # may be None
            return row

        # ── Validate each schedule row ───────────────────────────────────────
        result_rows = []
        all_valid = True
        csv_schedules = []  # (display_row, day1, s1, e1, day2, s2, e2)

        for idx, raw in enumerate(data_rows):
            # Pad short rows to avoid index errors
            while len(raw) < 10:
                raw.append('')
            (subject_code_r, classday_1_r, starttime_1_r, endtime_1_r,
             classday_2_r, starttime_2_r, endtime_2_r,
             classroom_r, classsection_r, student_count_r) = [c.strip() for c in raw[:10]]

            display_row = header_row_index + 2 + idx  # 1-based row number in file
            errors = []

            subject_code  = subject_code_r
            classday_1    = classday_1_r
            starttime_1_r = starttime_1_r
            endtime_1_r   = endtime_1_r
            classday_2    = classday_2_r or None
            starttime_2_r = starttime_2_r or None
            endtime_2_r   = endtime_2_r or None
            classroom     = classroom_r
            classsection  = classsection_r

            # Parse student_count
            student_count = None
            if student_count_r:
                try:
                    student_count = int(student_count_r)
                    if student_count < 1:
                        errors.append('Student Count must be a positive number')
                        student_count = None
                except ValueError:
                    errors.append(f"Student Count '{student_count_r}' is not a valid number")

            # Required fields
            if not subject_code:  errors.append('Subject Code is required')
            if not classday_1:    errors.append('Day 1 is required')
            if not starttime_1_r: errors.append('Start Time 1 is required')
            if not endtime_1_r:   errors.append('End Time 1 is required')
            if not classroom:     errors.append('Room is required')
            if not classsection:  errors.append('Section is required')
            if not student_count_r: errors.append('Student Count is required')

            # Section validation
            if classsection and not is_valid_section(classsection):
                errors.append(
                    f"Section '{classsection}' is not a valid section number. "
                    "Must be a numeric code within a recognised college range "
                    "(e.g. 51001 for CAS, 31001 for CBA, 83001 for CCS, "
                    "45001 for COC, 72001 for COEd, 21001 for COE)."
                )

            # Room validation
            if classroom and classroom not in VALID_ROOMS:
                errors.append(
                    f"Room '{classroom}' is not in the official room list. "
                    "Please check the room name (e.g. LR1, VR5, E101, SPEECHLAB, CRIM 1-1, ALAB)."
                )

            # Day name validation
            if classday_1 and classday_1 not in valid_days:
                errors.append(f"Day 1 '{classday_1}' is not valid — use Monday, Tuesday, … Sunday")
            if classday_2 and classday_2 not in valid_days:
                errors.append(f"Day 2 '{classday_2}' is not valid — use Monday, Tuesday, … Sunday")

            # Time parsing
            starttime_1, t_err = parse_time(starttime_1_r)
            if t_err: errors.append(f'Start Time 1: {t_err}')
            endtime_1, t_err = parse_time(endtime_1_r)
            if t_err: errors.append(f'End Time 1: {t_err}')

            starttime_2 = endtime_2 = None
            has_day2 = classday_2 or starttime_2_r or endtime_2_r
            if has_day2:
                if not classday_2:    errors.append('Day 2 is required when Day 2 times are filled')
                if not starttime_2_r: errors.append('Start Time 2 is required when Day 2 is filled')
                if not endtime_2_r:   errors.append('End Time 2 is required when Day 2 is filled')
                if starttime_2_r:
                    starttime_2, t_err = parse_time(starttime_2_r)
                    if t_err: errors.append(f'Start Time 2: {t_err}')
                if endtime_2_r:
                    endtime_2, t_err = parse_time(endtime_2_r)
                    if t_err: errors.append(f'End Time 2: {t_err}')

            if starttime_1 and endtime_1 and to_minutes(starttime_1) >= to_minutes(endtime_1):
                errors.append('Start Time 1 must be earlier than End Time 1')
            if starttime_2 and endtime_2 and to_minutes(starttime_2) >= to_minutes(endtime_2):
                errors.append('Start Time 2 must be earlier than End Time 2')

            # 30-minute boundary check (no 15-minute intervals)
            for t_val, label in [(starttime_1, 'Start Time 1'), (endtime_1, 'End Time 1'),
                                  (starttime_2, 'Start Time 2'), (endtime_2, 'End Time 2')]:
                if t_val:
                    t_min = int(t_val.split(':')[1])
                    if t_min not in (0, 30):
                        errors.append(
                            f'{label}: Times must be on the hour or half-hour (e.g. 07:00, 07:30) '
                            f'— :{t_min:02d} is not a valid interval'
                        )

            # Subject lookup
            subject_id = None
            subject_name = subject_code
            subject_units = None
            if subject_code:
                subj = lookup_subject(subject_code)
                if subj:
                    subject_id = subj[0]
                    subject_units = subj[1]
                    subject_name = subj[2]
                    subject_college_id = subj[3]
                    subject_college_name = subj[4] or 'Unknown College'
                    # College mismatch check
                    if faculty_college_id and subject_college_id and faculty_college_id != subject_college_id:
                        errors.append(
                            f'Subject "{subject_code}" belongs to {subject_college_name} but '
                            f'{faculty_display} is from {faculty_college_name}. '
                            f'Faculty may only be assigned subjects from their own college.'
                        )
                else:
                    errors.append(f'Subject code "{subject_code}" was not found in the system')

            # Units validation: total scheduled time must equal subject units × 60 minutes
            if subject_units is not None and starttime_1 and endtime_1:
                day1_mins = to_minutes(endtime_1) - to_minutes(starttime_1)
                day2_mins = (to_minutes(endtime_2) - to_minutes(starttime_2)) if (starttime_2 and endtime_2) else 0
                total_mins = day1_mins + day2_mins
                expected_mins = round(float(subject_units) * 60)
                if total_mins != expected_mins:
                    total_hrs = total_mins / 60
                    exp_hrs = float(subject_units)
                    errors.append(
                        f'Total scheduled time ({total_hrs:.1f} hr{"s" if total_hrs != 1 else ""}) does not match '
                        f'subject units ({subject_units} unit{"s" if float(subject_units) != 1 else ""} '
                        f'= {exp_hrs:.1f} hr{"s" if exp_hrs != 1 else ""} per week). '
                        f'Adjust the time slots to total {exp_hrs:.1f} hr{"s" if exp_hrs != 1 else ""} across all days.'
                    )

            # Internal conflict: day1 == day2 overlapping
            if (not errors and classday_1 and classday_2 and classday_1 == classday_2
                    and starttime_1 and endtime_1 and starttime_2 and endtime_2):
                s1, e1, s2, e2 = to_minutes(starttime_1), to_minutes(endtime_1), to_minutes(starttime_2), to_minutes(endtime_2)
                if (s1 < e2 and e1 > s2) or (s2 < e1 and e2 > s1):
                    errors.append(f'Day 1 and Day 2 are both {classday_1} with overlapping times')

            # DB conflict check (against existing saved schedules)
            if not errors and starttime_1 and endtime_1:
                cursor.execute("""
                    SELECT sub.subjectcode, s.classday_1, s.starttime_1, s.endtime_1, s.classsection
                    FROM schedule s JOIN subjects sub ON s.subject_id = sub.subject_id
                    WHERE s.personnel_id = %s AND s.acadcalendar_id = %s
                      AND s.classday_1 = %s AND s.starttime_1 < %s AND s.endtime_1 > %s
                """, (personnel_id, semester_id, classday_1, endtime_1, starttime_1))
                for c in cursor.fetchall():
                    errors.append(f'Conflicts with existing schedule: {c[0]} on {c[1]} {fmt_time(c[2])} – {fmt_time(c[3])} (Section {c[4]})')

                cursor.execute("""
                    SELECT sub.subjectcode, s.classday_2, s.starttime_2, s.endtime_2, s.classsection
                    FROM schedule s JOIN subjects sub ON s.subject_id = sub.subject_id
                    WHERE s.personnel_id = %s AND s.acadcalendar_id = %s
                      AND s.classday_2 = %s AND s.starttime_2 < %s AND s.endtime_2 > %s
                """, (personnel_id, semester_id, classday_1, endtime_1, starttime_1))
                for c in cursor.fetchall():
                    errors.append(f'Conflicts with existing schedule: {c[0]} on {c[1]} {fmt_time(c[2])} – {fmt_time(c[3])} (Section {c[4]})')

                if classday_2 and starttime_2 and endtime_2:
                    cursor.execute("""
                        SELECT sub.subjectcode, s.classday_1, s.starttime_1, s.endtime_1, s.classsection
                        FROM schedule s JOIN subjects sub ON s.subject_id = sub.subject_id
                        WHERE s.personnel_id = %s AND s.acadcalendar_id = %s
                          AND s.classday_1 = %s AND s.starttime_1 < %s AND s.endtime_1 > %s
                    """, (personnel_id, semester_id, classday_2, endtime_2, starttime_2))
                    for c in cursor.fetchall():
                        errors.append(f'Conflicts with existing schedule: {c[0]} on {c[1]} {fmt_time(c[2])} – {fmt_time(c[3])} (Section {c[4]})')

                    cursor.execute("""
                        SELECT sub.subjectcode, s.classday_2, s.starttime_2, s.endtime_2, s.classsection
                        FROM schedule s JOIN subjects sub ON s.subject_id = sub.subject_id
                        WHERE s.personnel_id = %s AND s.acadcalendar_id = %s
                          AND s.classday_2 = %s AND s.starttime_2 < %s AND s.endtime_2 > %s
                    """, (personnel_id, semester_id, classday_2, endtime_2, starttime_2))
                    for c in cursor.fetchall():
                        errors.append(f'Conflicts with existing schedule: {c[0]} on {c[1]} {fmt_time(c[2])} – {fmt_time(c[3])} (Section {c[4]})')

            # Inter-row conflict (within this file)
            if not errors and starttime_1 and endtime_1:
                for (prev_rn, prev_d1, prev_s1, prev_e1, prev_d2, prev_s2, prev_e2) in csv_schedules:
                    if overlaps(classday_1, starttime_1, endtime_1, prev_d1, prev_s1, prev_e1):
                        errors.append(f'Conflicts with Row {prev_rn} in this file (same day and overlapping time)')
                    if overlaps(classday_1, starttime_1, endtime_1, prev_d2, prev_s2, prev_e2):
                        errors.append(f'Conflicts with Row {prev_rn} in this file (same day and overlapping time)')
                    if classday_2 and starttime_2 and endtime_2:
                        if overlaps(classday_2, starttime_2, endtime_2, prev_d1, prev_s1, prev_e1):
                            errors.append(f'Conflicts with Row {prev_rn} in this file (same day and overlapping time)')
                        if overlaps(classday_2, starttime_2, endtime_2, prev_d2, prev_s2, prev_e2):
                            errors.append(f'Conflicts with Row {prev_rn} in this file (same day and overlapping time)')

            status = 'error' if errors else 'valid'
            if errors:
                all_valid = False

            result_row = {
                'row_num': display_row,
                'status': status,
                'errors': errors,
                'subject_code': subject_code,
                'subject_name': subject_name,
                'classday_1': classday_1,
                'starttime_1': starttime_1_r or '',
                'endtime_1': endtime_1_r or '',
                'classday_2': classday_2 or '',
                'starttime_2': starttime_2_r or '',
                'endtime_2': endtime_2_r or '',
                'classroom': classroom,
                'classsection': classsection,
                'student_count': student_count,
            }

            if status == 'valid':
                result_row['import_data'] = {
                    'personnel_id': personnel_id,
                    'subject_id': subject_id,
                    'classday_1': classday_1,
                    'starttime_1': starttime_1,
                    'endtime_1': endtime_1,
                    'classday_2': classday_2,
                    'starttime_2': starttime_2,
                    'endtime_2': endtime_2,
                    'classroom': classroom,
                    'classsection': classsection,
                    'student_count': student_count,
                }
                csv_schedules.append((display_row, classday_1, starttime_1, endtime_1,
                                      classday_2, starttime_2, endtime_2))

            result_rows.append(result_row)

        cursor.close()
        db_pool.return_connection(conn)
        conn = None

        valid_count = sum(1 for r in result_rows if r['status'] == 'valid')
        error_count = sum(1 for r in result_rows if r['status'] == 'error')
        return jsonify({
            'success': True,
            'faculty_name': faculty_display,
            'all_valid': all_valid,
            'rows': result_rows,
            'total': len(result_rows),
            'valid_count': valid_count,
            'error_count': error_count,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        if conn:
            db_pool.return_connection(conn)
        return jsonify({'success': False, 'error': f'Error processing CSV: {str(e)}'})


@app.route('/api/hr/import-schedule-csv', methods=['POST'])
@require_auth([20003])
def api_hr_import_schedule_csv():
    """Import pre-validated schedule rows from the CSV flow (all-or-nothing transaction)."""
    conn = None
    try:
        data = request.get_json()
        semester_id = data.get('semester_id')
        rows = data.get('rows', [])

        if not semester_id:
            return jsonify({'success': False, 'error': 'Semester is required'})
        if not rows:
            return jsonify({'success': False, 'error': 'No rows to import'})

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COALESCE(MAX(class_id), 60000) FROM schedule")
        base_id = cursor.fetchone()[0]

        for i, row in enumerate(rows):
            new_class_id = base_id + 1 + i
            cursor.execute("""
                INSERT INTO schedule (
                    class_id, personnel_id, subject_id,
                    classday_1, starttime_1, endtime_1,
                    classday_2, starttime_2, endtime_2,
                    classroom, acadcalendar_id, classsection, student_count
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                new_class_id,
                row['personnel_id'], row['subject_id'],
                row['classday_1'], row['starttime_1'], row['endtime_1'],
                row.get('classday_2'), row.get('starttime_2'), row.get('endtime_2'),
                row['classroom'], semester_id, row['classsection'], row.get('student_count'),
            ))
            create_initial_attendance_report(row['personnel_id'], new_class_id, semester_id, conn)

        conn.commit()

        hr_user_id = session['user_id']
        hr_info = get_personnel_info(hr_user_id)
        log_audit_action(
            hr_info.get('personnel_id'),
            'Bulk schedule import',
            f"HR imported {len(rows)} schedule(s) via CSV for semester {semester_id}"
        )

        cursor.close()
        db_pool.return_connection(conn)
        conn = None
        return jsonify({'success': True, 'message': f'Successfully imported {len(rows)} schedule(s)', 'count': len(rows)})

    except Exception as e:
        import traceback
        traceback.print_exc()
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
            db_pool.return_connection(conn)
        return jsonify({'success': False, 'error': f'Import failed: {str(e)}'})


@app.route('/api/hr/delete-schedule/classes/<int:personnel_id>')
@require_auth([20003])
def api_hr_delete_schedule_classes(personnel_id):
    """Get all schedule classes for a faculty member for deletion dropdown"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                s.class_id,
                s.classday_1,
                s.starttime_1,
                s.endtime_1,
                s.classday_2,
                s.starttime_2,
                s.endtime_2,
                sub.subjectcode,
                s.classsection,
                s.classroom
            FROM schedule s
            JOIN subjects sub ON s.subject_id = sub.subject_id
            WHERE s.personnel_id = %s
            ORDER BY sub.subjectcode, s.classsection
        """, (personnel_id,))
        
        schedules = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        schedule_list = []
        for schedule in schedules:
            (class_id, classday_1, starttime_1, endtime_1, classday_2, 
             starttime_2, endtime_2, subjectcode, classsection, classroom) = schedule
            
            def format_time_display(time_val):
                if not time_val:
                    return None
                if isinstance(time_val, str):
                    time_str = time_val[:5]
                elif hasattr(time_val, 'strftime'):
                    time_str = time_val.strftime('%H:%M')
                else:
                    time_str = str(time_val)[:5]
                
                try:
                    hours, minutes = map(int, time_str.split(':'))
                    period = 'AM' if hours < 12 else 'PM'
                    display_hour = hours if hours <= 12 else hours - 12
                    display_hour = 12 if display_hour == 0 else display_hour
                    return f"{display_hour}:{minutes:02d} {period}"
                except:
                    return time_str
            
            day1_display = f"{classday_1} {format_time_display(starttime_1)}-{format_time_display(endtime_1)}" if classday_1 else None
            day2_display = f"{classday_2} {format_time_display(starttime_2)}-{format_time_display(endtime_2)}" if classday_2 else None
            
            schedule_label = f"{subjectcode}, Section: {classsection}"
            if day1_display and day2_display:
                schedule_label += f", {day1_display} & {day2_display}"
            elif day1_display:
                schedule_label += f", {day1_display}"
            elif day2_display:
                schedule_label += f", {day2_display}"
            
            schedule_label += f", Room: {classroom}" if classroom else ""
            
            schedule_list.append({
                'class_id': class_id,
                'display_label': schedule_label,
                'subject_code': subjectcode,
                'section': classsection,
                'day1': day1_display,
                'day2': day2_display,
                'classroom': classroom
            })
        
        return {
            'success': True,
            'schedules': schedule_list
        }
        
    except Exception as e:
        print(f"Error fetching faculty schedule classes: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/delete-schedule', methods=['POST'])
@require_auth([20003])
def api_hr_delete_schedule():
    """Delete one or more schedules and all associated attendance records / reports"""
    try:
        data = request.get_json()

        # Accept either a single class_id (legacy) or a class_ids list
        class_ids = data.get('class_ids') or []
        if not class_ids and data.get('class_id'):
            class_ids = [data['class_id']]
        if not class_ids:
            return {'success': False, 'error': 'At least one Class ID is required'}

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        total_attendance = 0
        total_reports = 0
        audit_lines = []

        cursor.execute("BEGIN")
        for cid in class_ids:
            cursor.execute("""
                SELECT s.class_id, sub.subjectcode, sub.subjectname,
                       s.classsection, s.classday_1, s.starttime_1, s.endtime_1,
                       s.classday_2, s.starttime_2, s.endtime_2,
                       p.firstname, p.lastname, p.honorifics
                FROM schedule s
                JOIN subjects sub ON s.subject_id = sub.subject_id
                JOIN personnel p   ON s.personnel_id = p.personnel_id
                WHERE s.class_id = %s
            """, (cid,))
            row = cursor.fetchone()
            if not row:
                continue

            (cid, subjectcode, subjectname, classsection,
             classday_1, starttime_1, endtime_1,
             classday_2, starttime_2, endtime_2,
             firstname, lastname, honorifics) = row

            faculty_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"

            cursor.execute("SELECT COUNT(*) FROM attendance WHERE class_id = %s", (cid,))
            att_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM attendancereport WHERE class_id = %s", (cid,))
            rep_count = cursor.fetchone()[0]

            cursor.execute("UPDATE rfidlogs SET matched_class_id = NULL WHERE matched_class_id = %s", (cid,))
            cursor.execute("DELETE FROM attendancereport WHERE class_id = %s", (cid,))
            cursor.execute("DELETE FROM attendance      WHERE class_id = %s", (cid,))
            cursor.execute("DELETE FROM schedule        WHERE class_id = %s", (cid,))

            total_attendance += att_count
            total_reports    += rep_count

            sched_info = f"{subjectcode} - {subjectname} | Section {classsection}"
            if classday_1:
                sched_info += f" | {classday_1} {starttime_1}-{endtime_1}"
            if classday_2:
                sched_info += f" & {classday_2} {starttime_2}-{endtime_2}"
            audit_lines.append(f"{faculty_name}: {sched_info}")

        conn.commit()

        hr_user_id = session['user_id']
        hr_personnel_info = get_personnel_info(hr_user_id)
        hr_personnel_id = hr_personnel_info.get('personnel_id')

        log_audit_action(
            hr_personnel_id,
            "Schedule deleted",
            f"HR deleted {len(class_ids)} schedule(s), {total_attendance} attendance record(s), "
            f"and {total_reports} attendance report(s).\n" + "\n".join(audit_lines),
            before_value=f"Schedules existed for class IDs: {class_ids}",
            after_value="Schedules and all associated records deleted"
        )

        cursor.close()
        db_pool.return_connection(conn)

        n = len(class_ids)
        message = (f'{n} schedule(s), {total_attendance} attendance record(s), and '
                   f'{total_reports} attendance report(s) deleted successfully')

        return {
            'success': True,
            'message': message,
            'deleted_count': n,
            'attendance_records_deleted': total_attendance,
            'attendance_reports_deleted': total_reports
        }
        
    except Exception as e:
        print(f"Error deleting schedule: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        
        if 'violates foreign key constraint' in str(e):
            return {
                'success': False, 
                'error': f'Cannot delete schedule due to database constraints. Please contact administrator. Error: {str(e)}'
            }
        
        return {'success': False, 'error': str(e)}

@app.route('/hr_employee_profile/<int:personnel_id>')
@require_auth([20003])
def hr_employee_profile(personnel_id):
    """HR view of employee profile"""
    try:

        hr_info = get_personnel_info(session['user_id'])
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                p.firstname,
                p.lastname,
                p.honorifics,
                c.collegename,
                p.employee_no,
                r.rolename,
                u.email,
                pr.position,
                pr.employmentstatus
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN roles r ON p.role_id = r.role_id
            LEFT JOIN users u ON p.user_id = u.user_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE p.personnel_id = %s
        """, (personnel_id,))
        
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus = result
            
            full_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            
            employee_info = {
                'hr_name': hr_info['hr_name'],  
                'college': hr_info['college'],  
                'profile_image_base64': hr_info['profile_image_base64'], 
                'employee_name': full_name,
                'college': collegename or 'College of Computer Studies',
                'employee_no': employee_no,
                'email': email or 'email@spc.edu.ph',
                'position': position or 'Full-Time Employee',
                'employment_status': employmentstatus or 'Regular',
                'firstname': firstname,
                'is_hr_viewing': True,
                'is_vp_viewing': False
            }
            
            session['viewing_personnel_id'] = personnel_id
            
            return render_template('faculty&dean/faculty-profile.html', **employee_info)
        else:
            return "Employee not found", 404
            
    except Exception as e:
        print(f"Error loading employee profile: {e}")
        return "Error loading profile", 500

@app.route('/faculty_employee_profile/<int:personnel_id>')
@require_auth([20003, 20004])
def faculty_employee_profile(personnel_id):
    """HR view of faculty/dean profile"""
    try:
        hr_info = get_personnel_info(session['user_id'])
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                p.firstname,
                p.lastname,
                p.honorifics,
                c.collegename,
                p.employee_no,
                r.rolename,
                u.email,
                pr.position,
                pr.employmentstatus
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN roles r ON p.role_id = r.role_id
            LEFT JOIN users u ON p.user_id = u.user_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE p.personnel_id = %s
        """, (personnel_id,))
        
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus = result
            
            full_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            
            employee_info = {
                'hr_name': hr_info['hr_name'],  
                'college': hr_info['college'],   
                'profile_image_base64': hr_info['profile_image_base64'], 
                'faculty_name': full_name,
                'college': collegename or 'College of Computer Studies',
                'employee_no': employee_no,
                'email': email or 'email@spc.edu.ph',
                'position': position or 'Full-Time Employee',
                'employment_status': employmentstatus or 'Regular',
                'firstname': firstname,
                'is_hr_viewing': True,
                'is_vp_viewing': False
            }
            
            session['viewing_personnel_id'] = personnel_id
            
            return render_template('faculty&dean/faculty-profile.html', **employee_info)
        else:
            return "Employee not found", 404
            
    except Exception as e:
        print(f"Error loading employee profile: {e}")
        return "Error loading profile", 500
    
@app.route('/vp_employee_profile/<int:personnel_id>')
@require_auth([20004])
def vp_employee_profile(personnel_id):
    """HR view of VP/President profile"""
    try:
        hr_info = get_personnel_info(session['user_id'])
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                p.firstname,
                p.lastname,
                p.honorifics,
                c.collegename,
                p.employee_no,
                r.rolename,
                u.email,
                pr.position,
                pr.employmentstatus
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN roles r ON p.role_id = r.role_id
            LEFT JOIN users u ON p.user_id = u.user_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE p.personnel_id = %s
        """, (personnel_id,))
        
        print(f"Executing SQL for VP profile with personnel_id: {personnel_id}")
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus = result
            
            full_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            
            employee_info = {
                'hr_name': hr_info['hr_name'],  
                'college': hr_info['college'],  
                'profile_image_base64': hr_info['profile_image_base64'],  
                'vp_name': full_name,
                'college': collegename or 'College of Computer Studies',
                'employee_no': employee_no,
                'email': email or 'email@spc.edu.ph',
                'position': position or 'Full-Time Employee',
                'employment_status': employmentstatus or 'Regular',
                'firstname': firstname,
                'is_hr_viewing': False,
                'is_vp_viewing': True
            }
            
            session['viewing_personnel_id'] = personnel_id
            
            return render_template('vp&pres/vp-profile.html', **employee_info)
        else:
            return "Employee not found", 404
            
    except Exception as e:
        print(f"Error loading VP employee profile: {e}")
        return "Error loading profile", 500

@app.route('/api/hr/employee/profile/<int:personnel_id>')
@require_auth([20003])
def api_hr_employee_profile(personnel_id):
    """API endpoint to get employee profile data for HR viewing"""
    return api_get_faculty_profile()

@app.route('/api/hr/employee/profile/stats/<int:personnel_id>')
@require_auth([20003])
def api_hr_employee_profile_stats(personnel_id):
    """API endpoint to get employee profile statistics for HR viewing"""
    return api_get_profile_stats()

@app.route('/api/hr/colleges-list')
@require_auth([20003])
def api_hr_colleges_list():
    """OPTIMIZED: Get all colleges for filter dropdown - CACHED"""
    cache_key = "colleges_list"
    cached = get_cached(cache_key, ttl=3600)
    if cached:
        return cached
    
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT college_id, collegename 
            FROM college 
            ORDER BY collegename
        """)
        
        colleges = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        colleges_list = []
        for college in colleges:
            college_id, collegename = college
            colleges_list.append({
                'college_id': college_id,
                'collegename': collegename
            })
        
        result = {
            'success': True,
            'colleges': colleges_list
        }
        
        set_cached(cache_key, result)
        return result
        
    except Exception as e:
        print(f"Error fetching colleges list: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/clear-viewing-session')
@require_auth([20003])
def clear_viewing_session():
    """Clear the viewing personnel session variable"""
    session.pop('viewing_personnel_id', None)
    return {'success': True}

@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        try:
            conn = db_pool.get_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT user_id FROM users WHERE email = %s", (email,))
            user_exists = cursor.fetchone()
            
            if not user_exists:
                cursor.close()
                db_pool.return_connection(conn)
                return render_template('login.html', error="User does not exist.")
            
            cursor.execute("""
                SELECT u.user_id, u.email, u.role_id, u.lastlogin, u.lastlogout
                FROM users u 
                WHERE u.email = %s AND u.password = %s
            """, (email, password))
            
            user = cursor.fetchone()
            
            if user and user[2] in ROLE_REDIRECTS:
                user_id, user_email, role_id, last_login, last_logout = user
    
                philippines_tz = pytz.timezone('Asia/Manila')
                current_time = datetime.now(philippines_tz).replace(microsecond=0)
                
                cursor.execute("""
                    UPDATE users SET lastlogin = %s WHERE user_id = %s
                """, (current_time, user_id))
                
                cursor.execute("SELECT personnel_id, firstname, lastname FROM personnel WHERE user_id = %s", (user_id,))
                personnel_result = cursor.fetchone()
                
                conn.commit()
                cursor.close()
                db_pool.return_connection(conn)
                
                session['user_id'] = user_id
                session['email'] = user_email
                session['user_role'] = role_id
                session['user_type'] = ROLE_REDIRECTS[role_id][0]
                
                if personnel_result:
                    personnel_id, firstname, lastname = personnel_result
                    last_login_str = last_login.strftime('%Y-%m-%d %H:%M:%S') if last_login else "Never"
                    last_logout_str = last_logout.strftime('%Y-%m-%d %H:%M:%S') if last_logout else "Never"
                    current_time_str = current_time.strftime('%Y-%m-%d %H:%M:%S')
                    
                    log_audit_action(
                        personnel_id, 
                        "User logged in", 
                        f"User logged in of the system",
                        before_value=f"Last logout: {last_logout_str}",
                        after_value=f"Current login: {current_time_str}"
                    )
                
                return redirect(url_for(ROLE_REDIRECTS[role_id][1]))
            else:
                cursor.execute("SELECT personnel_id FROM personnel WHERE user_id IN (SELECT user_id FROM users WHERE email = %s)", (email,))
                personnel_result = cursor.fetchone()
                
                if personnel_result:
                    log_audit_action(
                        personnel_result[0],
                        "Failed login attempt",
                        f"Failed login attempt for email: {email}",
                        evidence="Invalid password provided"
                    )
                
                cursor.close()
                db_pool.return_connection(conn)
                return render_template('login.html', error="Invalid password. Please try again.")
                
        except Exception as e:
            print(f"Database error: {e}")
            return render_template('login.html', error="Database connection error. Please try again.")
    
    return render_template('login.html')

@app.route('/reset_password')
def reset_password():
    return render_template('reset.html')

@app.route('/faculty-dashboard')
@require_auth([20001, 20002])
def faculty_dashboard():
    faculty_info = get_faculty_info(session['user_id'])
    return render_template('faculty&dean/faculty-dashboard.html', **faculty_info)

@app.route('/faculty_attendance')
@require_auth([20001, 20002])
def faculty_attendance():
    faculty_info = get_faculty_info(session['user_id'])
    return render_template('faculty&dean/faculty-attendance.html', **faculty_info)

@app.route('/faculty_evaluations')
@require_auth([20001, 20002])
def faculty_evaluations():
    faculty_info = get_faculty_info(session['user_id'])
    return render_template('faculty&dean/faculty-evaluations.html', **faculty_info)

def generate_peer_assignments(acadcalendar_id, department):
    """
    Implements the '2 and 2' rule: every faculty evaluates 2 peers 
    and is evaluated by 2 peers within their department.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Resolve collegename -> college_id
        cur.execute("SELECT college_id FROM college WHERE collegename = %s", (department,))
        college_row = cur.fetchone()
        if college_row is None:
            return False, f"College '{department}' not found."
        college_id = college_row[0]

        # Fetch all faculty in that college (faculty role only, not deans)
        cur.execute("SELECT personnel_id FROM personnel WHERE college_id = %s AND role_id = 20001", (college_id,))
        faculty_ids = [row[0] for row in cur.fetchall()]
        
        # Guidelines require at least 3 members for rotation
        if len(faculty_ids) < 3:
            return False, "At least 3 faculty members are required for peer rotation."

        import random
        random.shuffle(faculty_ids)
        n = len(faculty_ids)

        # Delete existing non-completed assignments for this dept+term before regenerating
        cur.execute("""
            DELETE FROM peer_assignments
            WHERE acadcalendar_id = %s
              AND evaluator_id IN (
                  SELECT personnel_id FROM personnel WHERE college_id = %s
              )
              AND is_completed = FALSE
        """, (acadcalendar_id, college_id))

        for i in range(n):
            evaluator = faculty_ids[i]
            # Use circular shift (i+1 and i+2) to pick 2 distinct peers
            peer1 = faculty_ids[(i + 1) % n]
            peer2 = faculty_ids[(i + 2) % n]
            
            # Insert assignments into the new table structure
            cur.execute("""
                INSERT INTO peer_assignments (evaluator_id, evaluatee_id, acadcalendar_id, is_completed)
                VALUES (%s, %s, %s, FALSE), (%s, %s, %s, FALSE)
            """, (evaluator, peer1, acadcalendar_id, evaluator, peer2, acadcalendar_id))

        conn.commit()
        return True, "Success"
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        cur.close()
        conn.close()

# --- Peer Evaluation Routes ---

@app.route('/api/hr/generate-peer-assignments', methods=['POST'])
@require_auth([20003]) # HR only
def api_generate_peer_assignments():
    try:
        data = request.get_json()
        acadcalendar_id = data.get('acadcalendar_id')
        department = data.get('collegename')

        if not acadcalendar_id or not department:
            return jsonify(success=False, message="Missing Academic Term or Department."), 400

        # Call the existing helper function in app.py
        success, message = generate_peer_assignments(acadcalendar_id, department)
        
        if success:
            # Optional: Log the action for audit
            hr_info = get_personnel_info(session['user_id'])
            log_audit_action(
                hr_info.get('personnel_id'),
                "Generated Peer Assignments",
                f"Generated randomized peer evaluations for {department}."
            )
            return jsonify(success=True, message=message)
        else:
            return jsonify(success=False, message=message), 400

    except Exception as e:
        print(f"Error in peer assignment route: {str(e)}")
        return jsonify(success=False, message="Internal Server Error"), 500

@app.route('/api/faculty/peer-assignments')
@require_auth([20001, 20002])
def api_faculty_peer_assignments():
    """Returns the peer evaluation tasks assigned to the logged-in faculty member."""
    try:
        user_id = session['user_id']
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT 
                pa.assignment_id,
                pa.evaluatee_id,
                p.firstname,
                p.lastname,
                col.collegename,
                c.acadyear,
                c.semester,
                pa.is_completed
            FROM peer_assignments pa
            JOIN personnel p ON pa.evaluatee_id = p.personnel_id
            JOIN college col ON p.college_id = col.college_id
            JOIN acadcalendar c ON pa.acadcalendar_id = c.acadcalendar_id
            WHERE pa.evaluator_id = %s
            ORDER BY pa.is_completed ASC, c.semesterstart DESC
        """, (personnel_id,))

        rows = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)

        assignments = []
        for row in rows:
            assignment_id, evaluatee_id, firstname, lastname, college, acadyear, semester, is_completed = row
            assignments.append({
                'assignment_id': assignment_id,
                'evaluatee_id': evaluatee_id,
                'name': f"{firstname} {lastname}",
                'college': college,
                'period': f"{acadyear if acadyear.startswith('AY') else 'AY ' + acadyear} — {semester}",
                'is_completed': is_completed
            })

        return jsonify({'success': True, 'assignments': assignments})

    except Exception as e:
        print(f"Error fetching peer assignments: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/faculty/evaluation-trends')
@require_auth([20001, 20002])
def api_faculty_evaluation_trends():
    """Returns the logged-in faculty's evaluation scores across all semesters."""
    try:
        user_id = session.get('user_id')
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        if not personnel_id:
            return jsonify({'success': False, 'error': 'Personnel not found'}), 404

        conn = db_pool.get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                ac.acadcalendar_id,
                ac.semester,
                ac.acadyear,
                ac.semesterstart,
                MAX(CASE WHEN fe.evaluator_type = 'student'    THEN fe.score END) AS avg_student,
                MAX(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score END) AS avg_supervisor,
                MAX(CASE WHEN fe.evaluator_type = 'peer'       THEN fe.score END) AS avg_peer
            FROM faculty_evaluations fe
            JOIN acadcalendar ac ON fe.acadcalendar_id = ac.acadcalendar_id
            WHERE fe.personnel_id = %s
            GROUP BY ac.acadcalendar_id, ac.semester, ac.acadyear, ac.semesterstart
            ORDER BY ac.semesterstart ASC NULLS LAST
        """, (personnel_id,))

        trends = []
        for row in cur.fetchall():
            _, sem, year, _, avg_s, avg_sv, avg_p = row
            short = '1st Sem' if 'First' in sem else ('2nd Sem' if 'Second' in sem else 'Summer')
            year_str = (year or '').replace('AY ', '')
            label = f"{short} AY {year_str}"

            avg_s  = float(avg_s)  if avg_s  is not None else None
            avg_sv = float(avg_sv) if avg_sv is not None else None
            avg_p  = float(avg_p)  if avg_p  is not None else None

            weights = []
            if avg_s  is not None: weights.append((avg_s,  0.55))
            if avg_sv is not None: weights.append((avg_sv, 0.35))
            if avg_p  is not None: weights.append((avg_p,  0.10))

            overall = None
            if weights:
                total_w = sum(w for _, w in weights)
                overall = round(sum(v * w for v, w in weights) / total_w, 3)

            trends.append({
                'label':      label,
                'overall':    overall,
                'student':    round(avg_s,  3) if avg_s  is not None else None,
                'supervisor': round(avg_sv, 3) if avg_sv is not None else None,
                'peer':       round(avg_p,  3) if avg_p  is not None else None,
            })

        cur.close()
        db_pool.return_connection(conn)
        return jsonify({'success': True, 'trends': trends})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hr/peer-assignments')
@require_auth([20003])
def api_hr_peer_assignments():
    """Returns all peer assignments for HR to view, filterable by term and department."""
    try:
        term_id = request.args.get('term_id', type=int)
        dept = request.args.get('dept', '').strip()

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        query = """
            SELECT
                pa.assignment_id,
                pa.is_completed,
                ev.firstname || ' ' || ev.lastname AS evaluator_name,
                ee.firstname || ' ' || ee.lastname AS evaluatee_name,
                col.collegename,
                c.acadyear,
                c.semester,
                c.acadcalendar_id
            FROM peer_assignments pa
            JOIN personnel ev ON pa.evaluator_id = ev.personnel_id
            JOIN personnel ee ON pa.evaluatee_id = ee.personnel_id
            JOIN college col ON ev.college_id = col.college_id
            JOIN acadcalendar c ON pa.acadcalendar_id = c.acadcalendar_id
        """
        conditions = []
        params = []
        if term_id:
            conditions.append("pa.acadcalendar_id = %s")
            params.append(term_id)
        if dept:
            conditions.append("col.collegename = %s")
            params.append(dept)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY col.collegename, ev.lastname, ee.lastname"

        cursor.execute(query, params)
        rows = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)

        assignments = []
        for row in rows:
            assignment_id, is_completed, evaluator_name, evaluatee_name, collegename, acadyear, semester, acad_id = row
            assignments.append({
                'assignment_id': assignment_id,
                'is_completed': is_completed,
                'evaluator_name': evaluator_name,
                'evaluatee_name': evaluatee_name,
                'department': collegename,
                'period': f"{acadyear if acadyear.startswith('AY') else 'AY ' + acadyear} — {semester}",
                'acadcalendar_id': acad_id,
            })

        total = len(assignments)
        completed = sum(1 for a in assignments if a['is_completed'])
        return jsonify(success=True, assignments=assignments, total=total, completed=completed)

    except Exception as e:
        print(f"Error fetching HR peer assignments: {e}")
        return jsonify(success=False, error=str(e)), 500


@app.route('/api/hr/peer-assignments/<int:assignment_id>', methods=['DELETE'])
@require_auth([20003])
def api_delete_peer_assignment(assignment_id):
    """HR deletes a single non-completed peer assignment."""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT is_completed FROM peer_assignments WHERE assignment_id = %s",
            (assignment_id,)
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error="Assignment not found."), 404

        if row[0]:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error="Cannot delete a completed assignment."), 400

        cursor.execute(
            "DELETE FROM peer_assignments WHERE assignment_id = %s",
            (assignment_id,)
        )
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)

        hr_info = get_personnel_info(session['user_id'])
        log_audit_action(
            hr_info.get('personnel_id'),
            "Deleted Peer Assignment",
            f"Deleted peer assignment ID {assignment_id}."
        )
        return jsonify(success=True, message="Assignment deleted.")

    except Exception as e:
        print(f"Error deleting peer assignment: {e}")
        return jsonify(success=False, error=str(e)), 500


@app.route('/api/hr/peer-assignments/manual', methods=['POST'])
@require_auth([20003])
def api_manual_peer_assignment():
    """HR manually creates a single peer assignment."""
    try:
        data = request.get_json()
        evaluator_id = data.get('evaluator_id')
        evaluatee_id = data.get('evaluatee_id')
        acadcalendar_id = data.get('acadcalendar_id')

        if not all([evaluator_id, evaluatee_id, acadcalendar_id]):
            return jsonify(success=False, error="Missing required fields."), 400

        if int(evaluator_id) == int(evaluatee_id):
            return jsonify(success=False, error="Evaluator and evaluatee cannot be the same person."), 400

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        # Prevent duplicate assignments
        cursor.execute("""
            SELECT 1 FROM peer_assignments
            WHERE evaluator_id = %s AND evaluatee_id = %s AND acadcalendar_id = %s
        """, (evaluator_id, evaluatee_id, acadcalendar_id))
        if cursor.fetchone():
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error="This assignment already exists."), 409

        cursor.execute("""
            INSERT INTO peer_assignments (evaluator_id, evaluatee_id, acadcalendar_id, is_completed)
            VALUES (%s, %s, %s, FALSE)
        """, (evaluator_id, evaluatee_id, acadcalendar_id))
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)

        hr_info = get_personnel_info(session['user_id'])
        log_audit_action(
            hr_info.get('personnel_id'),
            "Manual Peer Assignment",
            f"Manually assigned evaluator ID {evaluator_id} to evaluate ID {evaluatee_id} for term {acadcalendar_id}."
        )
        return jsonify(success=True, message="Assignment created.")

    except Exception as e:
        print(f"Error creating manual peer assignment: {e}")
        return jsonify(success=False, error=str(e)), 500


@app.route('/faculty/peer-evaluations')
@require_auth([20001, 20002])
def peer_evaluations_list():
    # Peer evaluation tasks are shown on the faculty evaluations dashboard.
    return redirect(url_for('faculty_evaluations'))

@app.route('/faculty/evaluate/<int:evaluatee_id>', methods=['GET', 'POST'])
@require_auth([20001, 20002])
def submit_peer_eval(evaluatee_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        try:
            scores = [int(request.form.get(f'q{i}')) for i in range(1, 21)]
            cat1_avg = sum(scores[0:5]) / 5
            cat2_avg = sum(scores[5:10]) / 5
            cat3_avg = sum(scores[10:15]) / 5
            cat4_avg = sum(scores[15:20]) / 5
            final_score = (cat1_avg * 0.30) + (cat2_avg * 0.30) + (cat3_avg * 0.20) + (cat4_avg * 0.20)

            evaluator_name = request.form.get('evaluator_name', '').strip() or None
            strengths = request.form.get('strengths', '').strip()
            growth = request.form.get('growth', '').strip()
            comments = request.form.get('comments', '').strip() or None

            # Resolve personnel_id from user_id
            cur.execute('SELECT personnel_id FROM personnel WHERE user_id = %s', (user_id,))
            evaluator_row = cur.fetchone()
            if not evaluator_row:
                raise ValueError(f'No personnel record for user_id {user_id}')
            evaluator_personnel_id = evaluator_row[0]

            # Get acadcalendar_id from the assignment
            cur.execute(
                'SELECT acadcalendar_id FROM peer_assignments '
                'WHERE evaluator_id = %s AND evaluatee_id = %s '
                'ORDER BY date_assigned DESC LIMIT 1',
                (evaluator_personnel_id, evaluatee_id))
            assignment_row = cur.fetchone()
            if not assignment_row:
                raise ValueError(f'No peer assignment for evaluator {evaluator_personnel_id} -> {evaluatee_id}')
            acadcalendar_id = assignment_row[0]

            # 1. Save to peer_evaluation_submissions (source of truth)
            cur.execute(
                'INSERT INTO peer_evaluation_submissions '
                '(evaluator_id, evaluatee_id, acadcalendar_id, evaluator_name, '
                ' cat1_score, cat2_score, cat3_score, cat4_score, '
                ' final_score, strengths, growth, comments, date_submitted) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())',
                (evaluator_personnel_id, evaluatee_id, acadcalendar_id, evaluator_name,
                 cat1_avg, cat2_avg, cat3_avg, cat4_avg,
                 final_score, strengths, growth, comments))

            # 2. Mark assignment complete
            cur.execute(
                'UPDATE peer_assignments SET is_completed = TRUE '
                'WHERE evaluator_id = %s AND evaluatee_id = %s AND acadcalendar_id = %s',
                (evaluator_personnel_id, evaluatee_id, acadcalendar_id))

            # 3. Recompute average from all submissions for this evaluatee+semester
            cur.execute(
                'SELECT AVG(final_score), COUNT(*) FROM peer_evaluation_submissions '
                'WHERE evaluatee_id = %s AND acadcalendar_id = %s',
                (evaluatee_id, acadcalendar_id))
            avg_row = cur.fetchone()
            avg_peer_score = float(avg_row[0]) if avg_row and avg_row[0] else 0.0
            completed_count = int(avg_row[1]) if avg_row else 0
            expected_count = 2

            # 4. Upsert summary into faculty_evaluations (class_id = NULL for peer)
            cur.execute(
                'INSERT INTO faculty_evaluations '
                '(personnel_id, acadcalendar_id, class_id, evaluator_type, '
                ' score, total_responses, expected_responses, response_met, last_updated) '
                'VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, NOW()) '
                'ON CONFLICT (personnel_id, acadcalendar_id, evaluator_type) '
                'WHERE class_id IS NULL '
                'DO UPDATE SET score=EXCLUDED.score, total_responses=EXCLUDED.total_responses, '
                '             response_met=EXCLUDED.response_met, last_updated=NOW()',
                (evaluatee_id, acadcalendar_id, 'peer', avg_peer_score,
                 completed_count, expected_count, completed_count >= expected_count))

            conn.commit()
            print(f'Peer eval submitted: evaluator={evaluator_personnel_id} -> evaluatee={evaluatee_id}, score={final_score:.2f}')
            return redirect(url_for('faculty_evaluations'))

        except Exception as e:
            conn.rollback()
            print(f'Peer eval error: {e}')
            return f'An error occurred: {e}', 500
        finally:
            cur.close()
            return_db_connection(conn)

    # GET
    cur.execute(
        'SELECT p.firstname, p.lastname, col.collegename '
        'FROM personnel p '
        'JOIN college col ON p.college_id = col.college_id '
        'WHERE p.personnel_id = %s',
        (evaluatee_id,))
    row = cur.fetchone()
    cur.close()
    return_db_connection(conn)

    if row is None:
        return f'Employee with ID {evaluatee_id} not found.', 404

    return render_template(
        'faculty&dean/peer-eval-form.html',
        target_firstname=row[0],
        target_lastname=row[1],
        target_department=row[2],
        evaluatee_id=evaluatee_id
    )

# EDITED BY CARDS - FIXED VERSION
@app.route('/faculty_promotion')
@require_auth([20001, 20002])
def faculty_promotion():
    user_id = session.get('user_id')
    if not user_id:
        return "Unauthorized", 401
    
    conn = db_pool.get_connection()
    cursor = conn.cursor()
    
    # 1. Fetch Faculty Info (includes probationary_start_date for accurate countdown)
    cursor.execute("""
        SELECT
            p.personnel_id, p.hiredate, pr.position, p.firstname, p.lastname,
            p.honorifics, c.collegename, pr.profilepic, pr.employmentstatus,
            (SELECT acadcalendar_id FROM acadcalendar
             WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
             ORDER BY semesterstart DESC LIMIT 1),
            pr.has_doctorate, pr.has_aligned_master,
            pr.probationary_start_date
        FROM personnel p
        LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
        LEFT JOIN college c ON p.college_id = c.college_id
        WHERE p.user_id = %s
    """, (user_id,))

    result = cursor.fetchone()
    if not result:
        cursor.close()
        db_pool.return_connection(conn)
        return "Faculty record not found", 400

    (faculty_id, hire_date, current_rank, firstname, lastname,
     honorifics, college, profilepic, employment_status, current_term_id,
     has_doctorate, has_aligned_master,
     probationary_start_date) = result

    # 2. SERVICE CALCULATION
    from datetime import date
    today = date.today()
    total_days = 0
    years_decimal = 0

    if hire_date:
        delta = today - hire_date
        total_days = max(0, delta.days)
        years_decimal = total_days / 365.25

    # 3. SPC REGULARIZATION LOGIC
    # Countdown uses probationary_start_date if set, else falls back to hire_date.
    # A faculty member only enters probation once they hold an aligned Master's.
    reg_percent = 0
    reg_status_label = employment_status or "Contractual"
    reg_message = ""

    # Extra detail vars for the breakdown display
    _prob_days = 0
    _days_remaining_regular = 1095
    _expected_regular_date = None
    _prob_start_date = None

    if employment_status in ["Regular", "Tenured"]:
        reg_percent = 100
        reg_message = "Regular status achieved."
        _prob_days = 1095
        _days_remaining_regular = 0
    elif not has_aligned_master:
        reg_status_label = "Contractual"
        reg_percent = 0
        reg_message = "Status: Contractual. An aligned Master's Degree is required to begin the 3-year probationary period."
    else:
        reg_status_label = "Probationary"
        prob_start = probationary_start_date if probationary_start_date else hire_date
        _prob_start_date = prob_start
        _prob_days = max(0, (today - prob_start).days) if prob_start else 0
        reg_percent = min(round((_prob_days / 1095) * 100, 1), 100)  # 1095 = 3 years
        reg_message = f"Probationary Progress: {_prob_days} days completed out of 1,095 ({reg_percent}%)."
        _days_remaining_regular = max(0, 1095 - _prob_days)
        if prob_start and _days_remaining_regular > 0:
            from datetime import timedelta as _td
            _expected_regular_date = prob_start + _td(days=1095)
        prob_days = _prob_days  # keep for any existing references below

    # 4. RANK ELIGIBILITY (SPC Promotion Table)
    # Associate Instructor : Bachelor's + 3 yrs
    # Instructor           : Master's   + 4 yrs
    # Assistant Professor  : Master's   + 5 yrs
    # Associate Professor  : Doctorate  + 9 yrs
    # Professor            : Doctorate  + 10 yrs
    RANK_ORDER = ["Associate Instructor", "Instructor", "Assistant Professor", "Associate Professor", "Professor"]
    YEAR_REQS  = {"Associate Instructor": 3, "Instructor": 4, "Assistant Professor": 5,
                  "Associate Professor": 9, "Professor": 10}
    DEGREE_REQS = {
        "Associate Instructor": "bachelor",
        "Instructor": "master",
        "Assistant Professor": "master",
        "Associate Professor": "doctorate",
        "Professor": "doctorate",
    }

    lock_reasons = []
    available_ranks = []

    # Promotion window: June 1 – August 31
    # DEV: window check disabled for testing
    # if not (6 <= today.month <= 8):
    #     lock_reasons.append(
    #         f"Submission window closed. Promotion applications are only accepted June 1 – August 31 each year."
    #     )

    # Active application check
    cursor.execute(
        "SELECT COUNT(*) FROM promotion_application WHERE faculty_id = %s AND final_decision IS NULL",
        (faculty_id,)
    )
    has_active_app = cursor.fetchone()[0] > 0
    if has_active_app:
        lock_reasons.append("You already have an active promotion application under review.")

    # Attendance rate check (current semester, minimum 80%)
    attendance_rate = None
    if current_term_id:
        cursor.execute("""
            SELECT CASE
                WHEN COUNT(ar.attendancereport_id) > 0
                THEN ROUND(AVG(ar.attendancerate)::numeric, 2)
                ELSE NULL
            END
            FROM attendancereport ar
            WHERE ar.personnel_id = %s AND ar.acadcalendar_id = %s
        """, (faculty_id, current_term_id))
        att_row = cursor.fetchone()
        if att_row and att_row[0] is not None:
            attendance_rate = float(att_row[0])

    if attendance_rate is not None and attendance_rate < 80.0:
        lock_reasons.append(
            f"Acceptable attendance rate not met. Your current attendance rate is {attendance_rate:.1f}% (minimum required: 80%)."
        )

    # Evaluation score check (current semester, weighted: student 55% / supervisor 35% / peer 10%, minimum 3.0)
    eval_score = None
    if current_term_id:
        cursor.execute("""
            SELECT COALESCE(
                SUM(CASE WHEN evaluator_type = 'student'    THEN avg_score * 0.55 ELSE 0 END) +
                SUM(CASE WHEN evaluator_type = 'supervisor' THEN avg_score * 0.35 ELSE 0 END) +
                SUM(CASE WHEN evaluator_type = 'peer'       THEN avg_score * 0.10 ELSE 0 END),
            0)
            FROM (
                SELECT evaluator_type, AVG(score) AS avg_score
                FROM faculty_evaluations
                WHERE personnel_id = %s AND acadcalendar_id = %s
                GROUP BY evaluator_type
            ) scores
        """, (faculty_id, current_term_id))
        eval_row = cursor.fetchone()
        if eval_row and eval_row[0] is not None and float(eval_row[0]) > 0:
            eval_score = float(eval_row[0])

    if eval_score is not None and eval_score < 3.0:
        lock_reasons.append(
            f"Evaluation score requirement not met. Your current evaluation score is {eval_score:.2f} (minimum required: 3.0)."
        )

    # Target rank determination
    target_idx = RANK_ORDER.index(current_rank) + 1 if current_rank in RANK_ORDER else 0
    if target_idx >= len(RANK_ORDER):
        pass  # Already at highest rank — available_ranks stays empty
    else:
        target_rank = RANK_ORDER[target_idx]
        req_years  = YEAR_REQS[target_rank]
        req_degree = DEGREE_REQS[target_rank]

        rank_lock_reasons = []

        # Degree check (doctorate satisfies master's requirement)
        if req_degree == "doctorate" and not has_doctorate:
            rank_lock_reasons.append(f"{target_rank} requires a Doctorate Degree.")
        elif req_degree == "master" and not (has_aligned_master or has_doctorate):
            rank_lock_reasons.append(f"{target_rank} requires an aligned Master's Degree.")

        # Experience check (years from hire date)
        if years_decimal < req_years:
            rank_lock_reasons.append(
                f"{target_rank} requires {req_years} years of teaching "
                f"(current: {years_decimal:.1f} yrs)."
            )

        lock_reasons.extend(rank_lock_reasons)
        if not rank_lock_reasons and not has_active_app:
            available_ranks = [target_rank]
    # 5. Fetch Active Application and History
    cursor.execute("""
        SELECT application_id, current_status, date_submitted, hrmd_approval_date, vpa_approval_date, pres_approval_date,
               final_decision, resume, cover_letter, resume_filename, cover_letter_filename, requested_rank,
               tor_filename, tor, diploma_filename, diploma
        FROM promotion_application
        WHERE faculty_id = %s AND final_decision IS NULL
        ORDER BY date_submitted DESC LIMIT 1
    """, (faculty_id,))
    row = cursor.fetchone()

    # Fetch latest application (including finalized) for stepper display
    cursor.execute("""
        SELECT application_id, current_status, date_submitted, hrmd_approval_date, vpa_approval_date, pres_approval_date,
               final_decision, requested_rank, letter_acknowledged
        FROM promotion_application
        WHERE faculty_id = %s
        ORDER BY date_submitted DESC LIMIT 1
    """, (faculty_id,))
    latest_row = cursor.fetchone()

    cursor.execute("""
        SELECT years_of_service, current_status, hrmd_endorsement_date, vpa_recommendation_date, pres_approval_date, final_decision, date_initiated
        FROM regularization_application WHERE faculty_id = %s AND final_decision IS NULL ORDER BY date_initiated DESC LIMIT 1
    """, (faculty_id,))
    reg_app = cursor.fetchone()
    
    # --- STEP 5: Updated Application History Query ---
    cursor.execute("""
        SELECT 
            date_submitted, current_status, final_decision, 
            hrmd_approval_date, vpa_approval_date, pres_approval_date, 
            requested_rank, rejection_reason 
        FROM promotion_application 
        WHERE faculty_id = %s 
        ORDER BY date_submitted DESC
    """, (faculty_id,))

    _STAGE_LABELS = {
        'hrmd': 'Under HR Review',
        'vpa': 'Under VPA Review',
        'pres': 'Awaiting Presidential Approval',
    }
    application_history = []
    for h in cursor.fetchall():
        _status  = (h[1] or '').lower()
        _decision = h[2]
        # Treat current_status='rejected' as a fallback for legacy rows
        # where final_decision was not set to 0.
        if _decision == 1 or _status == 'approved':
            display_final = 'Approved'
        elif _decision == 0 or _status == 'rejected':
            display_final = 'Rejected'
        else:
            display_final = 'Pending'

        # Identify which stage the rejection occurred at by checking
        # which approval timestamps are still NULL.
        if display_final == 'Rejected':
            if not h[3]:       # hrmd_approval_date is NULL → rejected at HR stage
                rejected_at = 'hrmd'
            elif not h[4]:     # vpa_approval_date is NULL → rejected at VPA stage
                rejected_at = 'vpa'
            else:              # both set → rejected at President stage
                rejected_at = 'pres'
        else:
            rejected_at = ''

        application_history.append({
            'date_submitted': h[0],
            'current_status': _status,
            'stage_label': _STAGE_LABELS.get(_status, _status.title() if _status else 'Processing'),
            'final_decision': display_final,
            'rejected_at_stage': rejected_at,
            'hrmd_date': h[3].strftime('%Y-%m-%d') if h[3] else None,
            'vpa_date': h[4].strftime('%Y-%m-%d') if h[4] else None,
            'pres_date': h[5].strftime('%Y-%m-%d') if h[5] else None,
            'requested_position': h[6],
            'remarks': (
                (h[7] or 'No reason provided.')
                if display_final == 'Rejected'
                else (f"Congratulations! Your promotion to {h[6]} has been approved."
                      if display_final == 'Approved'
                      else 'Application is currently under review.')
            )
        })

    # 6. Prepare Final Template Data
    import base64
    profile_img = f"data:image/jpeg;base64,{base64.b64encode(bytes(profilepic)).decode('utf-8')}" if profilepic else ''
    
    at_highest_rank = current_rank == 'Professor'

    # DEV: disable all promotion locks for testing
    lock_reasons.clear()
    if not available_ranks and not at_highest_rank:
        # determine target rank so the form has something to submit
        target_idx = RANK_ORDER.index(current_rank) + 1 if current_rank in RANK_ORDER else 0
        if target_idx < len(RANK_ORDER):
            available_ranks = [RANK_ORDER[target_idx]]

    # Stepper display vars — always sourced from latest_row so they persist after approval/rejection
    _STAGE_STATUS = {
        'hrmd': 'Under HR Review',
        'vpa': 'Under VPA Review',
        'pres': 'Awaiting Presidential Approval',
        'approved': 'Approved',
        'rejected': 'Rejected',
    }
    if latest_row:
        _lr_status   = (latest_row[1] or '').lower()
        _lr_decision = latest_row[6]
        if _lr_decision == 1 or _lr_status == 'approved':
            _display_decision = 'approved'
        elif _lr_decision == 0 or _lr_status == 'rejected':
            _display_decision = 'rejected'
        else:
            _display_decision = None
        display_date_submitted  = latest_row[2]
        display_hrmd_date       = latest_row[3]
        display_vpa_date        = latest_row[4]
        display_pres_date       = latest_row[5]
        display_current_status  = _STAGE_STATUS.get(_lr_status, _lr_status.title() if _lr_status else '')
        display_requested_rank  = latest_row[7]
        display_final_decision  = _display_decision
        has_unacknowledged_letter = (_lr_decision == 1 and latest_row[8] is False)
        letter_app_id           = latest_row[0] if _lr_decision == 1 else None
    else:
        display_date_submitted = display_hrmd_date = display_vpa_date = display_pres_date = None
        display_current_status = display_requested_rank = display_final_decision = None
        has_unacknowledged_letter = False
        letter_app_id = None

    template_data = {
        'faculty_name': f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}",
        'college': college or 'College of Computer Studies',
        'profile_image_base64': profile_img,
        'regularization_percentage': reg_percent,
        'regularization_status': reg_status_label,
        'regularization_message': reg_message,
        'tenure_type': reg_status_label,
        'years_employed': round(years_decimal, 1),
        'hire_date': hire_date,
        'prob_days': _prob_days,
        'prob_days_required': 1095,
        'days_remaining_regular': _days_remaining_regular,
        'expected_regular_date': _expected_regular_date,
        'has_aligned_master': bool(has_aligned_master),
        'prob_start_date': _prob_start_date,
        'total_service_days': total_days,
        'current_rank': current_rank,
        'available_ranks': available_ranks,
        'application_history': application_history,
        'lock_reasons': lock_reasons,
        'at_highest_rank': at_highest_rank,
        'can_apply_for_promotion': len(lock_reasons) == 0 and len(available_ranks) > 0,
        'attendance_rate': attendance_rate,
        'eval_score': eval_score,
        'application_id': row[0] if row else None,
        'regularization_status_data': {
            'requested_tenure': "Tenured" if float(reg_app[0] or years_decimal) >= 7 else "Regular",
            'current_status': reg_app[1], 'hrmd_date': reg_app[2], 'vpa_date': reg_app[3], 'pres_date': reg_app[4]
        } if reg_app else None,
        # Stepper display (always shows latest application state)
        'display_date_submitted': display_date_submitted,
        'display_hrmd_date': display_hrmd_date,
        'display_vpa_date': display_vpa_date,
        'display_pres_date': display_pres_date,
        'display_current_status': display_current_status,
        'display_requested_rank': display_requested_rank,
        'display_final_decision': display_final_decision,
        'has_unacknowledged_letter': has_unacknowledged_letter,
        'letter_app_id': letter_app_id,
    }

    if row:
        template_data.update({
            'current_status': row[1],
            'date_submitted': row[2],
            'hrmd_approval_date': row[3],
            'vpa_approval_date': row[4],
            'pres_approval_date': row[5],
            'resume_filename': row[9],
            'cover_letter_filename': row[10],
            'requested_rank': row[11],
            'upload_locked': row[1] in ['hrmd', 'vpa', 'pres'],
            'tor_filename': row[12],
            'diploma_filename': row[14],
            'has_tor': row[13] is not None,
            'has_diploma': row[15] is not None
        })

    cursor.close()
    db_pool.return_connection(conn)
    return render_template('faculty&dean/faculty-promotion.html', **template_data)

@app.route('/faculty_profile')
@require_auth([20001, 20002, 20003])
def faculty_profile():
    faculty_info = get_faculty_info(session['user_id'])
    return render_template('faculty&dean/faculty-profile.html', **faculty_info)

@app.route('/faculty_settings')
@require_auth([20001, 20002])
def faculty_settings():
    faculty_info = get_faculty_info(session['user_id'])
    return render_template('faculty&dean/faculty-settings.html', **faculty_info)

# HR/Admin routes
@app.route('/hr_dashboard')
@require_auth([20003])
def hr_dashboard():
    personnel_info = get_personnel_info(session['user_id'])
    return render_template('hrmd/hr-dashboard.html', **personnel_info)

@app.route('/hr_employees')
@require_auth([20003])
def hr_employees():
    personnel_info = get_personnel_info(session['user_id'])
    personnel_info['today_date'] = datetime.now().strftime('%Y-%m-%d')
    return render_template('hrmd/hr-employees.html', **personnel_info)

@app.route('/hr_employees_return')
@require_auth([20003])
def hr_employees_return():
    session.pop('viewing_personnel_id', None)
    return redirect(url_for('hr_employees'))

@app.route('/hr_evaluations')
@require_auth([20003])
def hr_evaluations():
    personnel_info = get_personnel_info(session['user_id'])
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(
                (SELECT fe.acadcalendar_id
                 FROM faculty_evaluations fe
                 JOIN acadcalendar ac ON ac.acadcalendar_id = fe.acadcalendar_id
                 GROUP BY fe.acadcalendar_id, ac.semesterstart
                 ORDER BY ac.semesterstart DESC
                 LIMIT 1),
                (SELECT acadcalendar_id FROM acadcalendar
                 WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                 ORDER BY semesterstart DESC LIMIT 1),
                (SELECT acadcalendar_id FROM acadcalendar
                 ORDER BY semesterstart DESC LIMIT 1)
            )
        """)
        row = cursor.fetchone()
        current_term_id = row[0] if row and row[0] else 80001
        cursor.close()
        db_pool.return_connection(conn)
    except Exception as e:
        print(f"Warning: could not resolve current term: {e}")
        current_term_id = 80001
    return render_template('hrmd/hr-evaluations.html', acadcalendar_id=current_term_id, **personnel_info)

@app.route('/api/hr/evaluation-dashboard-data')
@require_auth([20003])
def api_hr_evaluation_dashboard_data():
    """
    Fetches aggregated evaluation data for the HR dashboard KPIs and charts.
    FIXED: Removed the invalid GROUP BY clause and simplified the final SELECT logic.
    """
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        # 1. Determine the current academic calendar ID
        cursor.execute("""
            SELECT acadcalendar_id
            FROM acadcalendar 
            WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
            ORDER BY semesterstart DESC LIMIT 1
        """)
        current_term_result = cursor.fetchone()
        current_term_id = current_term_result[0] if current_term_result else '80001' # Fallback
        
        # 2. Fetch all necessary data using CTEs
        # The main SELECT statement is restructured to pull aggregated data from the CTEs directly.
        cursor.execute("""
            WITH faculty_eval_scores AS (
                SELECT 
                    p.personnel_id,
                    c.collegename,
                    -- Weighted Overall Score (55/35/10)
                    COALESCE(
                        SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) +
                        SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) +
                        SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END),
                    0) AS overall_score
                FROM personnel p
                LEFT JOIN faculty_evaluations fe ON fe.personnel_id = p.personnel_id AND fe.acadcalendar_id = %s
                LEFT JOIN college c ON p.college_id = c.college_id
                WHERE p.role_id IN (20001, 20002)
                GROUP BY p.personnel_id, c.collegename
            ),
            valid_eval_breakdown AS (
                SELECT 
                    overall_score,
                    collegename,
                    CASE 
                        WHEN overall_score >= 3.0 THEN 'Above Average'
                        WHEN overall_score >= 2.0 THEN 'Average'
                        WHEN overall_score > 0 THEN 'Below Average'
                        ELSE 'Not Rated'
                    END AS rating_group
                FROM faculty_eval_scores
                WHERE overall_score > 0
            ),
            final_aggregates AS (
                SELECT
                    -- KPI: Average Evaluation Score
                    COALESCE(AVG(overall_score), 0) AS avg_eval_score,
                    
                    -- Chart Data: Rating Breakdown
                    (SELECT json_agg(json_build_object('rating_group', rating_group, 'count', rating_count))
                     FROM (
                        SELECT rating_group, COUNT(*) AS rating_count
                        FROM valid_eval_breakdown
                        GROUP BY rating_group
                     ) AS breakdown_counts) AS rating_counts_json,
                    
                    -- Chart Data: Top Departments
                    (SELECT json_agg(json_build_object('department', collegename, 'avg_score', dept_avg))
                     FROM (
                        SELECT collegename, AVG(overall_score) AS dept_avg
                        FROM faculty_eval_scores
                        WHERE overall_score > 0
                        GROUP BY collegename
                        ORDER BY dept_avg DESC
                        LIMIT 5
                     ) AS dept_averages) AS dept_scores_json
                
                FROM faculty_eval_scores
            )
            SELECT * FROM final_aggregates;
        """, (current_term_id,))
        
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if not result or result[0] is None:
            return jsonify({
                'success': True,
                'kpi_avg_eval': 'N/A',
                'rating_breakdown': [],
                'top_departments': []
            })
            
        avg_eval_score, rating_counts_json, dept_scores_json = result
        
        # --- Process Rating Breakdown for Doughnut Chart ---
        rating_data = {
            'Above Average': 0, 'Average': 0, 'Below Average': 0
        }
        if rating_counts_json:
            for item in rating_counts_json:
                if item and item['rating_group'] in rating_data:
                    rating_data[item['rating_group']] = item['count']
        
        # --- Process Top Departments for Bar Chart ---
        top_departments = []
        if dept_scores_json:
            # Structure for chart
            seen_departments = set()
            for item in dept_scores_json:
                if item and item['department'] not in seen_departments:
                    top_departments.append({
                        'department': item['department'],
                        'score': round(float(item['avg_score']), 2)
                    })
                    seen_departments.add(item['department'])
        
        # Sort the final list to ensure the top departments are always ordered by score
        top_departments.sort(key=lambda x: x['score'], reverse=True)
        
        return jsonify({
            'success': True,
            'kpi_avg_eval': float(avg_eval_score),
            'rating_breakdown': [
                rating_data['Above Average'],
                rating_data['Average'],
                rating_data['Below Average']
            ],
            'top_departments': top_departments
        })
        
    except Exception as e:
        print(f"Error fetching evaluation dashboard data: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hr/evaluations', methods=['GET'])
@require_auth([20003])
def api_hr_evaluations():
    term = request.args.get('term')
    dept = request.args.get('dept', '')
    status = request.args.get('status', '') 
    search = request.args.get('search', '')
    position_filter = request.args.get('position', '') 
    response_rate_filter = request.args.get('response_rate', '') 
    
    conn = db_pool.get_connection()
    cursor = conn.cursor()

    # Resolve term ID: use filter param if provided, otherwise find the most
    # recent semester that has evaluation data (not necessarily today's semester).
    if term:
        try:
            current_term_id = int(term)
        except (ValueError, TypeError):
            current_term_id = 80001
    else:
        try:
            cursor.execute("""
                SELECT COALESCE(
                    (SELECT fe.acadcalendar_id
                     FROM faculty_evaluations fe
                     JOIN acadcalendar ac ON ac.acadcalendar_id = fe.acadcalendar_id
                     GROUP BY fe.acadcalendar_id, ac.semesterstart
                     ORDER BY ac.semesterstart DESC LIMIT 1),
                    (SELECT acadcalendar_id FROM acadcalendar
                     WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                     ORDER BY semesterstart DESC LIMIT 1),
                    (SELECT acadcalendar_id FROM acadcalendar
                     ORDER BY semesterstart DESC LIMIT 1)
                )
            """)
            row = cursor.fetchone()
            current_term_id = row[0] if row and row[0] else 80001
        except Exception:
            current_term_id = 80001
    
    # Fetch academic calendar display information (reuse existing cursor)
    cursor.execute("""
        SELECT semester, acadyear, semesterend
        FROM acadcalendar WHERE acadcalendar_id = %s
    """, (current_term_id,))
    cal_row = cursor.fetchone()
    if cal_row:
        sem_name, acad_year, sem_end = cal_row
        if 'semester' not in sem_name.lower():
            sem_name = f"{sem_name} Semester"
        year_clean = acad_year.replace('AY ', '').replace('AY', '').strip()
        acadcalendar_info = {
            'semester_name': sem_name,
            'acad_year': year_clean,
            'deadline': sem_end.strftime('%b %d, %Y') if sem_end else 'N/A',
            'display': f"📅 {sem_name} — AY {year_clean}",
        }
    else:
        acadcalendar_info = {
            'semester_name': 'N/A', 'acad_year': 'N/A',
            'deadline': 'N/A', 'display': '📅 N/A — AY N/A',
        }

    # --- KPI 1, 2, 3, & 4 Calculation (Combined Query) ---
    cursor.execute("""
        WITH faculty_data AS (
            SELECT
                p.personnel_id,
                p.college_id,
                -- Weighted Overall Score: partial if not all 3 components present
                COALESCE(
                    COALESCE(AVG(CASE WHEN fe.evaluator_type = 'student'    THEN fe.score END), 0) * 0.55 +
                    COALESCE(MAX(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score END), 0) * 0.35 +
                    COALESCE(MAX(CASE WHEN fe.evaluator_type = 'peer'       THEN fe.score END), 0) * 0.10,
                0) AS overall_score,
                -- Score completeness flag
                (CASE WHEN MAX(CASE WHEN fe.evaluator_type = 'student'    THEN 1 END) = 1
                       AND MAX(CASE WHEN fe.evaluator_type = 'supervisor' THEN 1 END) = 1
                       AND MAX(CASE WHEN fe.evaluator_type = 'peer'       THEN 1 END) = 1
                  THEN TRUE ELSE FALSE END) AS score_complete,
                -- Student Response Count
                COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.total_responses ELSE 0 END), 0) AS student_responses_count,
                -- Total enrolled students across all classes this term
                COALESCE((
                    SELECT SUM(s.student_count)
                    FROM schedule s
                    WHERE s.personnel_id = p.personnel_id
                      AND s.acadcalendar_id = %s
                      AND s.student_count IS NOT NULL
                ), 0) AS total_students
            FROM personnel p
            LEFT JOIN faculty_evaluations fe ON fe.personnel_id = p.personnel_id AND fe.acadcalendar_id = %s
            WHERE p.role_id = 20001
            GROUP BY p.personnel_id, p.college_id
        ),
        department_avg AS (
            SELECT
                fd.college_id,
                AVG(fd.overall_score) AS dept_avg_score
            FROM faculty_data fd
            WHERE fd.overall_score > 0
            GROUP BY fd.college_id
            ORDER BY dept_avg_score DESC
            LIMIT 1
        )
        SELECT
            -- General KPIs
            (SELECT COUNT(fd.personnel_id) FROM faculty_data fd) AS total_faculty,
            (SELECT COALESCE(AVG(fd.overall_score), 0) FROM faculty_data fd) AS average_rating,
            -- Met response rate: faculty whose responses >= threshold based on total enrolled students
            (SELECT SUM(CASE
                WHEN fd.total_students = 0 THEN 0
                WHEN fd.student_responses_count >= fd.total_students * CASE
                    WHEN fd.total_students <= 25  THEN 0.80
                    WHEN fd.total_students <= 50  THEN 0.66
                    WHEN fd.total_students <= 100 THEN 0.50
                    WHEN fd.total_students <= 200 THEN 0.33
                    ELSE 0.25
                END THEN 1
                ELSE 0
            END) FROM faculty_data fd) AS met_response_rate_count,
            (SELECT COUNT(fd.personnel_id) FROM faculty_data fd) AS faculty_with_data,

            -- Leading Department KPI
            c.collegename AS best_department_name
        FROM department_avg da
        JOIN college c ON da.college_id = c.college_id
    """, (current_term_id, current_term_id))
    
    kpi_results = cursor.fetchone()
    
    # Map results
    if kpi_results:
        kpis = {
            "total_faculty": kpi_results[0],
            "avg_rating": kpi_results[1],
            "met_response_rate_count": kpi_results[2],
            "faculty_with_data": kpi_results[3],
            "best_department_name": kpi_results[4] if kpi_results[4] else "N/A"
        }
    else:
         kpis = {
            "total_faculty": 0,
            "avg_rating": 0,
            "met_response_rate_count": 0,
            "faculty_with_data": 0,
            "best_department_name": "N/A"
        }
    # --- END KPI CALCULATION ---
    
    # --- TABLE DATA RETRIEVAL (Modified) ---
    query = """
        SELECT 
            p.personnel_id, 
            CONCAT(p.lastname, ', ', p.firstname) as name,
            c.collegename,
            pr.position,
            
            -- METRIC 1 (Response Rate): Student Response Count ONLY
            COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.total_responses ELSE 0 END), 0) AS student_responses_count,
            -- Total enrolled students across all classes this term
            COALESCE((
                SELECT SUM(s.student_count)
                FROM schedule s
                WHERE s.personnel_id = p.personnel_id
                  AND s.acadcalendar_id = %s
                  AND s.student_count IS NOT NULL
            ), 0) AS total_students,
            
            -- METRIC 2: Weighted Overall Score (partial if missing components)
            COALESCE(
                COALESCE(AVG(CASE WHEN fe.evaluator_type = 'student'    THEN fe.score END), 0) * 0.55 +
                COALESCE(MAX(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score END), 0) * 0.35 +
                COALESCE(MAX(CASE WHEN fe.evaluator_type = 'peer'       THEN fe.score END), 0) * 0.10,
            0) AS overall_score,
            -- Individual Scores
            COALESCE(AVG(CASE WHEN fe.evaluator_type = 'student'    THEN fe.score END), 0) AS student_score,
            COALESCE(MAX(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score END), 0) AS supervisor_score,
            COALESCE(MAX(CASE WHEN fe.evaluator_type = 'peer'       THEN fe.score END), 0) AS peer_score,
            -- Completeness flag: TRUE only when all 3 evaluator types present
            (CASE WHEN MAX(CASE WHEN fe.evaluator_type = 'student'    THEN 1 END) = 1
                   AND MAX(CASE WHEN fe.evaluator_type = 'supervisor' THEN 1 END) = 1
                   AND MAX(CASE WHEN fe.evaluator_type = 'peer'       THEN 1 END) = 1
              THEN TRUE ELSE FALSE END) AS score_complete,
            -- Store position for separate filtering/list
            pr.position AS faculty_position
            
        FROM personnel p
        LEFT JOIN faculty_evaluations fe ON fe.personnel_id = p.personnel_id AND fe.acadcalendar_id = %s
        LEFT JOIN college c ON p.college_id = c.college_id
        LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
        WHERE 1=1 AND p.role_id = 20001
    """
    params = [current_term_id, current_term_id]  # [total_students subquery, fe.acadcalendar_id]

    # Dynamic filtering
    if dept:
        query += " AND c.collegename = %s"
        params.append(dept)
    if search:
        query += " AND (LOWER(p.firstname) LIKE %s OR LOWER(p.lastname) LIKE %s)"
        like = f"%{search.lower()}%"
        params.extend([like, like])
        
    # Position Filter
    if position_filter:
        query += " AND pr.position = %s"
        params.append(position_filter)
        
    query += " GROUP BY p.personnel_id, name, c.collegename, pr.position"
    
    # --- Status Filtering (Applied to the aggregated data) ---
    if status or response_rate_filter: 
        query += " HAVING 1=1" 
    
    # Apply Rating Status Filter
    if status:
        if status == 'above-average':
            query += " AND COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) + SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) + SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END), 0) >= 3.0"
        elif status == 'average':
            query += " AND COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) + SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) + SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END), 0) >= 2.0 AND COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) + SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) + SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END), 0) < 3.0"
        elif status == 'below-average':
            query += " AND COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) + SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) + SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END), 0) > 0.0 AND COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) + SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) + SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END), 0) < 2.0"
            
    # Response Rate Filter is applied in Python after fetch (dynamic threshold based on total_students)
            
    query += " ORDER BY overall_score DESC"

    cursor.execute(query, tuple(params))
    evaluations = cursor.fetchall()

    # Dynamic response rate post-filter
    def _response_threshold(total_students):
        if total_students <= 25:  return 0.80
        if total_students <= 50:  return 0.66
        if total_students <= 100: return 0.50
        if total_students <= 200: return 0.33
        return 0.25

    if response_rate_filter in ('met', 'not-met'):
        filtered = []
        for row in evaluations:
            responses   = row[4]   # student_responses_count
            total_stud  = row[5] or 0  # total_students
            if total_stud > 0:
                threshold = _response_threshold(total_stud)
                met = responses >= total_stud * threshold
            else:
                met = False
            if (response_rate_filter == 'met' and met) or (response_rate_filter == 'not-met' and not met):
                filtered.append(row)
        evaluations = filtered

    # Get all unique positions for the frontend filter dropdown
    cursor.execute("""
        SELECT DISTINCT pr.position
        FROM personnel p
        JOIN profile pr ON p.personnel_id = pr.personnel_id
        WHERE p.role_id = 20001 AND pr.position IS NOT NULL
        ORDER BY pr.position
    """)
    unique_positions = [row[0] for row in cursor.fetchall() if row[0] and row[0].strip() != '']
    
    cursor.close()
    db_pool.return_connection(conn)

    # Shape results to JSON
    # IMPORTANT: Ensure the column indices match the SQL SELECT statement (9 total columns before position)
    evals = [{
        "personnelid": row[0],
        "name": row[1],
        "department": row[2],
        "position": row[3],
        "studentresponses": row[4],
        "total_students": int(row[5]) if row[5] else 0,
        "avgscore": row[6],
        "student_score": row[7],
        "supervisor_score": row[8],
        "peer_score": row[9],
        "score_complete": bool(row[10]),
    } for row in evaluations]

    # Combine table data and KPIs into the final JSON response
    return jsonify(
        success=True, 
        evaluations=evals, 
        kpis=kpis,
        unique_positions=unique_positions,
        acadcalendar_info=acadcalendar_info # Dynamic term info
    )


@app.route('/api/hr/update-evaluation-score', methods=['POST'])
@require_auth([20003])
def api_hr_update_evaluation_score():
    """Manually update a specific evaluation score (e.g., Peer Score)"""
    try:
        data = request.get_json()
        updates = data.get('updates', [])
        term_id = data.get('term_id')
        
        if not updates or not term_id:
            return jsonify({'success': False, 'error': 'Missing updates or term ID.'}), 400
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        user_id = session['user_id']
        hr_personnel_info = get_personnel_info(user_id)
        hr_personnel_id = hr_personnel_info.get('personnel_id')
        
        updated_count = 0
        
        for update in updates:
            personnel_id = update.get('personnel_id')
            peer_score = update.get('peer_score')
            
            if personnel_id and peer_score is not None:
                # 1. Fetch current score and name for logging
                cursor.execute("""
                    SELECT fe.score, CONCAT(p.lastname, ', ', p.firstname)
                    FROM personnel p
                    LEFT JOIN faculty_evaluations fe ON fe.personnel_id = p.personnel_id AND fe.acadcalendar_id = %s AND fe.evaluator_type = 'peer'
                    WHERE p.personnel_id = %s
                """, (term_id, personnel_id))
                
                result = cursor.fetchone()
                current_score = result[0] if result and result[0] is not None else 0.0
                faculty_name = result[1] if result else "Unknown Faculty"
                
                # 2. Perform INSERT or UPDATE (ON CONFLICT)
                cursor.execute("""
                    INSERT INTO faculty_evaluations (
                        personnel_id, acadcalendar_id, evaluator_type, score, total_responses, last_updated, class_id
                    ) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, 0)
                    ON CONFLICT (personnel_id, acadcalendar_id, class_id, evaluator_type)
                    DO UPDATE SET
                        score = EXCLUDED.score,
                        last_updated = CURRENT_TIMESTAMP
                """, (
                    personnel_id,
                    term_id,
                    'peer',
                    float(peer_score),
                    1 # Manually set responses to 1 for manual entry to count
                ))
                updated_count += 1
                
                # 3. Log Audit Action
                log_audit_action(
                    hr_personnel_id,
                    "Manual Evaluation Score Update",
                    f"HR manually set Peer Score for {faculty_name} (Term ID: {term_id})",
                    before_value=f"Peer Score: {float(current_score):.2f}",
                    after_value=f"Peer Score: {float(peer_score):.2f}"
                )
                
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        
        return jsonify({
            'success': True,
            'message': f'Successfully updated {updated_count} Peer Score record(s).',
            'updated_count': updated_count
        })
        
    except Exception as e:
        print(f"Error updating evaluation score: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hr/evaluation-trends', methods=['GET'])
@require_auth([20003])
def api_hr_evaluation_trends():
    personnel_id = request.args.get('personnel_id', '').strip()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        if personnel_id:
            # Per-faculty mode: scores for one faculty member across all semesters
            cur.execute("""
                SELECT
                    ac.acadcalendar_id,
                    ac.semester,
                    ac.acadyear,
                    ac.semesterstart,
                    MAX(CASE WHEN fe.evaluator_type = 'student'    THEN fe.score END) AS avg_student,
                    MAX(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score END) AS avg_supervisor,
                    MAX(CASE WHEN fe.evaluator_type = 'peer'       THEN fe.score END) AS avg_peer
                FROM faculty_evaluations fe
                JOIN acadcalendar ac ON fe.acadcalendar_id = ac.acadcalendar_id
                WHERE fe.personnel_id = %s
                GROUP BY ac.acadcalendar_id, ac.semester, ac.acadyear, ac.semesterstart
                ORDER BY ac.semesterstart ASC
            """, (personnel_id,))
        else:
            # Aggregate mode: institution-wide average per semester
            cur.execute("""
                SELECT
                    ac.acadcalendar_id,
                    ac.semester,
                    ac.acadyear,
                    ac.semesterstart,
                    AVG(CASE WHEN fe.evaluator_type = 'student'    THEN fe.score END) AS avg_student,
                    AVG(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score END) AS avg_supervisor,
                    AVG(CASE WHEN fe.evaluator_type = 'peer'       THEN fe.score END) AS avg_peer
                FROM faculty_evaluations fe
                JOIN acadcalendar ac ON fe.acadcalendar_id = ac.acadcalendar_id
                JOIN personnel p ON fe.personnel_id = p.personnel_id
                WHERE p.role_id = 20001
                GROUP BY ac.acadcalendar_id, ac.semester, ac.acadyear, ac.semesterstart
                ORDER BY ac.semesterstart ASC
            """)

        rows = cur.fetchall()
        trends = []
        for row in rows:
            sem_label = row[1]
            short = '1st Sem' if 'First' in sem_label else ('2nd Sem' if 'Second' in sem_label else 'Summer')
            year = row[2].replace('AY ', '') if row[2] else ''
            label = f"{short} AY {year}"

            avg_s  = float(row[4]) if row[4] is not None else None
            avg_sv = float(row[5]) if row[5] is not None else None
            avg_p  = float(row[6]) if row[6] is not None else None

            weights = []
            if avg_s  is not None: weights.append((avg_s,  0.55))
            if avg_sv is not None: weights.append((avg_sv, 0.35))
            if avg_p  is not None: weights.append((avg_p,  0.10))

            if weights:
                total_w = sum(w for _, w in weights)
                avg_overall = round(sum(v * w for v, w in weights) / total_w, 3)
            else:
                avg_overall = None

            trends.append({
                'label':      label,
                'overall':    avg_overall,
                'student':    round(avg_s,  3) if avg_s  is not None else None,
                'supervisor': round(avg_sv, 3) if avg_sv is not None else None,
                'peer':       round(avg_p,  3) if avg_p  is not None else None,
            })

        return jsonify({'success': True, 'trends': trends})
    except Exception as e:
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/hr/dept-trends', methods=['GET'])
@require_auth([20003])
def api_hr_dept_trends():
    """Per-department weighted avg score per semester, ordered chronologically."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                ac.acadcalendar_id,
                ac.semester,
                ac.acadyear,
                ac.semesterstart,
                c.collegename,
                AVG(CASE WHEN fe.evaluator_type = 'student'    THEN fe.score END) AS avg_s,
                AVG(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score END) AS avg_sv,
                AVG(CASE WHEN fe.evaluator_type = 'peer'       THEN fe.score END) AS avg_p
            FROM faculty_evaluations fe
            JOIN acadcalendar ac  ON fe.acadcalendar_id = ac.acadcalendar_id
            JOIN personnel p      ON fe.personnel_id    = p.personnel_id
            JOIN college c        ON p.college_id       = c.college_id
            WHERE p.role_id = 20001
            GROUP BY ac.acadcalendar_id, ac.semester, ac.acadyear, ac.semesterstart, c.collegename
            ORDER BY ac.semesterstart ASC, c.collegename ASC
        """)
        rows = cur.fetchall()

        # Build ordered semester label list and per-dept score map
        sem_order = []
        seen_sems = set()
        dept_data = {}

        for row in rows:
            sem_id   = row[0]
            sem_name = row[1]
            year     = (row[2] or '').replace('AY ', '')
            short    = '1st Sem' if 'First' in sem_name else ('2nd Sem' if 'Second' in sem_name else 'Summer')
            label    = f"{short} AY {year}"
            dept     = row[4]

            if sem_id not in seen_sems:
                seen_sems.add(sem_id)
                sem_order.append({'id': sem_id, 'label': label})

            avg_s  = float(row[5]) if row[5] is not None else None
            avg_sv = float(row[6]) if row[6] is not None else None
            avg_p  = float(row[7]) if row[7] is not None else None

            weights = []
            if avg_s  is not None: weights.append((avg_s,  0.55))
            if avg_sv is not None: weights.append((avg_sv, 0.35))
            if avg_p  is not None: weights.append((avg_p,  0.10))

            overall = None
            if weights:
                total_w = sum(w for _, w in weights)
                overall = round(sum(v * w for v, w in weights) / total_w, 3)

            if dept not in dept_data:
                dept_data[dept] = {}
            dept_data[dept][sem_id] = overall

        # Align each dept's scores to the global semester order (None for missing)
        sem_labels = [s['label'] for s in sem_order]
        sem_ids    = [s['id']    for s in sem_order]
        depts_aligned = {
            dept: [dept_data[dept].get(sid) for sid in sem_ids]
            for dept in sorted(dept_data.keys())
        }

        return jsonify({'success': True, 'semesters': sem_labels, 'depts': depts_aligned})
    except Exception as e:
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/hr/distribution-trends', methods=['GET'])
@require_auth([20003])
def api_hr_distribution_trends():
    """Per-semester count of faculty in each rating bucket (0-1, 1-2, 2-3, 3-4)."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            WITH faculty_sem_scores AS (
                SELECT
                    fe.acadcalendar_id,
                    fe.personnel_id,
                    COALESCE(MAX(CASE WHEN fe.evaluator_type = 'student'    THEN fe.score END), 0) * 0.55 +
                    COALESCE(MAX(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score END), 0) * 0.35 +
                    COALESCE(MAX(CASE WHEN fe.evaluator_type = 'peer'       THEN fe.score END), 0) * 0.10
                        AS overall
                FROM faculty_evaluations fe
                JOIN personnel p ON fe.personnel_id = p.personnel_id
                WHERE p.role_id = 20001
                GROUP BY fe.acadcalendar_id, fe.personnel_id
            )
            SELECT
                ac.acadcalendar_id,
                ac.semester,
                ac.acadyear,
                ac.semesterstart,
                COUNT(CASE WHEN fss.overall > 0 AND fss.overall <= 1 THEN 1 END) AS b01,
                COUNT(CASE WHEN fss.overall > 1 AND fss.overall <= 2 THEN 1 END) AS b12,
                COUNT(CASE WHEN fss.overall > 2 AND fss.overall <= 3 THEN 1 END) AS b23,
                COUNT(CASE WHEN fss.overall > 3                      THEN 1 END) AS b34
            FROM faculty_sem_scores fss
            JOIN acadcalendar ac ON fss.acadcalendar_id = ac.acadcalendar_id
            WHERE fss.overall > 0
            GROUP BY ac.acadcalendar_id, ac.semester, ac.acadyear, ac.semesterstart
            ORDER BY ac.semesterstart ASC
        """)
        rows = cur.fetchall()

        sem_labels = []
        buckets = {'0–1': [], '1–2': [], '2–3': [], '3–4': []}

        for row in rows:
            sem_name = row[1]
            year     = (row[2] or '').replace('AY ', '')
            short    = '1st Sem' if 'First' in sem_name else ('2nd Sem' if 'Second' in sem_name else 'Summer')
            sem_labels.append(f"{short} AY {year}")
            buckets['0–1'].append(int(row[4]))
            buckets['1–2'].append(int(row[5]))
            buckets['2–3'].append(int(row[6]))
            buckets['3–4'].append(int(row[7]))

        return jsonify({'success': True, 'semesters': sem_labels, 'buckets': buckets})
    except Exception as e:
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/hr/faculty-evaluation-report/<int:personnel_id>')
@require_auth([20003])
def api_hr_faculty_evaluation_report(personnel_id):
    """
    API endpoint to fetch a detailed faculty evaluation report for modal viewing.
    Uses calculated weights from the existing logic (55/35/10).
    Resolves TypeError by casting database scores to float.
    """
    term_id = request.args.get('term_id')
    if not term_id:
        return jsonify({'success': False, 'error': 'Missing term ID'}), 400
    try:
        term_id = int(term_id)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'Invalid term ID'}), 400

    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        # 1. Fetch Faculty Name, College, and Semester Info
        cursor.execute("""
            SELECT 
                p.firstname, p.lastname, p.honorifics, c.collegename,
                ac.semester, ac.acadyear
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            JOIN acadcalendar ac ON ac.acadcalendar_id = %s
            WHERE p.personnel_id = %s
        """, (term_id, personnel_id))
        
        info_row = cursor.fetchone()
        
        if not info_row:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Faculty or Semester not found'}), 404
        
        firstname, lastname, honorifics, collegename, semester_name, acadyear = info_row
        faculty_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
        semester_display = f"{semester_name}, AY {acadyear}"

        # 2. Fetch all evaluation scores AND qualitative feedback
        cursor.execute("""
            SELECT 
                evaluator_type, 
                score, 
                total_responses,
                qualitative_feedback
            FROM faculty_evaluations 
            WHERE personnel_id = %s AND acadcalendar_id = %s
        """, (personnel_id, term_id))
        
        evaluation_rows = cursor.fetchall()
        
        # 3. Aggregate data, calculate overall score, and collect feedback
        total_score = 0
        rating_breakdown = []
        qualitative_feedback = []
        
        # Fixed weights based on business logic: Student(55%), Supervisor(35%), Peer(10%)
        weights = {'student': 0.55, 'supervisor': 0.35, 'peer': 0.10}

        # Group by type: student has multiple rows (per class), supervisor/peer have one.
        from collections import defaultdict
        type_scores = defaultdict(list)
        type_responses = defaultdict(int)
        type_feedback = defaultdict(list)

        for eval_type, score, total_responses, feedback in evaluation_rows:
            score_float = float(score) if score is not None else 0.0
            type_scores[eval_type].append(score_float)
            type_responses[eval_type] += (total_responses or 0)
            if feedback and feedback.strip():
                comments = [c.strip() for c in feedback.split('\n') if c.strip()]
                type_feedback[eval_type].extend(comments)

        score_complete = all(et in type_scores for et in ['student', 'supervisor', 'peer'])

        for eval_type, weight in weights.items():
            scores_list = type_scores.get(eval_type, [])
            avg_score = (sum(scores_list) / len(scores_list)) if scores_list else 0.0
            total_score += avg_score * weight
            rating_breakdown.append({
                'type': eval_type.capitalize(),
                'score': avg_score,
                'weight': weight,
                'total_responses': type_responses.get(eval_type, 0)
            })
            qualitative_feedback.extend(type_feedback.get(eval_type, []))


        # 4. Fetch peer evaluation submissions for richer feedback
        cursor.execute("""
            SELECT evaluator_name, strengths, growth, comments, final_score
            FROM peer_evaluation_submissions
            WHERE evaluatee_id = %s AND acadcalendar_id = %s
            ORDER BY date_submitted
        """, (personnel_id, term_id))
        peer_submission_rows = cursor.fetchall()
        peer_submissions = []
        for row in peer_submission_rows:
            ev_name, strengths, growth, comments, final_score = row
            peer_submissions.append({
                'evaluator_name': ev_name or 'Anonymous',
                'strengths': (strengths or '').strip(),
                'growth': (growth or '').strip(),
                'comments': (comments or '').strip(),
                'final_score': float(final_score) if final_score else 0.0
            })

        cursor.close()
        db_pool.return_connection(conn)

        return jsonify({
            'success': True,
            'report': {
                'faculty_name': faculty_name,
                'college': collegename,
                'semester_display': semester_display,
                'overall_rating': total_score,
                'score_complete': score_complete,
                'rating_breakdown': rating_breakdown,
                'qualitative_feedback': qualitative_feedback,
                'peer_submissions': peer_submissions
            }
        })

    except Exception as e:
        print(f"Error fetching evaluation report: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/hr/faculty-evaluation-report-pdf/<int:personnel_id>')
@require_auth([20003])
def api_hr_faculty_evaluation_report_pdf(personnel_id):
    term_id = request.args.get('term_id')

    # 1. FETCH DATA (Reusing your existing API logic)
    report_response = api_hr_faculty_evaluation_report(personnel_id)
    if report_response.status_code != 200:
        return report_response
    
    from flask import json
    report_data = json.loads(report_response.data.decode('utf-8'))['report']

    # --- NEW: 1. Detect Non-Final Status and Identify Missing Components ---
    is_final = True
    missing_components = []
    
    # Required component weights
    required_weights = {
        'Student': 0.55, 
        'Supervisor': 0.35, 
        'Peer': 0.10
    }
    
    # Tally weights from fetched data
    fetched_weights = {item['type']: item['weight'] for item in report_data.get('rating_breakdown', [])}
    
    # Check for missing weights (This explicitly identifies which component is missing)
    for component, required_weight in required_weights.items():
        if fetched_weights.get(component, 0) == 0:
            is_final = False
            missing_components.append(component)

    if report_data.get('overall_rating', 0.0) == 0.0 and is_final:
        is_final = False 
        
    # 2. SETUP DOCUMENT
    buffer = BytesIO()
    
    doc = SimpleDocTemplate(
        buffer, 
        pagesize=(8.5 * inch, 11 * inch),
        topMargin=0.75 * inch, 
        leftMargin=0.75 * inch, 
        rightMargin=0.75 * inch, 
        bottomMargin=0.5 * inch
    )
    
    styles = getSampleStyleSheet()
    story = []

    # 3. ADD HEADER & METADATA
    
    title_style = ParagraphStyle(
        'Title', 
        parent=styles['h1'], 
        fontSize=16, 
        textColor=colors.HexColor('#7b1113'),
        spaceAfter=6
    )

    preliminary_style = ParagraphStyle(
        'Disclaimer', 
        parent=styles['h2'], 
        fontSize=14, 
        textColor=colors.red,
        alignment=1, # Center
        spaceAfter=18,
        spaceBefore=12
    )

    faculty_name = report_data.get('faculty_name', 'Faculty Report')
    
    raw_semester = report_data.get('semester_display', 'N/A')
    
    semester = raw_semester.replace('AY AY', 'AY').replace('  ', ' ').strip() 

    overall_rating = report_data.get('overall_rating', 0.0)

    story.append(Paragraph("Saint Peter's College - Faculty Evaluation Report", title_style))
    
    # --- NEW: 2. Add a Prominent Disclaimer if not final ---
    if not is_final:
        story.append(Paragraph(
            "<b>*** PRELIMINARY REPORT - NOT FINALIZED ***</b>", 
            preliminary_style
        ))
        
        missing_list_str = ", ".join(missing_components)
        warning_message = f"Warning: This report is incomplete. Missing components are: {missing_list_str}. Final score relies on missing components being weighted as zero."
        
        story.append(Paragraph(
            f"<font color='#FF0000' size='9'><i>{warning_message}</i></font>", 
            styles['Normal']
        ))
    
    story.append(Paragraph(f"<b>Faculty:</b> {faculty_name}", styles['Normal']))
    story.append(Paragraph(f"<b>Department:</b> {report_data.get('college', 'N/A')}", styles['Normal']))
    story.append(Paragraph(f"<b>Semester:</b> {semester}", styles['Normal']))
    story.append(Spacer(1, 0.2 * inch))

    # 4. OVERALL RATING TABLE
    
    overall_data = [
        ['Overall Weighted Score:', f'{overall_rating:.2f}'],
        ['Final Status:', f'{overall_rating:.2f} ({getStatusLabel(overall_rating)})'],
        ['Generated Date:', datetime.now().strftime('%Y-%m-%d %H:%M')]
    ]

    table_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F2F2F2')),
        ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ])
    
    summary_table = Table(overall_data, colWidths=[2.5 * inch, 2.5 * inch])
    summary_table.setStyle(table_style)
    story.append(Paragraph("<b>Evaluation Summary</b>", styles['h3']))
    story.append(summary_table)
    story.append(Spacer(1, 0.3 * inch))

    # 5. RATING BREAKDOWN TABLE
    
    breakdown_header = [
        Paragraph("<b>Evaluator</b>", styles['Normal']), 
        Paragraph("<b>Weight</b>", styles['Normal']), 
        Paragraph("<b>Score</b>", styles['Normal']), 
        Paragraph("<b>Responses</b>", styles['Normal'])
    ]
    breakdown_data = [breakdown_header]

    for item in report_data.get('rating_breakdown', []):
        weight_percent = f"{item.get('weight', 0) * 100:.0f}%"
        
        score = item.get('score', 0.0)
        score_display = f"{score:.2f}"
        
        # If score is zero, and it's a weighted component, show indicator
        if score == 0.0 and item.get('weight', 0) > 0 and report_data.get('overall_rating', 0.0) > 0:
            score_display = f"{score_display} (Missing)"
        elif score == 0.0 and item.get('weight', 0) > 0 and report_data.get('overall_rating', 0.0) == 0.0:
            score_display = f"{score:.2f}"


        breakdown_data.append([
            item.get('type').capitalize(),
            weight_percent,
            score_display,
            item.get('total_responses', 0)
        ])

    breakdown_table = Table(breakdown_data, colWidths=[2.2 * inch, 1 * inch, 1 * inch, 1 * inch])
    breakdown_table.setStyle(TableStyle([
        ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
    ]))
    
    story.append(Paragraph("<b>Rating Breakdown</b>", styles['h3']))
    story.append(breakdown_table)
    story.append(Spacer(1, 0.3 * inch))

    # 6. QUALITATIVE FEEDBACK
    
    story.append(Paragraph("<b>Qualitative Feedback</b>", styles['h3']))
    
    bullet_style = ParagraphStyle(
        'BulletPoint', 
        parent=styles['BodyText'], 
        firstLineIndent=-0.25 * inch,  
        leftIndent=0.5 * inch,         
        bulletIndent=0.25 * inch,      
        spaceBefore=3, 
        spaceAfter=3,
        fontSize=10,
    )
    
    bullet_text = "\u2022" 

    raw_feedback_list = report_data.get('qualitative_feedback')
    clean_comments_for_pdf = []

    if raw_feedback_list:
        for comment in raw_feedback_list:
            
            parts = []
            for sub_part in comment.split('---'):
                parts.extend(sub_part.split('—'))
            
            for part in parts:
                clean_comment = part.strip()
                
                if clean_comment:
                    clean_comments_for_pdf.append(clean_comment)

    if clean_comments_for_pdf:
        for final_comment in clean_comments_for_pdf:
            story.append(Paragraph(
                final_comment, 
                bullet_style, 
                bulletText=bullet_text
            ))
    else:
        story.append(Paragraph("No qualitative feedback available.", styles['Italic']))

    # 7. BUILD PDF
    doc.build(story)

    # 8. SEND RESPONSE
    buffer.seek(0)
    
    # 8a. Create the response object.
    response = make_response(buffer.getvalue())
    
    # 8b. Set the Content-Type header once.
    response.headers['Content-Type'] = 'application/pdf'
    
    # 8c. Set the Content-Disposition header once, ensuring it is the only one.
    filename = f'Evaluation_Report_{faculty_name.replace(" ", "_")}_T{term_id}.pdf'
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'

    return response

@app.route('/api/hr/faculty-class-attendance-pdf/<int:personnel_id>/<int:class_id>')
@require_auth([20003])
def api_hr_faculty_class_attendance_pdf(personnel_id, class_id):
    """Generate a per-class attendance PDF for a specific faculty and class."""
    try:
        semester_id = request.args.get('semester_id')

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        # 1. Faculty details
        cursor.execute("""
            SELECT p.firstname, p.lastname, p.honorifics, c.collegename
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            WHERE p.personnel_id = %s
        """, (personnel_id,))
        person = cursor.fetchone()
        if not person:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Faculty not found'}), 404

        firstname, lastname, honorifics, collegename = person
        faculty_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
        department = collegename or 'N/A'

        # 2. Class / subject details
        cursor.execute("""
            SELECT sub.subjectcode, sub.subjectname, sch.classsection, sch.classroom
            FROM schedule sch
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE sch.class_id = %s
        """, (class_id,))
        cls = cursor.fetchone()
        if not cls:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Class not found'}), 404

        subject_code, subject_name, section, classroom = cls

        # 3. Semester display
        if not semester_id:
            cursor.execute("""
                SELECT acadcalendar_id FROM acadcalendar
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC LIMIT 1
            """)
            result = cursor.fetchone()
            semester_id = result[0] if result else None

        semester_display = 'N/A'
        is_preliminary = False
        if semester_id:
            cursor.execute("""
                SELECT semester, acadyear, semesterend FROM acadcalendar
                WHERE acadcalendar_id = %s
            """, (semester_id,))
            cal = cursor.fetchone()
            if cal:
                sem_name, acad_year, semesterend = cal
                if 'semester' not in sem_name.lower():
                    sem_name = f"{sem_name} Semester"
                acad_year_clean = acad_year.upper().lstrip('AY').strip()
                semester_display = f"{sem_name}, AY {acad_year_clean}"
                from datetime import date as _date
                if semesterend and _date.today() <= semesterend:
                    is_preliminary = True

        # 4. Attendance report for this specific class
        present = late = excused = absent = total_classes = 0
        rate = 0.0
        if semester_id:
            cursor.execute("""
                SELECT presentcount, latecount, excusedcount, absentcount, totalclasses, attendancerate
                FROM attendancereport
                WHERE personnel_id = %s AND class_id = %s AND acadcalendar_id = %s
            """, (personnel_id, class_id, semester_id))
            row = cursor.fetchone()
            if row:
                present, late, excused, absent, total_classes, rate = row
                present = present or 0; late = late or 0
                excused = excused or 0; absent = absent or 0
                total_classes = total_classes or 0
                rate = round(float(rate), 2) if rate else 0.0

        cursor.close()
        db_pool.return_connection(conn)

        # 5. Build PDF
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=(8.5 * inch, 11 * inch),
            topMargin=0.75 * inch,
            leftMargin=0.75 * inch,
            rightMargin=0.75 * inch,
            bottomMargin=0.5 * inch
        )
        styles = getSampleStyleSheet()
        story = []

        title_style = ParagraphStyle(
            'Title',
            parent=styles['h1'],
            fontSize=16,
            textColor=colors.HexColor('#7b1113'),
            spaceAfter=6
        )
        preliminary_style = ParagraphStyle(
            'Disclaimer',
            parent=styles['h2'],
            fontSize=14,
            textColor=colors.red,
            alignment=1,
            spaceAfter=18,
            spaceBefore=12
        )

        # 5a. Title
        story.append(Paragraph("Saint Peter's College - Faculty Attendance Report", title_style))

        # 5b. Preliminary banner
        if is_preliminary:
            story.append(Paragraph(
                "<b>*** PRELIMINARY REPORT - NOT FINALIZED ***</b>",
                preliminary_style
            ))
            story.append(Paragraph(
                "<font color='#FF0000' size='9'><i>Warning: This report is based on attendance recorded so far. "
                "The semester is still in progress and the final figures may change.</i></font>",
                styles['Normal']
            ))

        # 5c. Faculty / semester header
        story.append(Paragraph(f"<b>Faculty:</b> {faculty_name}", styles['Normal']))
        story.append(Paragraph(f"<b>Department:</b> {department}", styles['Normal']))
        story.append(Paragraph(f"<b>Semester:</b> {semester_display}", styles['Normal']))
        story.append(Spacer(1, 0.2 * inch))

        # 5d. Class Details table
        generated_date = datetime.now().strftime('%B %d, %Y %I:%M %p')
        class_details_data = [
            ['Subject:', f"{subject_code} \u2014 {subject_name}"],
            ['Section:', section or '\u2014'],
            ['Classroom:', classroom or '\u2014'],
            ['Generated Date:', generated_date],
        ]
        class_details_style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F2F2F2')),
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ])
        class_details_table = Table(class_details_data, colWidths=[2.5 * inch, 2.5 * inch])
        class_details_table.setStyle(class_details_style)
        story.append(Paragraph("<b>Class Details</b>", styles['h3']))
        story.append(class_details_table)
        story.append(Spacer(1, 0.25 * inch))

        # 6. Attendance summary
        summary_data = [
            ['Present:', str(present)],
            ['Late:', str(late)],
            ['Excused:', str(excused)],
            ['Absent:', str(absent)],
            ['Total Classes:', str(total_classes)],
            ['Attendance Rate:', f'{rate:.2f}%'],
        ]
        summary_table_style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F2F2F2')),
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ])
        summary_table = Table(summary_data, colWidths=[2.5 * inch, 2.5 * inch])
        summary_table.setStyle(summary_table_style)
        story.append(Paragraph("<b>Attendance Summary</b>", styles['h3']))
        story.append(summary_table)

        doc.build(story)

        buffer.seek(0)
        response = make_response(buffer.getvalue())
        response.headers['Content-Type'] = 'application/pdf'
        safe_name = faculty_name.replace(' ', '_')
        safe_code = subject_code.replace(' ', '_')
        safe_section = (section or 'NoSection').replace(' ', '_')
        filename = f'Attendance_Report_{safe_name}_{safe_code}_{safe_section}.pdf'
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    except Exception as e:
        print(f"Error generating class attendance report PDF: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/hr/faculty-attendance-report-pdf/<int:personnel_id>')
@require_auth([20003])
def api_hr_faculty_attendance_report_pdf(personnel_id):
    """Generate a full-semester attendance report PDF for a faculty, listing all their classes."""
    try:
        semester_id = request.args.get('semester_id')

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        # 1. Faculty details
        cursor.execute("""
            SELECT p.firstname, p.lastname, p.honorifics, c.collegename
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            WHERE p.personnel_id = %s
        """, (personnel_id,))
        person = cursor.fetchone()
        if not person:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Faculty not found'}), 404

        firstname, lastname, honorifics, collegename = person
        faculty_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
        department = collegename or 'N/A'

        # 2. Resolve semester
        if not semester_id:
            cursor.execute("""
                SELECT acadcalendar_id FROM acadcalendar
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC LIMIT 1
            """)
            result = cursor.fetchone()
            semester_id = result[0] if result else None

        semester_display = 'N/A'
        is_preliminary = False
        if semester_id:
            cursor.execute("""
                SELECT semester, acadyear, semesterend FROM acadcalendar
                WHERE acadcalendar_id = %s
            """, (semester_id,))
            cal = cursor.fetchone()
            if cal:
                sem_name, acad_year, semesterend = cal
                if 'semester' not in sem_name.lower():
                    sem_name = f"{sem_name} Semester"
                acad_year_clean = acad_year.upper().lstrip('AY').strip()
                semester_display = f"{sem_name}, AY {acad_year_clean}"
                from datetime import date as _date
                if semesterend and _date.today() <= semesterend:
                    is_preliminary = True

        # 3. All classes for this faculty in the semester
        classes = []
        if semester_id:
            cursor.execute("""
                SELECT sub.subjectcode, sub.subjectname, sch.classsection, sch.classroom,
                       ar.presentcount, ar.latecount, ar.excusedcount, ar.absentcount,
                       ar.totalclasses, ar.attendancerate
                FROM attendancereport ar
                JOIN schedule sch ON ar.class_id = sch.class_id
                JOIN subjects sub ON sch.subject_id = sub.subject_id
                WHERE ar.personnel_id = %s AND ar.acadcalendar_id = %s
                ORDER BY sub.subjectcode, sch.classsection
            """, (personnel_id, semester_id))
            classes = cursor.fetchall()

        cursor.close()
        db_pool.return_connection(conn)

        # 4. Build PDF
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=(8.5 * inch, 11 * inch),
            topMargin=0.75 * inch,
            leftMargin=0.75 * inch,
            rightMargin=0.75 * inch,
            bottomMargin=0.5 * inch
        )
        styles = getSampleStyleSheet()
        story = []

        title_style = ParagraphStyle(
            'Title',
            parent=styles['h1'],
            fontSize=16,
            textColor=colors.HexColor('#7b1113'),
            spaceAfter=6
        )
        preliminary_style = ParagraphStyle(
            'Disclaimer',
            parent=styles['h2'],
            fontSize=14,
            textColor=colors.red,
            alignment=1,
            spaceAfter=18,
            spaceBefore=12
        )

        story.append(Paragraph("Saint Peter's College - Faculty Attendance Report", title_style))

        if is_preliminary:
            story.append(Paragraph(
                "<b>*** PRELIMINARY REPORT - NOT FINALIZED ***</b>",
                preliminary_style
            ))
            story.append(Paragraph(
                "<font color='#FF0000' size='9'><i>Warning: This report is based on attendance recorded so far. "
                "The semester is still in progress and the final figures may change.</i></font>",
                styles['Normal']
            ))

        generated_date = datetime.now().strftime('%B %d, %Y %I:%M %p')
        story.append(Paragraph(f"<b>Faculty:</b> {faculty_name}", styles['Normal']))
        story.append(Paragraph(f"<b>Department:</b> {department}", styles['Normal']))
        story.append(Paragraph(f"<b>Semester:</b> {semester_display}", styles['Normal']))
        story.append(Paragraph(f"<b>Generated:</b> {generated_date}", styles['Normal']))
        story.append(Spacer(1, 0.25 * inch))

        # 5. Classes table
        story.append(Paragraph("<b>Class Summary</b>", styles['h3']))

        if not classes:
            story.append(Paragraph("No attendance records found for this semester.", styles['Normal']))
            total_present = total_late = total_excused = total_absent = total_classes = 0
            overall_rate = 0.0
        else:
            header = ['Subject', 'Section', 'Room', 'Present', 'Late', 'Excused', 'Absent', 'Total', 'Rate']
            table_data = [header]
            total_present = total_late = total_excused = total_absent = total_classes = 0

            for row in classes:
                subjectcode, subjectname, section, classroom, present, late, excused, absent, total_cls, rate = row
                present = present or 0; late = late or 0
                excused = excused or 0; absent = absent or 0
                total_cls = total_cls or 0
                rate = round(float(rate), 2) if rate else 0.0
                total_present  += present
                total_late     += late
                total_excused  += excused
                total_absent   += absent
                total_classes  += total_cls
                table_data.append([
                    f"{subjectcode}\n{subjectname}",
                    section or '—',
                    classroom or '—',
                    str(present),
                    str(late),
                    str(excused),
                    str(absent),
                    str(total_cls),
                    f"{rate:.1f}%"
                ])

            overall_rate = round((total_present + total_late) / total_classes * 100, 2) if total_classes else 0.0

            col_widths = [2.2*inch, 0.7*inch, 0.7*inch, 0.55*inch, 0.55*inch, 0.65*inch, 0.65*inch, 0.55*inch, 0.65*inch]
            tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
            tbl.setStyle(TableStyle([
                ('BACKGROUND',    (0, 0), (-1, 0),  colors.HexColor('#F2F2F2')),
                ('FONTNAME',      (0, 0), (-1, 0),  'Helvetica-Bold'),
                ('FONTSIZE',      (0, 0), (-1, 0),  9),
                ('ALIGN',         (3, 0), (-1, -1), 'CENTER'),
                ('FONTNAME',      (0, 1), (-1, -1), 'Helvetica'),
                ('FONTSIZE',      (0, 1), (-1, -1), 8),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F9F9F9')]),
                ('LINEBELOW',     (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
                ('LEFTPADDING',   (0, 0), (-1, -1), 5),
                ('RIGHTPADDING',  (0, 0), (-1, -1), 5),
                ('TOPPADDING',    (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(tbl)

        # 6. Faculty Summary
        story.append(Spacer(1, 0.25 * inch))
        story.append(Paragraph("<b>Faculty Summary</b>", styles['h3']))

        summary_data = [
            ['Present:',         str(total_present)],
            ['Late:',            str(total_late)],
            ['Excused:',         str(total_excused)],
            ['Absent:',          str(total_absent)],
            ['Total Classes:',   str(total_classes)],
            ['Attendance Rate:', f"{overall_rate:.2f}%"],
        ]
        faculty_summary_tbl = Table(summary_data, colWidths=[2.5 * inch, 2.5 * inch])
        faculty_summary_tbl.setStyle(TableStyle([
            ('BACKGROUND',   (0, 0), (-1, 0),  colors.HexColor('#F2F2F2')),
            ('LINEBELOW',    (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
            ('FONTNAME',     (0, 0), (-1, -1), 'Helvetica'),
            ('FONTNAME',     (0, 0), (0, -1),  'Helvetica-Bold'),
            ('FONTSIZE',     (0, 0), (-1, -1), 10),
            ('ALIGN',        (1, 0), (1, -1),  'RIGHT'),
            ('LEFTPADDING',  (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING',   (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING',(0, 0), (-1, -1), 5),
        ]))
        story.append(faculty_summary_tbl)

        doc.build(story)

        buffer.seek(0)
        response = make_response(buffer.getvalue())
        response.headers['Content-Type'] = 'application/pdf'
        safe_name = faculty_name.replace(' ', '_')
        filename = f'Attendance_Report_{safe_name}.pdf'
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    except Exception as e:
        print(f"Error generating faculty attendance report PDF: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/hr/campus-attendance-report-pdf/<int:personnel_id>')
@require_auth([20003])
def api_hr_campus_attendance_report_pdf(personnel_id):
    """Generate a campus attendance report PDF for a faculty member."""
    try:
        semester_id = request.args.get('semester_id')

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        # 1. Faculty details
        cursor.execute("""
            SELECT p.firstname, p.lastname, p.honorifics, c.collegename
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            WHERE p.personnel_id = %s
        """, (personnel_id,))
        person = cursor.fetchone()
        if not person:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Faculty not found'}), 404

        firstname, lastname, honorifics, collegename = person
        faculty_name = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
        department = collegename or 'N/A'

        # 2. Resolve semester
        date_start = date_end = None
        semester_display = 'N/A'
        is_preliminary = False
        if semester_id:
            cursor.execute("""
                SELECT semester, acadyear, semesterstart, semesterend FROM acadcalendar
                WHERE acadcalendar_id = %s
            """, (semester_id,))
            cal = cursor.fetchone()
            if cal:
                sem_name, acad_year, semesterstart, semesterend = cal
                date_start = semesterstart
                date_end   = semesterend
                if 'semester' not in sem_name.lower():
                    sem_name = f"{sem_name} Semester"
                acad_year_clean = acad_year.upper().lstrip('AY').strip()
                semester_display = f"{sem_name}, AY {acad_year_clean}"
                from datetime import date as _date
                if semesterend and _date.today() <= semesterend:
                    is_preliminary = True

        # 3. Aggregate campus attendance counts for this faculty
        params = [personnel_id]
        date_filter = ""
        if date_start and date_end:
            date_filter = "AND ca.attendance_date BETWEEN %s AND %s"
            params += [date_start, date_end]

        cursor.execute(f"""
            SELECT
                COALESCE(SUM(CASE WHEN ca.session = 'Morning'   AND ca.status = 'Present' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN ca.session = 'Morning'   AND ca.status = 'Late'    THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN ca.session = 'Morning'   AND ca.status = 'Absent'  THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN ca.session = 'Afternoon' AND ca.status = 'Present' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN ca.session = 'Afternoon' AND ca.status = 'Late'    THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN ca.session = 'Afternoon' AND ca.status = 'Absent'  THEN 1 ELSE 0 END), 0)
            FROM campus_attendance ca
            WHERE ca.personnel_id = %s {date_filter}
        """, params)
        row = cursor.fetchone()

        cursor.close()
        db_pool.return_connection(conn)

        mp, ml, ma, ap, al, aa = (int(v) for v in row) if row else (0, 0, 0, 0, 0, 0)

        total_sessions = mp + ml + ma + ap + al + aa
        present_late   = mp + ml + ap + al
        rate = round(present_late / total_sessions * 100, 2) if total_sessions else 0.0

        # 5. Build PDF
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=(8.5 * inch, 11 * inch),
            topMargin=0.75 * inch,
            leftMargin=0.75 * inch,
            rightMargin=0.75 * inch,
            bottomMargin=0.5 * inch
        )
        styles = getSampleStyleSheet()
        story = []

        title_style = ParagraphStyle(
            'Title',
            parent=styles['h1'],
            fontSize=16,
            textColor=colors.HexColor('#7b1113'),
            spaceAfter=6
        )
        preliminary_style = ParagraphStyle(
            'Disclaimer',
            parent=styles['h2'],
            fontSize=14,
            textColor=colors.red,
            alignment=1,
            spaceAfter=18,
            spaceBefore=12
        )

        story.append(Paragraph("Saint Peter's College - Campus Attendance Report", title_style))

        if is_preliminary:
            story.append(Paragraph(
                "<b>*** PRELIMINARY REPORT - NOT FINALIZED ***</b>",
                preliminary_style
            ))
            story.append(Paragraph(
                "<font color='#FF0000' size='9'><i>Warning: This report is based on attendance recorded so far. "
                "The semester is still in progress and the final figures may change.</i></font>",
                styles['Normal']
            ))

        generated_date = datetime.now().strftime('%B %d, %Y %I:%M %p')
        story.append(Paragraph(f"<b>Faculty:</b> {faculty_name}", styles['Normal']))
        story.append(Paragraph(f"<b>Department:</b> {department}", styles['Normal']))
        story.append(Paragraph(f"<b>Semester:</b> {semester_display}", styles['Normal']))
        story.append(Paragraph(f"<b>Generated:</b> {generated_date}", styles['Normal']))
        story.append(Spacer(1, 0.25 * inch))

        # 6. Summary
        story.append(Paragraph("<b>Summary</b>", styles['h3']))

        summary_data = [
            ['Morning Present:',   str(mp)],
            ['Morning Late:',      str(ml)],
            ['Morning Absent:',    str(ma)],
            ['Afternoon Present:', str(ap)],
            ['Afternoon Late:',    str(al)],
            ['Afternoon Absent:',  str(aa)],
            ['Total Sessions:',    str(total_sessions)],
            ['Attendance Rate:',   f"{rate:.2f}%"],
        ]
        summary_tbl = Table(summary_data, colWidths=[2.5 * inch, 2.5 * inch])
        summary_tbl.setStyle(TableStyle([
            ('BACKGROUND',   (0, 0), (-1, 0),  colors.HexColor('#F2F2F2')),
            ('LINEBELOW',    (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
            ('FONTNAME',     (0, 0), (-1, -1), 'Helvetica'),
            ('FONTNAME',     (0, 0), (0, -1),  'Helvetica-Bold'),
            ('FONTSIZE',     (0, 0), (-1, -1), 10),
            ('ALIGN',        (1, 0), (1, -1),  'RIGHT'),
            ('LEFTPADDING',  (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING',   (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING',(0, 0), (-1, -1), 5),
        ]))
        story.append(summary_tbl)

        doc.build(story)

        buffer.seek(0)
        response = make_response(buffer.getvalue())
        response.headers['Content-Type'] = 'application/pdf'
        safe_name = faculty_name.replace(' ', '_')
        filename = f'Campus_Attendance_Report_{safe_name}.pdf'
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    except Exception as e:
        print(f"Error generating campus attendance report PDF: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hr/new-evaluation-cycle', methods=['POST'])
@require_auth([20003])
def api_hr_new_evaluation_cycle():
    """HR initiates a new evaluation cycle for the current academic calendar."""
    try:
        data = request.get_json()
        current_term_id = data.get('current_term_id')
        
        next_term_id = int(current_term_id) + 1
        
        # 2. Get HR Personnel ID for audit logging
        hr_info = get_personnel_info(session['user_id'])
        hr_personnel_id = hr_info.get('personnel_id')
        
        # 3. Log the action
        log_audit_action(
            hr_personnel_id,
            "New Evaluation Cycle Initiated",
            "HR initiated a new evaluation cycle",
            before_value=f"Old Term ID: {current_term_id}",
            after_value=f"New Term ID (Simulated): {next_term_id}\nDatabase update required for new term."
        )
        
        # 4. Return success message
        return jsonify({
            'success': True,
            'message': f'New Evaluation Cycle (Term {next_term_id} - Simulated) initiated. Next step is to update acadcalendar and faculty_evaluations tables to reflect the new term.',
            'new_term_id': next_term_id 
        })
        
    except Exception as e:
        print(f"Error initiating new evaluation cycle: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/hr/fetch-evaluations', methods=['POST'])
@require_auth([20003])
def fetch_evaluations():
    
    # Define all data sources
    sources = [
        {'type': 'student', 'fetcher': get_students_score_records},
        {'type': 'supervisor', 'fetcher': get_supervisors_score_records},
        {'type': 'peer', 'fetcher': get_peers_score_records}
    ]

    total_updated = 0
    conn = None 
    
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        print("--- [EVAL UPDATE] Starting evaluation fetch process ---")

        for source in sources:
            records = source['fetcher']()
            print(f"🟢 [EVAL UPDATE] Processing {len(records)} records for {source['type']}.")

            for row in records:
                
                # 1. Class ID: students have one, supervisors always NULL.
                class_id_raw = row.get('Class ID', None)
                try:
                    class_id = int(class_id_raw) if class_id_raw else None
                except (ValueError, TypeError):
                    class_id = None
                if source['type'] == 'supervisor':
                    class_id = None

                # 2. Qualitative Feedback
                qualitative_feedback = row.get('Qualitative Feedback') or None

                if not row.get('Faculty Personnel ID') or not row.get('Semester_AY ID'):
                    print(f"    - [Record] 🛑 SKIP: Missing Faculty ID or Semester ID in {source['type']} row.")
                    continue

                if class_id is not None:
                    cursor.execute(
                        'INSERT INTO faculty_evaluations '
                        '(personnel_id, acadcalendar_id, class_id, evaluator_type, '
                        ' score, total_responses, qualitative_feedback) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s) '
                        'ON CONFLICT (personnel_id, acadcalendar_id, class_id, evaluator_type) '
                        'WHERE class_id IS NOT NULL '
                        'DO UPDATE SET score=EXCLUDED.score, '
                        '             total_responses=EXCLUDED.total_responses, '
                        '             qualitative_feedback=EXCLUDED.qualitative_feedback, '
                        '             last_updated=CURRENT_TIMESTAMP',
                        (row['Faculty Personnel ID'], row['Semester_AY ID'],
                         class_id, source['type'],
                         row['Score'], row['Total Responses'], qualitative_feedback))
                else:
                    cursor.execute(
                        'INSERT INTO faculty_evaluations '
                        '(personnel_id, acadcalendar_id, class_id, evaluator_type, '
                        ' score, total_responses, qualitative_feedback) '
                        'VALUES (%s,%s,NULL,%s,%s,%s,%s) '
                        'ON CONFLICT (personnel_id, acadcalendar_id, evaluator_type) '
                        'WHERE class_id IS NULL '
                        'DO UPDATE SET score=EXCLUDED.score, '
                        '             total_responses=EXCLUDED.total_responses, '
                        '             qualitative_feedback=EXCLUDED.qualitative_feedback, '
                        '             last_updated=CURRENT_TIMESTAMP',
                        (row['Faculty Personnel ID'], row['Semester_AY ID'],
                         source['type'],
                         row['Score'], row['Total Responses'], qualitative_feedback))
                total_updated += 1
            
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        print(f"✅ [EVAL UPDATE] Database COMMIT successful. Total records updated: {total_updated}")
        return jsonify(message=f"Successfully imported and updated {total_updated} evaluation records from all sources.", success=True)

    except Exception as e:
        print(f"❌ [EVAL UPDATE] CRITICAL FAILURE during processing: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            try:
                cursor.close()
                db_pool.return_connection(conn)
            except:
                pass
        return jsonify(message=f"Critical error processing evaluations. Check logs for details. Error: {str(e)}"), 500


# --- PLACEHOLDER GOOGLE FORM CONFIGURATION ---
GOOGLE_FORM_CONFIG = {
    'base_url': "https://docs.google.com/forms/d/e/1FAIpQLSfP_YOUR_FORM_ID_HERE/viewform",
    'entry_ids': {
        'personnel_id': 'entry.123456789',   
        'acadcalendar_id': 'entry.987654321', 
        'evaluator_type': 'entry.112233445'  
    }
}
# -----------------------------------------------


@app.route('/api/hr/generate-evaluation-link', methods=['POST'])
@require_auth([20003])
def api_hr_generate_evaluation_link():
    """Generates a pre-filled Google Form link for a specific evaluation."""
    try:
        data = request.get_json()
        personnel_id = data.get('personnel_id')
        acadcalendar_id = data.get('acadcalendar_id')
        evaluator_type = data.get('evaluator_type')
        
        if not all([personnel_id, acadcalendar_id, evaluator_type]):
            return jsonify({'success': False, 'error': 'Missing personnel ID, term ID, or evaluator type.'}), 400

        # 1. Fetch Faculty Name for logging
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT firstname, lastname FROM personnel WHERE personnel_id = %s", (personnel_id,))
        faculty_info = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if not faculty_info:
            return jsonify({'success': False, 'error': 'Faculty not found.'}), 404
        
        faculty_name = f"{faculty_info[0]} {faculty_info[1]}"
        
        # 2. Build the pre-filled link
        config = GOOGLE_FORM_CONFIG
        
        prefill_url = (
            f"{config['base_url']}?"
            f"&{config['entry_ids']['personnel_id']}={personnel_id}"
            f"&{config['entry_ids']['acadcalendar_id']}={acadcalendar_id}"
            f"&{config['entry_ids']['evaluator_type']}={evaluator_type.capitalize()}"
            f"&usp=pp_url" # Ensures the URL is correctly formatted for pre-filling
        )
        
        # 3. Log Audit Action
        hr_personnel_info = get_personnel_info(session['user_id'])
        log_audit_action(
            hr_personnel_info.get('personnel_id'),
            "Evaluation Link Generated",
            f"Generated {evaluator_type.capitalize()} link for {faculty_name} (ID: {personnel_id}) for Term ID {acadcalendar_id}"
        )

        return jsonify({'success': True, 'link': prefill_url})
        
    except Exception as e:
        print(f"Error generating evaluation link: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/hr_attendance')
@require_auth([20003])
def hr_attendance():
    personnel_info = get_personnel_info(session['user_id'])
    return render_template('hrmd/hr-attendance.html', **personnel_info)

@app.route('/hr_promotions')
@require_auth([20003])
def hr_promotions():
    """HR Promotions Dashboard with promotions and regularizations"""
    try:
        personnel_info = get_personnel_info(session['user_id'])
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        # === FETCH PROMOTIONS ===
        cursor.execute("""
            SELECT 
                pa.application_id,
                pa.faculty_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                c.collegename,
                pr.position as current_rank,
                pa.requested_rank,
                pa.current_status,
                pa.date_submitted,
                pa.hrmd_approval_date,
                pa.vpa_approval_date,
                pa.pres_approval_date
            FROM promotion_application pa
            JOIN personnel p ON pa.faculty_id = p.personnel_id
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            ORDER BY pa.date_submitted DESC
        """)
        
        promotions = cursor.fetchall()
        
        # Format promotion data
        promotions_list = []
        for promo in promotions:
            (application_id, faculty_id, firstname, lastname, honorifics, collegename,
             current_rank, requested_rank, current_status, date_submitted, 
             hrmd_approval, vpa_approval, pres_approval) = promo
            
            if honorifics:
                fullname = f"{lastname}, {firstname}, {honorifics}"
            else:
                fullname = f"{lastname}, {firstname}"
            
            status_display = str(current_status).replace('_', ' ').title() if current_status else 'Pending HR Review'
            
            promotions_list.append({
                'application_id': application_id,
                'faculty_id': faculty_id,
                'name': fullname,
                'department': collegename or 'N/A',
                'currentrank': current_rank or 'Instructor',
                'requestedrank': requested_rank or 'Not Specified',
                'status': status_display,
                'submitteddate': date_submitted.strftime('%Y-%m-%d') if date_submitted else 'N/A'
            })
        
        # === FETCH ELIGIBLE FACULTY + ACTIVE REGULARIZATIONS ===
        # Only faculty with an aligned Master's are eligible (Contractual faculty are not).
        # Probationary countdown starts from probationary_start_date (or hiredate as fallback).
        cursor.execute("""
            SELECT
                p.personnel_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                COALESCE(pr.probationary_start_date, p.hiredate) AS prob_start,
                c.collegename,
                pr.position as current_rank,
                (CURRENT_DATE - COALESCE(pr.probationary_start_date, p.hiredate))::float / 365.25
                    AS years_of_service,
                NULL::text as reg_status,
                NULL::timestamp as date_initiated
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE
                pr.has_aligned_master = TRUE
                AND COALESCE(pr.probationary_start_date, p.hiredate) IS NOT NULL
                AND (CURRENT_DATE - COALESCE(pr.probationary_start_date, p.hiredate)) >= 1095
                AND p.personnel_id NOT IN (
                    SELECT faculty_id
                    FROM regularization_application
                    WHERE final_decision IS NULL
                )

            UNION ALL

            SELECT
                p.personnel_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                COALESCE(pr.probationary_start_date, p.hiredate) AS prob_start,
                c.collegename,
                pr.position as current_rank,
                ra.years_of_service,
                ra.current_status::text as reg_status,
                ra.date_initiated
            FROM regularization_application ra
            JOIN personnel p ON ra.faculty_id = p.personnel_id
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE ra.final_decision IS NULL

            ORDER BY prob_start ASC
        """)

        all_faculty = cursor.fetchall()
        cursor.close()

        # === FETCH CONTRACTUAL FACULTY (no aligned Master's) ===
        # Use a fresh cursor to avoid pg8000 cursor-reuse issues after UNION ALL.
        # IS NOT TRUE matches both FALSE and NULL safely.
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                p.personnel_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                c.collegename,
                pr.position,
                p.hiredate
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE p.role_id IN (20001, 20002)
              AND pr.has_aligned_master IS NOT TRUE
            ORDER BY p.lastname, p.firstname
        """)
        contractual_records = cursor.fetchall()
        cursor.close()

        # === FETCH IN-PROGRESS PROBATIONARY FACULTY ===
        # Has aligned master's but < 3 years — not yet eligible, not contractual.
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                p.personnel_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                COALESCE(pr.probationary_start_date, p.hiredate) AS prob_start,
                c.collegename,
                pr.position AS current_rank,
                (CURRENT_DATE - COALESCE(pr.probationary_start_date, p.hiredate))::float / 365.25
                    AS years_of_service
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE
                pr.has_aligned_master = TRUE
                AND COALESCE(pr.probationary_start_date, p.hiredate) IS NOT NULL
                AND (CURRENT_DATE - COALESCE(pr.probationary_start_date, p.hiredate)) < 1095
                AND p.personnel_id NOT IN (
                    SELECT faculty_id FROM regularization_application
                    WHERE final_decision IS NULL
                )
            ORDER BY prob_start ASC
        """)
        probationary_records = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)

        print(f"[Regularization] Contractual faculty found: {len(contractual_records)}")
        print(f"[Regularization] In-progress probationary found: {len(probationary_records)}")

        probationary_list = []
        for rec in probationary_records:
            (personnel_id, firstname, lastname, honorifics, prob_start,
             collegename, rank, years) = rec
            fullname = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            years = float(years) if years else 0
            days_done = int(years * 365.25)
            pct = min(round((days_done / 1095) * 100, 1), 99.9)
            probationary_list.append({
                'personnel_id': personnel_id,
                'name': fullname,
                'department': collegename or 'N/A',
                'current_rank': rank or 'Instructor',
                'prob_start': prob_start.strftime('%Y-%m-%d') if prob_start else 'N/A',
                'years_of_service': round(years, 2),
                'days_done': days_done,
                'pct': pct,
            })

        nearly_eligible_faculty = [f for f in probationary_list if f['days_done'] >= 1065]

        contractual_list = []
        for rec in contractual_records:
            (personnel_id, firstname, lastname, honorifics, collegename, position, hiredate) = rec
            fullname = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            contractual_list.append({
                'personnel_id': personnel_id,
                'name': fullname,
                'department': collegename or 'N/A',
                'current_rank': position or 'Instructor',
                'hiredate': hiredate.strftime('%Y-%m-%d') if hiredate else 'N/A',
            })

        # Format regularization data
        regularizations_list = []
        for f in all_faculty:
            (personnel_id, firstname, lastname, honorifics, prob_start,
             college, rank, years, reg_status, date_initiated) = f

            fullname = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            years = float(years) if years else 0

            if years >= 7:
                eligible_for = "Tenured"
                current_tenure = "Regular"
            elif years >= 3:
                eligible_for = "Regular"
                current_tenure = "Probationary"
            else:
                continue

            # Determine status display
            if reg_status:
                if reg_status == 'vpa':
                    status_display = "For VPA Review"
                    status_class = "warn"
                elif reg_status == 'pres':
                    status_display = "For President Review"
                    status_class = "warn"
                elif reg_status == 'approved':
                    status_display = "Approved"
                    status_class = "ok"
                elif reg_status == 'rejected':
                    status_display = "Rejected"
                    status_class = "rejected"
                else:
                    status_display = "In Progress"
                    status_class = "pending"
            else:
                status_display = "Eligible"
                status_class = "ok"

            regularizations_list.append({
                'personnel_id': personnel_id,
                'name': fullname,
                'department': college or 'N/A',
                'current_rank': rank or 'Instructor',
                'current_tenure': current_tenure,
                'years_of_service': round(years, 2),
                'hiredate': prob_start.strftime('%Y-%m-%d') if prob_start else 'N/A',
                'eligible_for': eligible_for,
                'status': status_display,
                'status_class': status_class,
                'reg_status': reg_status or 'eligible',
                'has_application': bool(reg_status)
            })
        
        return render_template('hrmd/hr-promotions.html',
                             hr_name=personnel_info.get('hr_name', 'HR Admin'),
                             college=personnel_info.get('college', 'HRMD'),
                             profile_image_base64=personnel_info.get('profile_image_base64', ''),
                             personnelinfo=personnel_info,
                             promotions=promotions_list,
                             regularizations=regularizations_list,
                             probationary_faculty=probationary_list,
                             nearly_eligible_faculty=nearly_eligible_faculty,
                             contractual_faculty=contractual_list)

    except Exception as e:
        print(f"Error in hr_promotions route: {e}")
        import traceback
        traceback.print_exc()

        personnel_info = get_personnel_info(session['user_id'])

        return render_template('hrmd/hr-promotions.html',
                             hr_name=personnel_info.get('hr_name', 'HR Admin'),
                             college=personnel_info.get('college', 'HRMD'),
                             profile_image_base64=personnel_info.get('profile_image_base64', ''),
                             personnelinfo=personnel_info,
                             promotions=[],
                             regularizations=[],
                             probationary_faculty=[],
                             nearly_eligible_faculty=[],
                             contractual_faculty=[])


@app.route('/hr_profile')
@require_auth([20003])
def hr_profile():
    """HR's own profile - clear viewing session"""
    session.pop('viewing_personnel_id', None)
    personnel_info = get_personnel_info(session['user_id'])
    return render_template('hrmd/hr-profile.html', **personnel_info)

@app.route('/hr_settings')
@require_auth([20003])
def hr_settings():
    """HR's own settings - clear viewing session"""
    session.pop('viewing_personnel_id', None)
    personnel_info = get_personnel_info(session['user_id'])
    return render_template('hrmd/hr-settings.html', **personnel_info)

@app.route('/vp_promotions')
@require_auth([20004])
def vp_promotions():
    """VP/President Promotions Dashboard with document flags and precise service tracking"""
    try:
        personnel_info = get_personnel_info(session['user_id'])
        user_role = session.get('user_role')
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        # === FETCH PROMOTIONS (Including OTR and Diploma flags) ===
        cursor.execute("""
            SELECT 
                pa.application_id,
                pa.faculty_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                c.collegename,
                pr.position as current_rank,
                pa.requested_rank,
                pa.current_status,
                pa.date_submitted,
                pa.hrmd_approval_date,
                pa.vpa_approval_date,
                pa.pres_approval_date,
                pa.tor IS NOT NULL as has_tor,       -- Added for OTR viewing
                pa.diploma IS NOT NULL as has_diploma, -- Added for Diploma viewing
                pa.resume IS NOT NULL as has_resume,   -- Added for Resume viewing
                pa.cover_letter IS NOT NULL as has_cover_letter -- Added for Cover Letter viewing
            FROM promotion_application pa
            JOIN personnel p ON pa.faculty_id = p.personnel_id
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            ORDER BY pa.date_submitted DESC
        """)
        
        promotions = cursor.fetchall()
        
        # Format promotion data
        promotions_list = []
        for promo in promotions:
            (application_id, faculty_id, firstname, lastname, honorifics, collegename,
             current_rank, requested_rank, current_status, date_submitted, 
             hrmd_approval, vpa_approval, pres_approval, 
             has_tor, has_diploma, has_resume, has_cover_letter) = promo
            
            fullname = f"{lastname}, {firstname}" + (f", {honorifics}" if honorifics else "")
            status_display = str(current_status).replace('_', ' ').title() if current_status else 'Pending HR Review'
            
            promotions_list.append({
                'application_id': application_id,
                'faculty_id': faculty_id,
                'name': fullname,
                'department': collegename or 'N/A',
                'currentrank': current_rank or 'Instructor',
                'requestedrank': requested_rank or 'Not Specified',
                'status': status_display,
                'submitteddate': date_submitted.strftime('%Y-%m-%d') if date_submitted else 'N/A',
                # Required for the pop-out document viewing logic in VP HTML
                'has_tor': has_tor,
                'has_diploma': has_diploma,
                'has_resume': has_resume,
                'has_cover_letter': has_cover_letter
            })
        
        # === FETCH ACTIVE REGULARIZATIONS ===
        # (This part remains as you have it, but you can add the same logic if needed)
        # Use probationary_start_date (or hiredate) as the service start for regularization.
        cursor.execute("""
            SELECT
                p.personnel_id, p.firstname, p.lastname, p.honorifics,
                COALESCE(pr.probationary_start_date, p.hiredate) AS prob_start,
                c.collegename, pr.position as current_rank,
                ra.years_of_service, ra.current_status as reg_status,
                ra.date_initiated, ra.regularization_id
            FROM regularization_application ra
            JOIN personnel p ON ra.faculty_id = p.personnel_id
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE ra.final_decision IS NULL
            ORDER BY ra.date_initiated DESC
        """)

        active_regs = cursor.fetchall()

        from datetime import date
        today = date.today()

        regularizations_list = []
        for f in active_regs:
            (personnel_id, firstname, lastname, honorifics, prob_start,
             college, rank, years, reg_status, date_initiated, regularization_id) = f

            # Years from probationary start (not hire date)
            actual_years = (today - prob_start).days / 365.25 if prob_start else float(years or 0)
            fullname = f"{lastname}, {firstname}" + (f", {honorifics}" if honorifics else "")

            if actual_years >= 7:
                eligible_for = "Tenured"
                current_tenure = "Regular"
            elif actual_years >= 3:
                eligible_for = "Regular"
                current_tenure = "Probationary"
            else:
                continue

            regularizations_list.append({
                'regularization_id': regularization_id,
                'personnel_id': personnel_id,
                'name': fullname,
                'department': college or 'N/A',
                'current_rank': rank or 'Instructor',
                'current_tenure': current_tenure,
                'years_of_service': round(actual_years, 1),
                'hiredate': prob_start.strftime('%Y-%m-%d') if prob_start else 'N/A',
                'eligible_for': eligible_for,
                'status': str(reg_status).replace('_', ' ').title(),
                'reg_status': reg_status,
                'has_application': True
            })

        cursor.close()

        # === FACULTY TENURE STATUS COUNTS (for regularization tab overview) ===
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                COUNT(CASE WHEN pr.has_aligned_master = TRUE
                      AND COALESCE(pr.probationary_start_date, p.hiredate) IS NOT NULL
                      AND (CURRENT_DATE - COALESCE(pr.probationary_start_date, p.hiredate)) >= 1095
                      THEN 1 END) AS eligible_count,
                COUNT(CASE WHEN pr.has_aligned_master = TRUE
                      AND COALESCE(pr.probationary_start_date, p.hiredate) IS NOT NULL
                      AND (CURRENT_DATE - COALESCE(pr.probationary_start_date, p.hiredate)) < 1095
                      THEN 1 END) AS probationary_count,
                COUNT(CASE WHEN pr.has_aligned_master IS NOT TRUE THEN 1 END) AS contractual_count
            FROM personnel p
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE p.role_id IN (20001, 20002)
        """)
        counts_row = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)

        eligible_count     = int(counts_row[0]) if counts_row and counts_row[0] else 0
        probationary_count = int(counts_row[1]) if counts_row and counts_row[1] else 0
        contractual_count  = int(counts_row[2]) if counts_row and counts_row[2] else 0

        return render_template('vp&pres/vp-promotion.html',
                             vp_name=personnel_info.get('vp_name', 'VP Admin'),
                             college=personnel_info.get('college', 'Office of the VP'),
                             profile_image_base64=personnel_info.get('profile_image_base64', ''),
                             personnelinfo=personnel_info,
                             promotions=promotions_list,
                             regularizations=regularizations_list,
                             eligible_count=eligible_count,
                             probationary_count=probationary_count,
                             contractual_count=contractual_count,
                             user_role=user_role)
                             
    except Exception as e:
        print(f"Error in vp_promotions route: {e}")
        import traceback
        traceback.print_exc()
        return "Internal Server Error", 500



# === REGULARIZATION API ROUTES FOR VP/PRESIDENT ===

@app.route('/api/promotion/forward-to-president', methods=['POST'])
@require_auth([20004])  # VPAA or President
def forward_to_president():
    """VPAA (or President acting as reviewer) forwards a vpa-status promotion to President."""
    try:
        data = request.get_json()
        application_id = data.get('application_id')
        vpa_remarks = data.get('vpa_remarks', '').strip()

        if not application_id:
            return jsonify(success=False, error='Application ID is required'), 400

        conn = db_pool.get_connection()
        cursor = conn.cursor()

        # Verify the application is actually at the vpa stage
        cursor.execute(
            "SELECT current_status FROM promotion_application WHERE application_id = %s",
            (application_id,)
        )
        status_row = cursor.fetchone()
        if not status_row:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Application not found'), 404
        if status_row[0] != 'vpa':
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Application is not at VPAA review stage'), 400

        # Determine who is forwarding (VPAA vs President)
        user_id = session.get('user_id')
        cursor.execute(
            "SELECT pr.position FROM personnel p LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id WHERE p.user_id = %s",
            (user_id,)
        )
        pos_row = cursor.fetchone()
        forwarder_label = 'President' if (pos_row and pos_row[0] == 'President') else 'VPAA'

        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz)

        cursor.execute(
            """UPDATE promotion_application
               SET current_status = %s,
                   vpa_approval_date = %s,
                   vpa_remarks = %s
               WHERE application_id = %s""",
            ('pres', current_time, vpa_remarks if vpa_remarks else None, application_id)
        )

        # Fetch faculty name for notification
        cursor.execute(
            "SELECT p.firstname, p.lastname, pa.requested_rank "
            "FROM promotion_application pa JOIN personnel p ON pa.faculty_id = p.personnel_id "
            "WHERE pa.application_id = %s", (application_id,)
        )
        fac_row = cursor.fetchone()

        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)

        vp_info = get_personnel_info(user_id)
        vp_personnel_id = vp_info.get('personnel_id')

        if fac_row:
            fac_name = f"{fac_row[1]}, {fac_row[0]}"
            fac_rank = fac_row[2]
            log_audit_action(
                vp_personnel_id,
                'Promotion forwarded to President',
                f'{forwarder_label} forwarded {fac_name}\'s promotion application (App ID: {application_id}) for {fac_rank}',
                before_value=f'Faculty: {fac_name} | Applying For: {fac_rank} | Status: VPAA Review',
                after_value='Status: President Review' + (f' | Notes: {vpa_remarks}' if vpa_remarks else '')
            )
            trigger_promotion_forwarded_president(fac_name, fac_rank)
        else:
            log_audit_action(
                vp_personnel_id,
                'Promotion forwarded to President',
                f'{forwarder_label} forwarded promotion application (App ID: {application_id}) to President',
                before_value='Status: VPAA Review',
                after_value='Status: President Review' + (f' | Notes: {vpa_remarks}' if vpa_remarks else '')
            )

        return jsonify(success=True, message='Application forwarded to President successfully')
        
    except Exception as e:
        print(f"Error forwarding to President: {str(e)}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return jsonify(success=False, error=str(e)), 500

@app.route('/api/regularization/approve-by-president', methods=['POST'])
@require_auth([20004])  # President only
def approve_regularization_by_president():
    """President approves regularization"""
    try:
        data = request.get_json()
        regularization_id = data.get('regularization_id')
        
        if not regularization_id:
            return jsonify(success=False, error='Regularization ID is required'), 400
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        # Additional check: Verify user has "President" position
        user_id = session.get('user_id')
        cursor.execute(
                   """
                    SELECT pr.position 
                    FROM personnel p
                    LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
                    WHERE p.user_id = %s
                    """,
                    (user_id,)
                )
        position_result = cursor.fetchone()
        
        print(f"User ID {user_id} position check result: {position_result}")

        if not position_result or position_result[0] != 'President':
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Unauthorized: Only President can approve regularizations'), 403
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz)
        
        # Fetch faculty_id, years_of_service, current status, and finalization state
        cursor.execute(
            "SELECT faculty_id, years_of_service, current_status, final_decision FROM regularization_application WHERE regularization_id = %s",
            (regularization_id,)
        )
        reg_result = cursor.fetchone()
        if not reg_result:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Regularization application not found'), 404

        faculty_id_for_update, years_of_service, current_reg_status, final_decision = reg_result

        if final_decision is not None:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Regularization has already been finalized'), 400

        # President can approve from 'vpa' (override) or 'pres' (normal flow)
        if current_reg_status not in ('vpa', 'pres'):
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Application is not at an active review stage'), 400

        new_employment_status = 'Tenured' if (years_of_service or 0) >= 7 else 'Regular'

        cursor.execute("""
            UPDATE regularization_application
            SET current_status = %s,
                pres_approval_date = %s,
                final_decision = %s
            WHERE regularization_id = %s
        """, ('approved', current_time, 1, regularization_id))

        # Fetch faculty name for audit log
        cursor.execute(
            "SELECT firstname, lastname FROM personnel WHERE personnel_id = %s",
            (faculty_id_for_update,)
        )
        name_row = cursor.fetchone()
        fac_name = f"{name_row[1]}, {name_row[0]}" if name_row else f"Faculty (ID: {faculty_id_for_update})"

        # Update faculty employment status in profile
        cursor.execute(
            "UPDATE profile SET employmentstatus = %s WHERE personnel_id = %s",
            (new_employment_status, faculty_id_for_update)
        )

        conn.commit()

        # Log audit
        personnel_info = get_personnel_info(user_id)
        pres_personnel_id = personnel_info.get('personnel_id')

        _stage_labels = {'vpa': 'VPAA Review', 'pres': 'President Review'}
        stage_label = _stage_labels.get(current_reg_status, current_reg_status.title())

        if pres_personnel_id:
            log_audit_action(
                pres_personnel_id,
                'Regularization approved',
                f'President approved regularization for {fac_name} (Reg ID: {regularization_id})',
                before_value=f'Faculty: {fac_name} | Status: {stage_label}',
                after_value=f'Status: Approved | New Employment: {new_employment_status}'
            )

        cursor.close()
        db_pool.return_connection(conn)

        return jsonify({
            'success': True,
            'message': f'Regularization approved. Faculty status updated to {new_employment_status}.'
        })
    
    except Exception as e:
        print(f"Error approving regularization: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/regularization/forward-to-president', methods=['POST'])
@require_auth([20004]) # Only VPAA is authorized for this step
def forward_regularization_to_president():
    """VPAA forwards regularization application to the President's review queue."""
    conn = None # Initialize conn outside try block for proper cleanup
    cursor = None
    try:
        data = request.get_json()
        regularization_id = data.get('regularization_id')

        if not regularization_id:
            return jsonify(success=False, error='Regularization ID is required'), 400

        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz)

        # 1. Fetch faculty_id, current status, final_decision, and faculty name for auditing
        cursor.execute("""
            SELECT ra.faculty_id, ra.current_status, p.firstname, p.lastname, ra.final_decision
            FROM regularization_application ra
            JOIN personnel p ON ra.faculty_id = p.personnel_id
            WHERE ra.regularization_id = %s
        """, (regularization_id,))
        result = cursor.fetchone()

        if not result:
            return jsonify(success=False, error='Regularization application not found.'), 404

        faculty_id, old_status, fac_firstname, fac_lastname, final_decision = result
        fac_name = f"{fac_lastname}, {fac_firstname}"

        if final_decision is not None:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Regularization has already been finalized'), 400

        if old_status != 'vpa':
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Application is not at VPAA review stage'), 400

        # 2. Update status to 'pres' (President Review) and record VPAA action date
        cursor.execute(
            """UPDATE regularization_application
               SET current_status = %s,
                   vpa_recommendation_date = %s
               WHERE regularization_id = %s""",
            ('pres', current_time, regularization_id)
        )
        
        conn.commit()

        # 3. Audit log
        user_id = session.get('user_id')
        personnel_info = get_personnel_info(user_id)
        vp_personnel_id = personnel_info.get('personnel_id')
        
        if vp_personnel_id:
            _stage_labels = {'vpa': 'VPAA Review', 'pres': 'President Review'}
            old_stage = _stage_labels.get(old_status, old_status.title() if old_status else 'Unknown')
            log_audit_action(
                vp_personnel_id,
                'Regularization forwarded to President',
                f'VPAA forwarded {fac_name}\'s regularization application (Reg ID: {regularization_id}) to President',
                before_value=f'Faculty: {fac_name} | Status: {old_stage}',
                after_value='Status: President Review'
            )

        cursor.close()
        db_pool.return_connection(conn)
        
        return jsonify(success=True, message='Regularization forwarded to President successfully')

    except Exception as e:
        print(f"Error processing regularization forward: {str(e)}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            if cursor: cursor.close()
            db_pool.return_connection(conn)
        
        # Return the actual exception message for better debugging
        return jsonify(success=False, error=f"Server Error during forwarding: {str(e)}"), 500

@app.route('/api/regularization/reject', methods=['POST'])
@require_auth([20004])
def reject_regularization():
    """President rejects regularization"""
    try:
        data = request.get_json()
        regularization_id = data.get('regularization_id')
        rejection_reason = data.get('rejection_reason', 'No reason provided')
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        # Get faculty_id, current_status, final_decision, and name
        cursor.execute("""
            SELECT ra.faculty_id, ra.current_status, p.firstname, p.lastname, ra.final_decision
            FROM regularization_application ra
            JOIN personnel p ON ra.faculty_id = p.personnel_id
            WHERE ra.regularization_id = %s
        """, (regularization_id,))

        result = cursor.fetchone()
        if not result:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Regularization not found'}), 404

        faculty_id, old_reg_status, fac_firstname, fac_lastname, final_decision = result
        fac_name = f"{fac_lastname}, {fac_firstname}"

        if final_decision is not None:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Regularization has already been finalized'}), 400

        # President can reject from 'vpa' (override) or 'pres' (normal flow)
        if old_reg_status not in ('vpa', 'pres'):
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Application is not at an active review stage'}), 400
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz)
        
        cursor.execute("""
            UPDATE regularization_application 
            SET current_status = %s,
                final_decision = %s,
                pres_notes = %s
            WHERE regularization_id = %s
        """, ('rejected', 0, rejection_reason, regularization_id))
        
        conn.commit()

        user_id = session.get('user_id')
        pres_info = get_personnel_info(user_id)
        pres_personnel_id = pres_info.get('personnel_id')
        _stage_labels = {'vpa': 'VPAA Review', 'pres': 'President Review'}
        stage_label = _stage_labels.get(old_reg_status, old_reg_status.title() if old_reg_status else 'Unknown')

        log_audit_action(
            pres_personnel_id,
            'Regularization rejected',
            f'President rejected regularization for {fac_name} (Reg ID: {regularization_id})',
            before_value=f'Faculty: {fac_name} | Status: {stage_label}',
            after_value=f'Status: Rejected | Reason: {rejection_reason}'
        )
        
        cursor.close()
        db_pool.return_connection(conn)
        
        return jsonify({'success': True, 'message': 'Regularization rejected'})
    
    except Exception as e:
        print(f"Error rejecting regularization: {e}")
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return jsonify({'success': False, 'error': str(e)}), 500



@app.route('/vp_profile')
@require_auth([20004])
def vp_profile():
    personnel_info = get_personnel_info(session['user_id'])
    return render_template('vp&pres/vp-profile.html', **personnel_info)

@app.route('/vp_settings')
@require_auth([20004])
def vp_settings():
    personnel_info = get_personnel_info(session['user_id'])
    return render_template('vp&pres/vp-settings.html', **personnel_info)

@app.route('/logout')
def logout():
    """Logout with audit logging"""
    user_id = session.get('user_id')
    
    if user_id:
        try:
            conn = db_pool.get_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT lastlogin FROM users WHERE user_id = %s", (user_id,))
            last_login_result = cursor.fetchone()
            last_login = last_login_result[0] if last_login_result else None
            
            cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
            personnel_result = cursor.fetchone()
            
            philippines_tz = pytz.timezone('Asia/Manila')
            current_time = datetime.now(philippines_tz).replace(microsecond=0)
            
            if personnel_result:
                personnel_id = personnel_result[0]
                cursor.execute("UPDATE users SET lastlogout = %s WHERE user_id = %s", (current_time, user_id))
                
                last_login_str = last_login.strftime('%Y-%m-%d %H:%M:%S') if last_login else "Never"
                current_time_str = current_time.strftime('%Y-%m-%d %H:%M:%S')
                
                log_audit_action(
                    personnel_id,
                    "User logged out",
                    "User logged out of the system",
                    before_value=f"Last login: {last_login_str}",
                    after_value=f"Current logout: {current_time_str}"
                )
            
            conn.commit()
            cursor.close()
            db_pool.return_connection(conn)
        except Exception as e:
            print(f"Error logging logout action: {e}")
    
    session.clear()
    return redirect(url_for('login'))

# Test database connection
@app.route('/test-db')
def test_db():
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT version();")
        version = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        return f"Database connected successfully! Version: {version[0]}"
    except Exception as e:
        return f"Database connection failed: {e}"

 # CARDO CODES MWHEHEHEHE   
@app.route('/faculty/promotion/upload', methods=['POST'])
@require_auth([20001, 20002])
def promotion_document_upload():
    userid = session.get("user_id")
    if not userid:
        return "Unauthorized", 401

    # DEV: submission window check disabled for testing
    # Enforce the official promotion submission window: June 1 – August 31
    # from datetime import date as _date
    # _today = _date.today()
    # if not (6 <= _today.month <= 8):
    #     return (
    #         "Promotion applications are only accepted between June 1 and August 31 each year. "
    #         "Please resubmit during the official submission period."
    #     ), 403

    conn = db_pool.get_connection()
    cursor = conn.cursor()

    # Get faculty_id from personnel via user_id
    cursor.execute("""
        SELECT personnel_id FROM personnel WHERE user_id = %s
    """, (userid,))
    result = cursor.fetchone()
    if not result:
        cursor.close()
        db_pool.return_connection(conn)
        return "Faculty record not found for the current user.", 400

    faculty_id = result[0]

    # Get requested rank from form
    requested_rank = request.form.get('requested_rank')

    if not requested_rank:
        cursor.close()
        db_pool.return_connection(conn)
        return "Please select a rank to apply for.", 400

    resume_cv_data = None
    resume_cv_filename = None
    cover_letter_data = None
    cover_letter_filename = None

    # Get resume file using request.files.get()
    resume_file = request.files.get('resume_cv')
    if resume_file and resume_file.filename:
        resume_cv_data = resume_file.read()
        resume_cv_filename = resume_file.filename

    # Get cover letter file using request.files.get()
    cover_letter_file = request.files.get('cover_letter')
    if cover_letter_file and cover_letter_file.filename:
        cover_letter_data = cover_letter_file.read()
        cover_letter_filename = cover_letter_file.filename

    # Validate that both files are uploaded
    if not resume_cv_data or not cover_letter_data:
        cursor.close()
        db_pool.return_connection(conn)
        return "Both Resume/CV and Cover Letter are required.", 400
    
    # 1. Capture the new files from the form
    # Note: 'tor' and 'diploma' must match the 'name' attribute in your HTML <input>
    tor_file = request.files.get('tor')
    diploma_file = request.files.get('diploma')
    
    # capturing data and filenames
    tor_data = tor_file.read() if tor_file and tor_file.filename else None
    tor_name = tor_file.filename if tor_file and tor_file.filename else None
    
    diploma_data = diploma_file.read() if diploma_file and diploma_file.filename else None
    diploma_name = diploma_file.filename if diploma_file and diploma_file.filename else None
    
    # Insert new application row with requested_rank
    cursor.execute("""
        INSERT INTO promotion_application (
            faculty_id, cover_letter, resume, resume_filename, cover_letter_filename, 
            requested_rank, date_submitted, current_status,
            tor, tor_filename, diploma, diploma_filename
        ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s)
    """, (
        faculty_id, 
        cover_letter_data, resume_cv_data, resume_cv_filename, cover_letter_filename, 
        requested_rank, 'hrmd',
        tor_data, tor_name, diploma_data, diploma_name
    ))

    
    
    # Fetch faculty name for audit log and notification
    cursor.execute("SELECT firstname, lastname FROM personnel WHERE personnel_id = %s", (faculty_id,))
    fac_row = cursor.fetchone()
    fac_full_name = f"{fac_row[1]}, {fac_row[0]}" if fac_row else "A Faculty Member"

    conn.commit()
    cursor.close()
    db_pool.return_connection(conn)

    log_audit_action(
        faculty_id,
        'Promotion application submitted',
        f'{fac_full_name} submitted a promotion application (Applying For: {requested_rank})',
        before_value='Status: Not submitted',
        after_value=f'Applying For: {requested_rank} | Status: Awaiting HRMD Review | Documents: CV, Cover Letter, TOR, Diploma'
    )

    # Trigger the real-time notification
    trigger_promotion_notification(faculty_id, fac_full_name, requested_rank)

    return redirect(url_for('faculty_promotion'))


@app.route('/faculty/promotion/view_resume')
@require_auth([20001, 20002])
def view_resume():
    userid = session.get("user_id")
    if not userid:
        return "Unauthorized", 401

    conn = db_pool.get_connection()
    cursor = conn.cursor()

    # get faculty_id
    cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (userid,))
    res = cursor.fetchone()
    if not res:
        cursor.close()
        db_pool.return_connection(conn)
        return "Faculty record not found", 400

    faculty_id = res[0]

    # get latest application resume for user
    cursor.execute("""
        SELECT resume FROM promotion_application
        WHERE faculty_id = %s
        ORDER BY date_submitted DESC LIMIT 1
    """, (faculty_id,))
    result = cursor.fetchone()
    cursor.close()
    db_pool.return_connection(conn)

    if result and result[0]:
        return Response(result[0], mimetype='application/pdf', headers={"Content-Disposition": "inline; filename=resume.pdf"})
    else:
        return "Resume not found", 404

    
@app.route('/faculty/promotion/view_cover_letter')
@require_auth([20001, 20002])
def view_cover_letter():
    userid = session.get("user_id")
    if not userid:
        return "Unauthorized", 401

    conn = db_pool.get_connection()
    cursor = conn.cursor()

    # Get faculty_id from user_id
    cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (userid,))
    result = cursor.fetchone()
    if not result:
        cursor.close()
        db_pool.return_connection(conn)
        return "Faculty record not found", 400

    faculty_id = result[0]

    # Get latest cover letter
    cursor.execute("""
        SELECT cover_letter FROM promotion_application
        WHERE faculty_id = %s
        ORDER BY date_submitted DESC LIMIT 1
    """, (faculty_id,))

    cover_letter = cursor.fetchone()
    cursor.close()
    db_pool.return_connection(conn)

    if cover_letter and cover_letter[0]:
        return Response(
            cover_letter[0],
            mimetype='application/pdf',
            headers={"Content-Disposition": "inline; filename=cover_letter.pdf"}
        )
    else:
        return "Cover Letter not found", 404

# NEW CODES TOR AND DIPLOMA
@app.route('/faculty/promotion/view_tor')
@require_auth([20001, 20002, 20003, 20004])
def view_tor():
    userid = session.get("user_id")
    # If HR or VP is viewing a specific faculty, use that ID
    viewing_id = session.get('viewing_personnel_id')
    
    conn = db_pool.get_connection()
    cursor = conn.cursor()

    # Get the correct personnel_id to query
    if not viewing_id:
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (userid,))
        res = cursor.fetchone()
        target_faculty_id = res[0] if res else None
    else:
        target_faculty_id = viewing_id

    cursor.execute("""
        SELECT tor, tor_filename FROM promotion_application
        WHERE faculty_id = %s ORDER BY date_submitted DESC LIMIT 1
    """, (target_faculty_id,))
    
    result = cursor.fetchone()
    cursor.close()
    db_pool.return_connection(conn)

    if result and result[0]:
        return Response(result[0], mimetype='application/pdf', 
                        headers={"Content-Disposition": f"inline; filename={result[1]}"})
    return "TOR not found", 404

@app.route('/faculty/promotion/view_diploma')
@require_auth([20001, 20002, 20003, 20004])
def view_diploma():
    userid = session.get("user_id")
    viewing_id = session.get('viewing_personnel_id')
    
    conn = db_pool.get_connection()
    cursor = conn.cursor()

    if not viewing_id:
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (userid,))
        res = cursor.fetchone()
        target_faculty_id = res[0] if res else None
    else:
        target_faculty_id = viewing_id

    cursor.execute("""
        SELECT diploma, diploma_filename FROM promotion_application
        WHERE faculty_id = %s ORDER BY date_submitted DESC LIMIT 1
    """, (target_faculty_id,))
    
    result = cursor.fetchone()
    cursor.close()
    db_pool.return_connection(conn)

    if result and result[0]:
        return Response(result[0], mimetype='application/pdf', 
                        headers={"Content-Disposition": f"inline; filename={result[1]}"})
    return "Diploma/Certificate not found", 404



    
@app.route('/delete_submission', methods=['POST'])  # Changed route and added POST
@require_auth([20001, 20002])
def delete_submission():
    userid = session.get("user_id")
    if not userid:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    conn = db_pool.get_connection()
    cursor = conn.cursor()

    try:
        # Get faculty_id
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (userid,))
        result = cursor.fetchone()
        if not result:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Faculty record not found'}), 400

        faculty_id = result[0]

        # Delete only ACTIVE (non-finalized) application
        cursor.execute("""
            DELETE FROM promotion_application
            WHERE faculty_id = %s AND final_decision IS NULL
        """, (faculty_id,))
        
        deleted_count = cursor.rowcount
        
        if deleted_count == 0:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'No active application found to delete'}), 400

        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)

        return jsonify({'success': True, 'message': 'Application deleted successfully'})
    
    except Exception as e:
        conn.rollback()
        cursor.close()
        db_pool.return_connection(conn)
        return jsonify({'success': False, 'error': str(e)}), 500



@app.route('/api/promotion/details/<int:application_id>')
@require_auth([20003, 20004])
def get_promotion_details(application_id):
    """Get detailed promotion information - COMPLETE VERSION"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                pa.application_id,
                pa.faculty_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                p.phone,
                c.collegename,
                pr.position as currentrank,
                pa.current_status,
                pa.date_submitted,
                u.email,
                pa.cover_letter_filename,
                pa.resume_filename,
                pa.cover_letter,
                pa.resume,
                pa.requested_rank,
                pa.hrmd_remarks,
                pa.vpa_remarks,
                pa.pres_remarks,
                pa.rejection_reason,
                pa.tor_filename, pa.diploma_filename, -- New fields
                pa.tor, pa.diploma -- Binary checks
            FROM promotion_application pa
            JOIN personnel p ON pa.faculty_id = p.personnel_id
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            LEFT JOIN users u ON p.user_id = u.user_id
            WHERE pa.application_id = %s
        """, (application_id,))
        
        row = cursor.fetchone()
        
        if not row:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Application not found'), 404
        
        (app_id, faculty_id, firstname, lastname, honorifics, phone, college, 
         currentrank, status, submitted, email, cover_filename, resume_filename, 
         cover_data, resume_data, requested_rank, hrmd_remarks, vpa_remarks, 
         pres_remarks, rejection_reason, tor_filename, diploma_filename, tor_data, diploma_data) = row
        
        fullname = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
        
        # Get profile image
        profile_image_base64 = None
        cursor.execute("SELECT profilepic FROM profile WHERE personnel_id = %s", (faculty_id,))
        pic_row = cursor.fetchone()
        if pic_row and pic_row[0]:
            profile_image_base64 = f"data:image/jpeg;base64,{base64.b64encode(bytes(pic_row[0])).decode('utf-8')}"
        
        cursor.close()
        db_pool.return_connection(conn)
        
        return jsonify(
            success=True,
            data={
                'application_id': app_id,
                'faculty_id': faculty_id,
                'name': fullname,
                'phone': phone or 'N/A',
                'department': college or 'N/A',
                'currentrank': currentrank or 'Instructor',
                'requestedrank': requested_rank or 'Not Specified',
                'status': status.replace('_', ' ').title() if status else 'Pending',
                'submitteddate': submitted.strftime('%Y-%m-%d') if submitted else 'N/A',
                'email': email or 'N/A',
                'profileimage': profile_image_base64,
                'has_cover_letter': cover_data is not None,
                'has_resume': resume_data is not None,
                'cover_letter_filename': cover_filename,
                'resume_filename': resume_filename,
                'tor_filename': tor_filename,
                'diploma_filename': diploma_filename,
                'has_tor': tor_data is not None,
                'has_diploma': diploma_data is not None,
                'hrmd_remarks': hrmd_remarks,
                'vpa_remarks': vpa_remarks,
                'pres_remarks': pres_remarks,
                'rejection_reason': rejection_reason
            }
        )
        
    except Exception as e:
        print(f"Error fetching promotion details: {e}")
        import traceback
        traceback.print_exc()
        return jsonify(success=False, error=str(e)), 500


@app.route('/api/profile/image-base64/<int:personnel_id>')
@require_auth([20001, 20002, 20003, 20004])
def get_profile_image_base64(personnel_id):
    """Get profile picture as base64 for embedding in HTML"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT profilepic FROM profile WHERE personnel_id = %s",
            (personnel_id,)
        )
        
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if result and result[0]:
            binary_image = bytes(result[0])
            
            # Try to resize with PIL
            try:
                from PIL import Image
                import io
                
                img = Image.open(io.BytesIO(binary_image))
                
                # Convert palette/RGBA images to RGB for JPEG compatibility
                if img.mode in ('P', 'RGBA', 'LA'):
                    # Create white background for transparent images
                    rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    rgb_img.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                    img = rgb_img
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                
                # Resize
                img.thumbnail((100, 100), Image.Resampling.LANCZOS)
                
                # Save as JPEG
                buffer = io.BytesIO()
                img.save(buffer, format='JPEG', quality=85)
                buffer.seek(0)
                
                base64_image = base64.b64encode(buffer.getvalue()).decode('utf-8')
            except Exception as resize_error:
                print(f"Could not resize image, using original: {resize_error}")
                # Fallback to original if resize fails
                base64_image = base64.b64encode(binary_image).decode('utf-8')
            
            return jsonify({
                'success': True,
                'image': f"data:image/jpeg;base64,{base64_image}"
            })
        else:
            return jsonify({'success': False, 'image': None})
            
    except Exception as e:
        print(f"Error fetching profile image: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500




@app.route('/api/promotion/document/<int:application_id>/<doc_type>')
@require_auth([20003, 20004])
def get_promotion_document(application_id, doc_type):
    """Serve PDF documents from promotion_application table"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        # Validate doc_type
        valid_types = ['cover_letter', 'resume', 'tor', 'diploma']
        if doc_type not in valid_types:
            return {'error': 'Invalid document type'}, 400
        
        # Fetch the document
        cursor.execute(f"""
            SELECT {doc_type}
            FROM promotion_application 
            WHERE application_id = %s
        """, (application_id,))
        
        result = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)
        
        if result and result[0]:
            binary_pdf = bytes(result[0])
            
            response = make_response(binary_pdf)
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Disposition'] = 'inline; filename=document.pdf'
            return response
        else:
            return {'error': 'Document not found'}, 404
            
    except Exception as e:
        print(f"Error fetching document: {e}")
        import traceback
        traceback.print_exc()
        return {'error': str(e)}, 500

@app.route('/api/promotion/forward-to-vpaa', methods=['POST'])
@require_auth([20003])
def forward_to_vpaa():
    """HR forwards promotion application to VPAA"""
    try:
        data = request.get_json()
        application_id = data.get('application_id')
        hrmd_remarks = data.get('hrmd_remarks', '').strip()
        
        if not application_id:
            return jsonify(success=False, error='Application ID is required'), 400
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        # Verify the application is at the hrmd or pending stage
        cursor.execute(
            "SELECT current_status FROM promotion_application WHERE application_id = %s",
            (application_id,)
        )
        status_row = cursor.fetchone()
        if not status_row:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Application not found'), 404
        if status_row[0] not in ('hrmd', 'pending'):
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Application is not at HRMD review stage'), 400

        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz)

        # Update status to VPA and save HRMD remarks
        cursor.execute(
            """UPDATE promotion_application
               SET current_status = %s,
                   hrmd_approval_date = %s,
                   hrmd_remarks = %s
               WHERE application_id = %s""",
            ('vpa', current_time, hrmd_remarks if hrmd_remarks else None, application_id)
        )
        
        # Fetch faculty name for notification before releasing connection
        cursor.execute(
            "SELECT p.firstname, p.lastname, pa.requested_rank "
            "FROM promotion_application pa JOIN personnel p ON pa.faculty_id = p.personnel_id "
            "WHERE pa.application_id = %s", (application_id,)
        )
        fac_row = cursor.fetchone()

        conn.commit()

        cursor.close()
        db_pool.return_connection(conn)

        user_id = session.get('user_id')
        hr_info = get_personnel_info(user_id)
        hr_personnel_id = hr_info.get('personnel_id')

        if fac_row:
            fac_name = f"{fac_row[1]}, {fac_row[0]}"
            fac_rank = fac_row[2]
            log_audit_action(
                hr_personnel_id,
                'Promotion forwarded to VPAA',
                f'HR forwarded {fac_name}\'s promotion application (App ID: {application_id}) for {fac_rank}',
                before_value=f'Faculty: {fac_name} | Applying For: {fac_rank} | Status: HRMD Review',
                after_value='Status: VPAA Review' + (f' | HR Notes: {hrmd_remarks}' if hrmd_remarks else '')
            )
            trigger_promotion_forwarded_vpaa(fac_name, fac_rank)
        else:
            log_audit_action(
                hr_personnel_id,
                'Promotion forwarded to VPAA',
                f'HR forwarded promotion application (App ID: {application_id}) to VPAA',
                before_value='Status: HRMD Review',
                after_value='Status: VPAA Review' + (f' | HR Notes: {hrmd_remarks}' if hrmd_remarks else '')
            )

        return jsonify(success=True, message='Application forwarded to VPAA successfully')
        
    except Exception as e:
        print(f"Error forwarding to VPAA: {str(e)}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return jsonify(success=False, error=str(e)), 500


@app.route('/api/promotion/approve', methods=['POST'])
@require_auth([20004])
def approve_promotion():
    """Final approval of promotion application by President"""
    try:
        data = request.get_json()
        application_id = data.get('application_id')
        pres_remarks = data.get('pres_remarks', '').strip()
        
        if not application_id:
            return jsonify(success=False, error='Application ID is required'), 400
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        user_id = session.get('user_id')
        cursor.execute(
            """
            SELECT pr.position 
            FROM personnel p
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE p.user_id = %s
            """,
            (user_id,)
        )
        position_result = cursor.fetchone()
        
        if not position_result or position_result[0] != 'President':
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Unauthorized: Only President can approve promotions'), 403
        
        # Get application details
        cursor.execute(
            "SELECT faculty_id, requested_rank FROM promotion_application WHERE application_id = %s",
            (application_id,)
        )
        result = cursor.fetchone()
        
        if not result:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Application not found'), 404
        
        faculty_id, requested_rank = result

        cursor.execute("SELECT firstname, lastname FROM personnel WHERE personnel_id = %s", (faculty_id,))
        name_row = cursor.fetchone()
        fac_name = f"{name_row[1]}, {name_row[0]}" if name_row else f"Faculty (ID: {faculty_id})"

        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz)
        
        # Update promotion application to approved
        cursor.execute(
            """UPDATE promotion_application 
               SET current_status = %s, 
                   pres_approval_date = %s, 
                   final_decision = %s,
                   pres_remarks = %s
               WHERE application_id = %s""",
            ('approved', current_time, 1, pres_remarks if pres_remarks else None, application_id)
        )
        
        # Update faculty rank in profile table
        cursor.execute(
            "UPDATE profile SET position = %s WHERE personnel_id = %s",
            (requested_rank, faculty_id)
        )

        # Generate promotion letter PDF with ReportLab
        from io import BytesIO
        import os as _os
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.lib import colors as rl_colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY

        # Fetch the President's full name (firstname + lastname + honorifics/suffix)
        cursor.execute("""
            SELECT p.firstname, p.lastname, p.honorifics
            FROM personnel p
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE pr.position = 'President'
            LIMIT 1
        """)
        pres_row = cursor.fetchone()
        if pres_row:
            pres_fn, pres_ln, pres_sfx = pres_row
            pres_fullname = f"{pres_fn} {pres_ln}"
            if pres_sfx:
                pres_fullname += f", {pres_sfx}"
        else:
            pres_fullname = "The President"

        buf = BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=(8.5 * inch, 11 * inch),
                                leftMargin=1.2*inch, rightMargin=1.2*inch,
                                topMargin=1*inch, bottomMargin=1*inch)
        styles = getSampleStyleSheet()
        heading  = ParagraphStyle('heading',  parent=styles['Normal'], fontSize=13, leading=16, alignment=TA_CENTER, fontName='Helvetica-Bold')
        subhead  = ParagraphStyle('subhead',  parent=styles['Normal'], fontSize=11, leading=14, alignment=TA_CENTER, fontName='Helvetica-Bold')
        body_j   = ParagraphStyle('body_j',   parent=styles['Normal'], fontSize=10.5, leading=15, alignment=TA_JUSTIFY, fontName='Helvetica')
        body_l   = ParagraphStyle('body_l',   parent=styles['Normal'], fontSize=10.5, leading=15, alignment=TA_LEFT,    fontName='Helvetica')
        bold_l   = ParagraphStyle('bold_l',   parent=styles['Normal'], fontSize=10.5, leading=15, alignment=TA_LEFT,    fontName='Helvetica-Bold')
        muted    = ParagraphStyle('muted',    parent=styles['Normal'], fontSize=9,    leading=12, alignment=TA_CENTER,  fontName='Helvetica', textColor=rl_colors.grey)

        ph_tz = pytz.timezone('Asia/Manila')
        letter_date = datetime.now(ph_tz).strftime('%B %d, %Y')
        fullname_display = name_row[0] + ' ' + name_row[1] if name_row else fac_name

        logo_path = _os.path.join(_os.path.dirname(__file__), 'static', 'img', 'spc_logo.png')

        story = []

        # Logo header
        if _os.path.exists(logo_path):
            logo = RLImage(logo_path, width=0.85*inch, height=0.85*inch)
            logo.hAlign = 'CENTER'
            story.append(logo)
            story.append(Spacer(1, 0.08*inch))

        story += [
            Paragraph("ST. PETER'S COLLEGE", heading),
            Paragraph("Iligan City, Philippines", subhead),
            Spacer(1, 0.1*inch),
            Paragraph("OFFICE OF THE PRESIDENT", subhead),
            Spacer(1, 0.3*inch),
            Paragraph(letter_date, body_l),
            Spacer(1, 0.15*inch),
            Paragraph(f"<b>{fullname_display}</b>", body_l),
            Paragraph("Faculty Member", body_l),
            Paragraph("St. Peter's College", body_l),
            Spacer(1, 0.25*inch),
            Paragraph("Dear Faculty Member,", body_l),
            Spacer(1, 0.15*inch),
            Paragraph(
                f"On behalf of the Administration of St. Peter's College, I am pleased to inform you that your "
                f"application for promotion has been reviewed and approved by the Office of the President.",
                body_j),
            Spacer(1, 0.15*inch),
            Paragraph(
                f"Effective immediately, you are hereby promoted to the rank of <b>{requested_rank}</b>. "
                "This promotion reflects your dedication, exemplary performance, and outstanding contributions "
                "to the academic community of St. Peter's College.",
                body_j),
            Spacer(1, 0.15*inch),
            Paragraph(
                "Please coordinate with the Human Resource Management Division (HRMD) for the necessary "
                "documentary requirements and adjustments to your compensation and benefits. "
                "Congratulations on this well-deserved achievement.",
                body_j),
            Spacer(1, 0.25*inch),
            Paragraph("Sincerely,", body_l),
            Spacer(1, 0.5*inch),
            Paragraph(f"<b>{pres_fullname}</b>", bold_l),
            Paragraph("President, St. Peter's College", body_l),
            Spacer(1, 0.4*inch),
            Paragraph(
                "This letter is system-generated and serves as official notification of your promotion. "
                "Please acknowledge receipt by clicking the Acknowledge button in the Faculty Portal.",
                muted),
        ]
        doc.build(story)
        letter_bytes = buf.getvalue()

        cursor.execute(
            """UPDATE promotion_application
               SET promotion_letter = %s, letter_generated_at = %s, letter_acknowledged = FALSE
               WHERE application_id = %s""",
            (letter_bytes, current_time, application_id)
        )

        conn.commit()

        # Notify the faculty member via SSE
        _push_to_queue(faculty_id, {
            'notification_type': 'promotion',
            'action': 'approved',
            'person_name': fac_name,
            'requested_rank': requested_rank,
            'message': f'Congratulations! Your promotion application to {requested_rank} has been approved.',
            'application_id': application_id,
            'tap_time': datetime.now(ph_tz).strftime('%A, %B %d, %Y %I:%M %p'),
        })

        # Audit log
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')

        if personnel_id:
            log_audit_action(
                personnel_id,
                'Promotion approved',
                f'President approved promotion for {fac_name} (App ID: {application_id})',
                before_value=f'Faculty: {fac_name} | Status: President Review',
                after_value=f'Status: Approved | New Rank: {requested_rank}' + (f' | President Notes: {pres_remarks}' if pres_remarks else '')
            )

        cursor.close()
        db_pool.return_connection(conn)

        return jsonify(success=True, message=f'Promotion approved! Faculty rank updated to {requested_rank}')
        
    except Exception as e:
        print(f"Error approving promotion: {str(e)}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return jsonify(success=False, error=str(e)), 500


@app.route('/api/promotion/reject', methods=['POST'])
@require_auth([20003, 20004])
def reject_promotion():
    """Reject promotion application - Does NOT update faculty rank"""
    try:
        data = request.get_json()
        application_id = data.get('application_id')
        rejection_reason = data.get('rejection_reason', '').strip()
        
        if not application_id:
            return jsonify(success=False, error='Application ID is required'), 400
        
        if not rejection_reason:
            return jsonify(success=False, error='Rejection reason is required'), 400
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        # Get current status, final_decision, and faculty name
        cursor.execute("""
            SELECT pa.current_status, p.firstname, p.lastname, pa.requested_rank, pa.faculty_id, pa.final_decision
            FROM promotion_application pa
            JOIN personnel p ON pa.faculty_id = p.personnel_id
            WHERE pa.application_id = %s
        """, (application_id,))
        result = cursor.fetchone()

        if not result:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Application not found'), 404

        current_status, fac_firstname, fac_lastname, requested_rank, rejected_faculty_id, final_decision = result

        if final_decision is not None:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Application has already been finalized'), 400
        fac_name = f"{fac_lastname}, {fac_firstname}"

        # Update status to rejected with rejection reason
        cursor.execute(
            """UPDATE promotion_application
               SET current_status = %s,
                   final_decision = %s,
                   rejection_reason = %s
               WHERE application_id = %s""",
            ('rejected', 0, rejection_reason, application_id)
        )

        conn.commit()

        # Notify the faculty member via SSE
        ph_tz = pytz.timezone('Asia/Manila')
        _push_to_queue(rejected_faculty_id, {
            'notification_type': 'promotion',
            'action': 'rejected',
            'person_name': fac_name,
            'requested_rank': requested_rank,
            'message': f'Your promotion application to {requested_rank} has been rejected. Reason: {rejection_reason}',
            'application_id': application_id,
            'tap_time': datetime.now(ph_tz).strftime('%A, %B %d, %Y %I:%M %p'),
        })

        # Audit log
        user_id = session.get('user_id')
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')

        if personnel_id:
            _stage_labels = {'hrmd': 'HRMD Review', 'vpa': 'VPAA Review', 'pres': 'President Review'}
            stage_label = _stage_labels.get(current_status, current_status.title() if current_status else 'Unknown')
            log_audit_action(
                personnel_id,
                'Promotion rejected',
                f'Promotion rejected for {fac_name} (App ID: {application_id}) — Applying for: {requested_rank}',
                before_value=f'Faculty: {fac_name} | Applying For: {requested_rank} | Status: {stage_label}',
                after_value=f'Status: Rejected | Reason: {rejection_reason}'
            )

        cursor.close()
        db_pool.return_connection(conn)

        return jsonify(success=True, message='Promotion application rejected')
        
    except Exception as e:
        print(f"Error rejecting promotion: {str(e)}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return jsonify(success=False, error=str(e)), 500


@app.route('/api/promotion/letter/<int:application_id>')
@require_auth([20001, 20002])
def get_promotion_letter(application_id):
    """Stream the promotion letter PDF for a given application."""
    try:
        user_id = session.get('user_id')
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        # Resolve the logged-in faculty's personnel_id
        cursor.execute(
            "SELECT p.personnel_id FROM personnel p WHERE p.user_id = %s",
            (user_id,)
        )
        pid_row = cursor.fetchone()
        if not pid_row:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Personnel record not found'), 404
        logged_in_personnel_id = pid_row[0]

        cursor.execute(
            "SELECT promotion_letter, faculty_id FROM promotion_application WHERE application_id = %s",
            (application_id,)
        )
        row = cursor.fetchone()
        cursor.close()
        db_pool.return_connection(conn)

        if not row or row[0] is None:
            return jsonify(success=False, error='Letter not found'), 404

        letter_bytes, owner_faculty_id = row
        if logged_in_personnel_id != owner_faculty_id:
            return jsonify(success=False, error='Access denied'), 403

        from flask import Response
        return Response(
            bytes(letter_bytes),
            mimetype='application/pdf',
            headers={'Content-Disposition': f'inline; filename="promotion_letter_{application_id}.pdf"'}
        )
    except Exception as e:
        print(f"Error serving promotion letter: {e}")
        return jsonify(success=False, error=str(e)), 500


@app.route('/api/promotion/acknowledge/<int:application_id>', methods=['POST'])
@require_auth([20001, 20002])
def acknowledge_promotion_letter(application_id):
    """Mark the promotion letter as acknowledged by the faculty."""
    conn = None
    try:
        user_id = session.get('user_id')
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT p.personnel_id FROM personnel p WHERE p.user_id = %s",
            (user_id,)
        )
        pid_row = cursor.fetchone()
        if not pid_row:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Personnel record not found'), 404
        logged_in_personnel_id = pid_row[0]

        cursor.execute(
            "SELECT faculty_id FROM promotion_application WHERE application_id = %s AND final_decision = 1",
            (application_id,)
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Application not found or not yet approved'), 404

        if logged_in_personnel_id != row[0]:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify(success=False, error='Access denied'), 403

        cursor.execute(
            "UPDATE promotion_application SET letter_acknowledged = TRUE WHERE application_id = %s",
            (application_id,)
        )
        conn.commit()
        cursor.close()
        db_pool.return_connection(conn)
        return jsonify(success=True, message='Letter acknowledged')
    except Exception as e:
        print(f"Error acknowledging promotion letter: {e}")
        if conn:
            conn.rollback()
        return jsonify(success=False, error=str(e)), 500


@app.route('/api/regularization/eligible-faculty')
@require_auth([20003])
def get_eligible_faculty():
    """Get list of faculty eligible for regularization"""
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT
                p.personnel_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                p.hiredate,
                c.collegename,
                pr.position as current_rank,
                COALESCE(pr.probationary_start_date, p.hiredate) AS prob_start,
                (CURRENT_DATE - COALESCE(pr.probationary_start_date, p.hiredate))::float / 365.25 AS years_of_service
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE
                p.hiredate IS NOT NULL
                AND pr.has_aligned_master = TRUE
                AND (CURRENT_DATE - COALESCE(pr.probationary_start_date, p.hiredate))::float / 365.25 >= 3
                AND p.personnel_id NOT IN (
                    SELECT faculty_id
                    FROM regularization_application
                    WHERE final_decision IS NULL
                )
            ORDER BY COALESCE(pr.probationary_start_date, p.hiredate) ASC
        """)
        
        eligible_faculty = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)
        
        faculty_list = []
        for f in eligible_faculty:
            (personnel_id, firstname, lastname, honorifics, hiredate,
             college, rank, prob_start, years) = f
            
            fullname = f"{lastname}, {firstname}, {honorifics}" if honorifics else f"{lastname}, {firstname}"
            
            if years >= 7:
                eligible_for = "Tenured"
                current_tenure = "Regular"
            elif years >= 3:
                eligible_for = "Regular"
                current_tenure = "Probationary"
            else:
                continue
            
            faculty_list.append({
                'personnel_id': personnel_id,
                'name': fullname,
                'department': college or 'N/A',
                'current_rank': rank or 'Instructor',
                'current_tenure': current_tenure,
                'years_of_service': round(float(years), 2) if years else 0,
                'hiredate': hiredate.strftime('%Y-%m-%d') if hiredate else 'N/A',
                'eligible_for': eligible_for
            })
        
        return jsonify({'success': True, 'eligible_faculty': faculty_list})
    
    except Exception as e:
        print(f"Error getting eligible faculty: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/regularization/initiate', methods=['POST'])
@require_auth([20003])
def initiate_regularization():
    """HR initiates regularization for eligible faculty"""
    try:
        data = request.get_json()
        faculty_id = data.get('faculty_id')
        notes = data.get('notes', '')
        
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        
        # Fetch hire date, aligned master flag, and probationary start date
        cursor.execute("""
            SELECT p.hiredate, pr.has_aligned_master,
                   COALESCE(pr.probationary_start_date, p.hiredate) AS prob_start
            FROM personnel p
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE p.personnel_id = %s
        """, (faculty_id,))
        result = cursor.fetchone()

        if not result or not result[0]:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Faculty not found or no hire date'}), 400

        hire_date, has_aligned_master, prob_start = result

        if not has_aligned_master:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({
                'success': False,
                'error': 'Faculty is not eligible. An aligned Master\'s Degree is required to begin the probationary period.'
            }), 400

        from datetime import date
        today = date.today()
        years_of_service = (today - prob_start).days / 365.25 if prob_start else 0

        if years_of_service < 3:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({
                'success': False,
                'error': f'Faculty not eligible. Only {years_of_service:.1f} probationary years completed (3 required).'
            }), 400
        
        cursor.execute("""
            SELECT regularization_id FROM regularization_application 
            WHERE faculty_id = %s AND final_decision IS NULL
        """, (faculty_id,))
        
        if cursor.fetchone():
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'success': False, 'error': 'Faculty already has active regularization'}), 400
        
        import pytz
        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz)
        
        cursor.execute("""
            INSERT INTO regularization_application 
            (faculty_id, years_of_service, date_initiated, current_status, eligibility_notes, hrmd_endorsement_date)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING regularization_id
        """, (faculty_id, years_of_service, current_time, 'vpa', notes, current_time))
        
        reg_id = cursor.fetchone()[0]

        cursor.execute("SELECT firstname, lastname FROM personnel WHERE personnel_id = %s", (faculty_id,))
        name_row = cursor.fetchone()
        fac_name = f"{name_row[1]}, {name_row[0]}" if name_row else f"Faculty (ID: {faculty_id})"

        conn.commit()

        next_tenure = "Tenured" if years_of_service >= 7 else "Regular"

        user_id = session.get('user_id')
        personnel_info = get_personnel_info(user_id)
        hr_personnel_id = personnel_info.get('personnel_id')

        if hr_personnel_id:
            log_audit_action(
                hr_personnel_id,
                'Regularization initiated',
                f'HR initiated regularization for {fac_name} (Reg ID: {reg_id})',
                before_value=f'Faculty: {fac_name} | Service: {years_of_service:.1f} yrs | Status: Probationary',
                after_value=f'Status: Awaiting VPAA Review | Target: {next_tenure}' + (f' | HR Notes: {notes}' if notes else '')
            )

        cursor.close()
        db_pool.return_connection(conn)
        
        return jsonify({
            'success': True, 
            'message': f'Regularization to {next_tenure} status initiated successfully',
            'regularization_id': reg_id
        })
    
    except Exception as e:
        print(f"Error initiating regularization: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
            cursor.close()
            db_pool.return_connection(conn)
        return jsonify({'success': False, 'error': str(e)}), 500


# Rank promotion ladder and per-rank requirements
_RANK_LADDER = [
    'Associate Instructor',
    'Instructor',
    'Assistant Professor',
    'Associate Professor',
    'Professor',
]

_RANK_REQUIREMENTS = {
    'Associate Instructor': {'degree': 'Bachelor',  'years': 3},
    'Instructor':           {'degree': 'Master',    'years': 4},
    'Assistant Professor':  {'degree': 'Master',    'years': 5},
    'Associate Professor':  {'degree': 'Doctorate', 'years': 9},
    'Professor':            {'degree': 'Doctorate', 'years': 10},
}


def _has_degree(required_degree, has_aligned_master, has_doctorate):
    """Return True if the faculty holds at least the required degree level.
    Doctorate is treated as satisfying a Master's requirement (hierarchical).
    """
    if required_degree == 'Bachelor':
        return True
    if required_degree == 'Master':
        return bool(has_aligned_master or has_doctorate)
    if required_degree == 'Doctorate':
        return bool(has_doctorate)
    return False


@app.route('/api/faculty/promotion/eligibility')
@require_auth([20001, 20002])
def get_promotion_eligibility():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    conn = db_pool.get_connection()
    cursor = conn.cursor()

    # Get faculty info, hire date, employment status, degree flags, current rank, and current term
    cursor.execute("""
        SELECT
            p.personnel_id,
            p.hiredate,
            pr.employmentstatus,
            pr.position,
            pr.has_aligned_master,
            pr.has_doctorate,
            (SELECT acadcalendar_id
             FROM acadcalendar
             WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
             ORDER BY semesterstart DESC
             LIMIT 1)
        FROM personnel p
        LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
        WHERE p.user_id = %s
    """, (user_id,))

    result = cursor.fetchone()
    if not result:
        cursor.close()
        db_pool.return_connection(conn)
        return jsonify({'success': False, 'error': 'Faculty not found'}), 404

    faculty_id, hire_date, employment_status, current_rank, has_aligned_master, has_doctorate, current_term_id = result

    # Calculate years of teaching from hire date
    from datetime import date
    today = date.today()
    years_employed = 0

    if hire_date:
        years_employed = today.year - hire_date.year
        if (today.month < hire_date.month) or (today.month == hire_date.month and today.day < hire_date.day):
            years_employed -= 1

    # Check attendance rate
    cursor.execute("""
        SELECT COALESCE(AVG(ar.attendancerate), 0) AS avg_attendance_rate
        FROM attendancereport ar
        WHERE ar.personnel_id = %s AND ar.acadcalendar_id = %s
    """, (faculty_id, current_term_id))
    avg_attendance_rate = float(cursor.fetchone()[0] or 0)

    # Check weighted evaluation score
    cursor.execute("""
        SELECT COALESCE(
            SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) +
            SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) +
            SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END),
            0
        ) AS weighted_eval_score
        FROM faculty_evaluations fe
        WHERE fe.personnel_id = %s AND fe.acadcalendar_id = %s
    """, (faculty_id, current_term_id))
    weighted_eval_score = float(cursor.fetchone()[0] or 0)

    # Check for active application
    cursor.execute("""
        SELECT COUNT(*)
        FROM promotion_application
        WHERE faculty_id = %s AND final_decision IS NULL
    """, (faculty_id,))
    has_active_application = cursor.fetchone()[0] > 0

    cursor.close()
    db_pool.return_connection(conn)

    # --- Determine next rank and rank-based requirements ---

    # Find the faculty's position in the ladder; default to bottom if unrecognized
    try:
        current_index = _RANK_LADDER.index(current_rank)
    except (ValueError, TypeError):
        current_index = -1  # treat as below Associate Instructor

    if current_index >= len(_RANK_LADDER) - 1:
        # Already at the top rank — cannot promote further
        return jsonify({
            'success': True,
            'can_apply': False,
            'has_active_application': has_active_application,
            'tenure_type': employment_status or 'Regular',
            'years_employed': years_employed,
            'attendance_rate': avg_attendance_rate,
            'eval_score': weighted_eval_score,
            'current_rank': current_rank,
            'next_rank': None,
            'required_degree': None,
            'required_years': None,
            'lock_reasons': ['Already at the highest rank (Professor)']
        })

    next_rank = _RANK_LADDER[current_index + 1]
    req = _RANK_REQUIREMENTS[next_rank]
    required_degree = req['degree']
    required_years = req['years']

    is_degree_ok = _has_degree(required_degree, has_aligned_master, has_doctorate)
    is_years_ok = years_employed >= required_years
    is_attendance_ok = avg_attendance_rate >= 80.0
    is_eval_ok = weighted_eval_score >= 3.0

    can_apply = is_degree_ok and is_years_ok and is_attendance_ok and is_eval_ok

    # Determine tenure type for display
    tenure_type = employment_status
    if not tenure_type:
        if years_employed >= 7:
            tenure_type = 'Tenured'
        elif years_employed >= 3:
            tenure_type = 'Regular'
        else:
            tenure_type = 'Probationary'

    lock_reasons = []

    if not is_degree_ok:
        lock_reasons.append(f"Degree: {required_degree}'s Degree required for {next_rank}")
    if not is_years_ok:
        years_needed = required_years - years_employed
        lock_reasons.append(f"Teaching experience: {years_needed} more year(s) needed (requires {required_years} years for {next_rank})")
    if not is_attendance_ok:
        lock_reasons.append(f"Attendance: {avg_attendance_rate:.1f}% (needs 80%+)")
    if not is_eval_ok:
        lock_reasons.append(f"Evaluation: {weighted_eval_score:.2f} (needs 3.00+)")

    return jsonify({
        'success': True,
        'can_apply': can_apply and not has_active_application,
        'has_active_application': has_active_application,
        'tenure_type': tenure_type,
        'years_employed': years_employed,
        'attendance_rate': avg_attendance_rate,
        'eval_score': weighted_eval_score,
        'current_rank': current_rank,
        'next_rank': next_rank,
        'required_degree': required_degree,
        'required_years': required_years,
        'is_degree_ok': is_degree_ok,
        'is_years_ok': is_years_ok,
        'lock_reasons': lock_reasons
    })


@app.route('/api/promotion/list')
@require_auth([20003, 20004])
def get_promotion_list():
    status_filter = request.args.get('status', None)

    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        if status_filter:
            cursor.execute("""
                SELECT application_id, faculty_id, current_status, requested_rank, date_submitted
                FROM promotion_application
                WHERE LOWER(current_status::text) = LOWER(%s)
                ORDER BY date_submitted DESC
            """, (status_filter,))
        else:
            cursor.execute("""
                SELECT application_id, faculty_id, current_status, requested_rank, date_submitted
                FROM promotion_application
                ORDER BY date_submitted DESC
            """)

        rows = cursor.fetchall()
        cursor.close()
        db_pool.return_connection(conn)

        data = [
            {
                'application_id': row[0],
                'faculty_id': row[1],
                'current_status': row[2],
                'requested_rank': row[3],
                'date_submitted': row[4].strftime('%Y-%m-%d') if row[4] else None
            }
            for row in rows
        ]

        return jsonify({'success': True, 'data': data})

    except Exception as e:
        print(f'Error fetching promotion list: {str(e)}')
        return jsonify({'success': False, 'error': 'Failed to fetch promotion list'}), 500
    
@app.route('/api/audit-logs')
@require_auth([20003])  # HR role only
def get_audit_logs():
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT a.audit_id, a.action, a.details, a.created_at,
                   CONCAT(p.firstname, ' ', p.lastname) AS performed_by_name
            FROM auditlogs a
            LEFT JOIN personnel p ON a.personnel_id = p.personnel_id
            WHERE a.action ILIKE '%Promotion%'
               OR a.action ILIKE '%Regularization%'
               OR a.action ILIKE '%Degree info%'
            ORDER BY a.created_at DESC
            LIMIT 200
        """)
        rows = cursor.fetchall()

        audit_events = []
        for row in rows:
            audit_id, action, details, created_at, performed_by = row

            if 'Promotion' in action:
                evt_type = 'Promotion'
            elif 'Regularization' in action:
                evt_type = 'Regularization'
            else:
                evt_type = 'Degree'

            audit_events.append({
                'type': evt_type,
                'event': action,
                'notes': details,
                'timestamp': created_at.isoformat() if isinstance(created_at, datetime) else str(created_at),
                'performed_by': performed_by or 'System'
            })

        cursor.close()
        db_pool.return_connection(conn)

        return jsonify({'success': True, 'audit_events': audit_events})

    except Exception as e:
        print(f"Audit log fetch error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/regularization/check-status/<int:faculty_id>')
@require_auth([20001, 20002, 20003, 20004])
def check_regularization_status(faculty_id):
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT pr.has_aligned_master,
                   COALESCE(pr.probationary_start_date, p.hiredate) AS prob_start,
                   pr.employmentstatus
            FROM profile pr
            JOIN personnel p ON pr.personnel_id = p.personnel_id
            WHERE p.personnel_id = %s
        """, (faculty_id,))

        res = cursor.fetchone()
        if not res:
            cursor.close()
            db_pool.return_connection(conn)
            return jsonify({'eligible': False, 'category': 'Unknown', 'reason': 'Faculty record not found.'}), 404
        has_master, prob_start, current_status = res

        if not has_master:
            return jsonify({
                'eligible': False,
                'category': 'Contractual',
                'reason': 'Requires a vertically aligned Master\'s Degree to begin the probationary period.'
            })

        # Countdown starts from probationary_start_date, not hire date
        years_diff = (datetime.now().date() - prob_start).days / 365.25 if prob_start else 0
        if years_diff < 3:
            return jsonify({
                'eligible': False,
                'category': 'Probationary',
                'reason': f'Probation in progress ({round(years_diff, 1)}/3 years completed from probationary start).'
            })

        return jsonify({'eligible': True, 'category': 'Regular', 'reason': 'Ready for HRMD final review.'})
    finally:
        cursor.close()
        db_pool.return_connection(conn)


if __name__ == "__main__":
    app.run(debug=True)