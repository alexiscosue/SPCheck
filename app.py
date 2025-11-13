import os
from datetime import datetime, timedelta, date
import pytz
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from dotenv import load_dotenv
import pg8000
from pg8000 import dbapi
from rfid_reader import RFIDReader
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


load_dotenv()

app = Flask(__name__)
app.secret_key = 'spc-faculty-system-2025-secret-key'

# ========== SESSION CONFIGURATION ==========
app.config['SESSION_PERMANENT'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=60)  # 1 hour max session

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
            host=os.getenv('DB_HOST', 'dpg-d48ontqli9vc739e8i90-a.oregon-postgres.render.com'),
            port=int(os.getenv('DB_PORT', 5432)),
            database=os.getenv('DB_NAME', 'spcheck_nf0n'),
            user=os.getenv('DB_USER', 'spcheck_user'),
            password=os.getenv('DB_PASSWORD', 'lW8SHs3IYfSzdldtFVSgfdnlIaguJhtf'),
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
rfid_reader = RFIDReader(db_pool)

absence_checker_thread = None
absence_checker_running = False

rfid_reader_state = {
    'is_running': False,
    'port': None,
    'started_by': None, 
    'started_at': None
}
rfid_state_lock = threading.Lock()

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

def check_and_record_absences():
    """Independent absence checker - calculates dates from schedule and records absences"""
    global absence_checker_running
    
    while absence_checker_running:
        try:
            philippines_tz = pytz.timezone('Asia/Manila')
            current_time = datetime.now(philippines_tz)
            current_date = current_time.date()
            current_time_only = current_time.time()
            
            conn = get_db_connection()
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
                                cursor.execute("SELECT COALESCE(MAX(attendance_id), 70000) FROM attendance")
                                new_id = cursor.fetchone()[0] + 1
                                
                                naive_midnight = datetime.combine(check_date, datetime.min.time())
                                absence_timestamp = philippines_tz.localize(naive_midnight)
                                
                                cursor.execute("""
                                    INSERT INTO attendance (
                                        attendance_id, personnel_id, class_id, 
                                        attendancestatus, timein, timeout
                                    )
                                    VALUES (%s, %s, %s, %s, %s, NULL)
                                """, (new_id, personnel_id, class_id, 'Absent', absence_timestamp))
                                
                                absences_recorded += 1
                                
                                print(f"ABSENCE RECORDED: {firstname} {lastname} - {subject_code} on {check_date}")
                        
                        check_date += timedelta(days=7)
            
            if absences_recorded > 0:
                conn.commit()
                print(f"Total absences recorded: {absences_recorded}")
            
            cursor.close()
            return_db_connection(conn)
            
        except Exception as e:
            print(f"Error in absence checker: {e}")
            if conn:
                try:
                    conn.rollback()
                    cursor.close()
                    return_db_connection(conn)
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

def get_db_connection():
    return db_pool.get_connection()

def return_db_connection(conn):
    db_pool.return_connection(conn)



# ========== Google Sheets ==========
def get_students_score_records():
    SERVICE_ACCOUNT_FILE = 'spcheck-ingest-key.json'
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    
    try:
        print("🟡 [SHEETS] Attempting to authorize Google Sheets API...")
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
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
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
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
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
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


# Helper function for ReportLab (place this outside any route/function)
def getStatusLabel(rating):
    if rating >= 3:
        return 'Above Average'
    elif rating >= 2:
        return 'Average'
    elif rating > 0:
        return 'Below Average'
    else:
        return 'Not Rated'


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

def get_personnel_info(user_id):
    """Get personnel information with profile picture - OPTIMIZED with single query"""
    cache_key = f"personnel_info_{user_id}"
    cached = get_cached(cache_key, ttl=600)
    if cached:
        return cached
    
    try:
        conn = get_db_connection()
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
        return_db_connection(conn)
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus, personnel_id, profilepic = result
            
            full_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
            
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
        conn = get_db_connection()
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
        return_db_connection(conn)
        
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

notification_queues = {}  
notification_lock = threading.Lock()

def broadcast_notification(personnel_id, notification_data):
    """Broadcast notification to specific personnel AND to HR"""
    with notification_lock:
        if personnel_id and personnel_id > 0 and personnel_id in notification_queues:
            for q in notification_queues[personnel_id]:
                try:
                    q.put(notification_data)
                except Exception as e:
                    print(f"Error putting notification in queue: {e}")
        
        hr_key = 'hr_all_notifications'
        if hr_key in notification_queues:
            for q in notification_queues[hr_key]:
                try:
                    q.put(notification_data)
                    print(f"✓ Sent notification to HR: {notification_data.get('action', 'unknown')}")
                except Exception as e:
                    print(f"Error putting notification in HR queue: {e}")

def handle_rfid_notification(notification_data):
    """Handle RFID notifications from the reader"""
    personnel_id = notification_data.get('personnel_id')
    if personnel_id is not None:
        broadcast_notification(personnel_id, notification_data)

rfid_reader.add_notification_callback(handle_rfid_notification)

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
            'personnel_id': personnel_id
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

@app.route('/api/faculty/semesters')
@require_auth([20001, 20002, 20003])
def api_faculty_semesters():
    """API endpoint to get available semesters - CACHED"""
    cache_key = "all_semesters"
    cached = get_cached(cache_key, ttl=1800) 
    if cached:
        return cached
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                acadcalendar_id,
                semester,
                acadyear,
                semesterstart,
                semesterend,
                CASE WHEN CURRENT_DATE BETWEEN semesterstart AND semesterend THEN 1 ELSE 0 END as is_current
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
        return_db_connection(conn)
        
        semester_options = []
        current_semester_id = None
        
        for sem in semesters:
            acadcalendar_id, semester, acadyear, start_date, end_date, is_current = sem
            
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
            
            display_text = f"{semester_display}, {year_display}"
            
            if is_current and current_semester_id is None:
                current_semester_id = acadcalendar_id
            
            semester_options.append({
                'id': acadcalendar_id,
                'text': display_text,
                'is_current': bool(is_current),
                'start_date': start_date.isoformat(),
                'end_date': end_date.isoformat()
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
        conn = get_db_connection()
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
                    sch.classroom
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
        return_db_connection(conn)
        
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
            'display': f"{semester_info_json['semester']}, AY {semester_info_json['acadyear']}"
        }
        
        return {
            'success': True,
            'attendance_logs': attendance_logs,
            'class_attendance': class_attendance,
            'status_breakdown': status_counts,
            'kpis': kpis,
            'semester_info': semester_info
        }
        
    except Exception as e:
        print(f"Error fetching attendance data for semester {semester_id}: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

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
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT firstname, lastname FROM personnel WHERE personnel_id = %s", (personnel_id,))
        faculty_info = cursor.fetchone()
        if not faculty_info:
            cursor.close()
            return_db_connection(conn)
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
            return_db_connection(conn)
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
        
        conn.commit()
        
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
        return_db_connection(conn)
        
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
        
        conn = get_db_connection()
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
        return_db_connection(conn)
        
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
        conn = get_db_connection()
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
            )
            SELECT 
                (SELECT row_to_json(current_semester) FROM current_semester),
                (SELECT json_agg(row_to_json(schedule_data)) FROM schedule_data),
                (SELECT json_agg(row_to_json(attendance_data)) FROM attendance_data),
                (SELECT COALESCE(SUM(units), 0) FROM schedule_data)
        """, (user_id,))
        
        result = cursor.fetchone()
        cursor.close()
        return_db_connection(conn)
        
        if not result or result[0] is None:
            return {'success': False, 'error': 'No active academic calendar found'}
        
        semester_info_json, schedule_json, attendance_json, teaching_load = result
        
        semester_start = date.fromisoformat(semester_info_json['semesterstart'])
        semester_end = date.fromisoformat(semester_info_json['semesterend'])
        
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
            'class_schedule': weekly_schedule,
            'teaching_load': int(teaching_load) if teaching_load else 0,
            'semester_info': {
                'name': semester_info_json['semester'],
                'year': semester_info_json['acadyear'],
                'display': f"{semester_info_json['semester']}, AY {semester_info_json['acadyear']}"
            }
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
        conn = get_db_connection()
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
            return_db_connection(conn)
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
        return_db_connection(conn)
        
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
        conn = get_db_connection()
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
            LIMIT 100
        """)
        
        logs = cursor.fetchall()
        cursor.close()
        return_db_connection(conn)
        
        audit_logs = []
        for log in logs:
            (audit_id, personnel_id, action, details, created_at, 
             firstname, lastname, honorifics) = log
            
            if personnel_id and firstname and lastname:
                personnel_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
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
        conn = get_db_connection()
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
        return_db_connection(conn)
        
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
        
        conn = get_db_connection()
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
        
        cursor.close()
        return_db_connection(conn)
        
        return {'success': True, 'profile': profile_data}
        
    except Exception as e:
        print(f"Error fetching profile data: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/profile/stats')
@require_auth([20001, 20002, 20003, 20004])
def api_get_profile_stats():
    """OPTIMIZED: Get profile statistics with single query"""
    try:
        viewing_personnel_id = session.get('viewing_personnel_id')
        if viewing_personnel_id:
            personnel_id = viewing_personnel_id
        else:
            personnel_info = get_personnel_info(session['user_id'])
            personnel_id = personnel_info.get('personnel_id')
            
            if not personnel_id:
                return {'success': False, 'error': 'Personnel record not found'}
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                pe.hiredate,
                COALESCE(array_length(pr.certificates, 1), 0) as certificates_count,
                COALESCE(array_length(pr.publications, 1), 0) as publications_count,
                COALESCE(array_length(pr.awards, 1), 0) as awards_count
            FROM personnel pe
            LEFT JOIN profile pr ON pe.personnel_id = pr.personnel_id
            WHERE pe.personnel_id = %s
        """, (personnel_id,))
        
        result = cursor.fetchone()
        cursor.close()
        return_db_connection(conn)
        
        if not result:
            return {'success': False, 'error': 'Personnel record not found'}
        
        hire_date, certificates_count, publications_count, awards_count = result
        
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
            'awards_count': awards_count
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
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        if not personnel_id:
            cursor.close()
            return_db_connection(conn)
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
        return_db_connection(conn)
        
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
        conn = get_db_connection()
        cursor = conn.cursor()
        
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        if not personnel_id:
            cursor.close()
            return_db_connection(conn)
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
        return_db_connection(conn)
        
        return {'success': True, 'message': 'Documents updated successfully'}
        
    except Exception as e:
        print(f"Error updating documents: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

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
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT password FROM users WHERE user_id = %s", (user_id,))
        current_pass_result = cursor.fetchone()
        
        if not current_pass_result or current_pass_result[0] != current_password:
            cursor.close()
            return_db_connection(conn)
            return {'success': False, 'error': 'Current password is incorrect'}
        
        if current_password == new_password:
            cursor.close()
            return_db_connection(conn)
            return {'success': False, 'error': 'New password cannot be the same as current password'}
        
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        cursor.execute("""
            UPDATE users SET password = %s WHERE user_id = %s
        """, (new_password, user_id))
        
        conn.commit()
        cursor.close()
        return_db_connection(conn)
        
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
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        if not personnel_id:
            cursor.close()
            return_db_connection(conn)
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
            return_db_connection(conn)
            return {'success': False, 'error': 'No documents found'}
        
        doc_array = list(doc_result[0])
        filenames = list(doc_result[1]) if doc_result[1] else []
        
        if index < 0 or index >= len(doc_array):
            cursor.close()
            return_db_connection(conn)
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
        return_db_connection(conn)
        
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
        conn = get_db_connection()
        cursor = conn.cursor()
        
        philippines_tz = pytz.timezone('Asia/Manila')
        today = datetime.now(philippines_tz).date()
        
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
                sch.classsection
            FROM attendance a
            JOIN personnel p ON a.personnel_id = p.personnel_id
            JOIN schedule sch ON a.class_id = sch.class_id
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE p.role_id IN (20001, 20002)
            ORDER BY a.timein DESC, p.lastname, p.firstname
        """)
        
        attendance_records = cursor.fetchall()
        
        cursor.execute("SELECT COUNT(*) FROM personnel WHERE role_id IN (20001, 20002)")
        total_faculty = cursor.fetchone()[0]
        
        cursor.close()
        return_db_connection(conn)
        
        attendance_logs = []
        status_counts = {'present': 0, 'late': 0, 'absent': 0, 'excused': 0}
        today_counts = {'present': 0, 'late': 0, 'absent': 0, 'excused': 0}
        
        seen_records = set()
        
        for record in attendance_records:
            (firstname, lastname, honorifics, status, timein, timeout, 
             classroom, subject_code, subject_name, class_section) = record
            
            faculty_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
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
                    'status': status.capitalize()
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
        
        conn = get_db_connection()
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
                name_parts = name.split(' ')
                if len(name_parts) >= 2:
                    firstname = name_parts[0]
                    lastname = name_parts[1].replace(',', '')
                    
                    print(f"   Looking for: {firstname} {lastname}, date: {date}, class: {class_name}")
                    
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
                            
                            changes_made = []
                            updates_applied = False
                            
                            original_timein = current_timein
                            original_timeout = current_timeout
                            original_status = current_status
                            
                            if 'time_in' in update:
                                updates_applied = True
                                if time_in == '':  
                                    midnight_time = f"{date} 00:00:00"
                                    cursor.execute("""
                                        UPDATE attendance 
                                        SET timein = %s::timestamp AT TIME ZONE 'Asia/Manila'
                                        WHERE attendance_id = %s
                                    """, (midnight_time, attendance_id))
                                    changes_made.append("deleted time-in")
                                    print(f"   ✅ Deleted time-in (set to midnight)")
                                else:  
                                    time_in_24hr = convert_to_24hour(time_in)
                                    new_timein = f"{date} {time_in_24hr}"
                                    cursor.execute("""
                                        UPDATE attendance 
                                        SET timein = %s::timestamp AT TIME ZONE 'Asia/Manila'
                                        WHERE attendance_id = %s
                                    """, (new_timein, attendance_id))
                                    changes_made.append(f"set time-in to {time_in}")
                                    print(f"   ✅ Set time-in to {time_in_24hr}")
                            
                            if 'time_out' in update:
                                if time_out == '':  
                                    cursor.execute("""
                                        UPDATE attendance 
                                        SET timeout = NULL
                                        WHERE attendance_id = %s
                                    """, (attendance_id,))
                                    changes_made.append("deleted time-out")
                                    print(f"   ✅ Deleted time-out")
                                    updates_applied = True
                                else:  
                                    current_timein_check = current_timein
                                    if 'time_in' in update and time_in != '':
                                        time_in_24hr = convert_to_24hour(time_in)
                                        current_timein_check = datetime.strptime(f"{date} {time_in_24hr}", "%Y-%m-%d %H:%M:%S")
                                    
                                    if current_timein_check and current_timein_check.strftime('%H:%M:%S') != '00:00:00':
                                        time_out_24hr = convert_to_24hour(time_out)
                                        new_timeout = f"{date} {time_out_24hr}"
                                        cursor.execute("""
                                            UPDATE attendance 
                                            SET timeout = %s::timestamp AT TIME ZONE 'Asia/Manila'
                                            WHERE attendance_id = %s
                                        """, (new_timeout, attendance_id))
                                        changes_made.append(f"set time-out to {time_out}")
                                        print(f"   ✅ Set time-out to {time_out_24hr}")
                                        updates_applied = True
                                    else:
                                        print(f"   ⚠️ Cannot add timeout - no valid timein exists")
                                        updates_applied = False
                            
                            if updates_applied:
                                cursor.execute("""
                                    SELECT timein, timeout FROM attendance WHERE attendance_id = %s
                                """, (attendance_id,))
                                updated_times = cursor.fetchone()
                                
                                if updated_times:
                                    updated_timein, updated_timeout = updated_times
                                    new_status = None
                                    
                                    if class_start and class_end and validation_day == day_of_week:
                                        print(f"   🔍 Validating against class schedule: {class_start}-{class_end}")
                                        
                                        if updated_timein and updated_timein.strftime('%H:%M:%S') != '00:00:00':
                                            if isinstance(class_start, str):
                                                class_start_time = datetime.strptime(class_start[:8], '%H:%M:%S').time()
                                            else:
                                                class_start_time = class_start
                                            
                                            if isinstance(class_end, str):
                                                class_end_time = datetime.strptime(class_end[:8], '%H:%M:%S').time()
                                            else:
                                                class_end_time = class_end
                                            
                                            class_start_dt = datetime.combine(date_obj, class_start_time)
                                            class_end_dt = datetime.combine(date_obj, class_end_time)
                                            
                                            timein_dt = updated_timein.astimezone(pytz.timezone('Asia/Manila')).replace(tzinfo=None)
                                            
                                            # EXACT RFID VALIDATION RULES
                                            timein_window_start = (class_start_dt - timedelta(minutes=15)).time()
                                            timein_window_end = (class_end_dt - timedelta(minutes=15)).time()
                                            present_threshold_dt = class_start_dt + timedelta(minutes=15)
                                            timeout_window_start = (class_end_dt - timedelta(minutes=15)).time()
                                            timeout_window_end = (class_end_dt + timedelta(minutes=15)).time()
                                            
                                            print(f"   📊 Time Windows:")
                                            print(f"     Time-in window: {timein_window_start} to {timein_window_end}")
                                            print(f"     Present threshold: {present_threshold_dt.time()}")
                                            print(f"     Time-out window: {timeout_window_start} to {timeout_window_end}")
                                            print(f"     Actual time-in: {timein_dt.time()}")
                                            
                                            # RULE 1: Check if time-in is within valid time-in window
                                            if timein_window_start <= timein_dt.time() <= timein_window_end:
                                                # RULE 5 & 6: Determine Present vs Late
                                                if timein_dt <= present_threshold_dt:
                                                    new_status = "Present"
                                                    print(f"   ✅ Time-in within window: RECORDED AS PRESENT")
                                                else:
                                                    new_status = "Late" 
                                                    print(f"   ✅ Time-in within window: RECORDED AS LATE")
                                            else:
                                                # Time-in outside window - check if it's in timeout window (RULE 8)
                                                if timeout_window_start <= timein_dt.time() <= timeout_window_end:
                                                    new_status = "Late"
                                                    print(f"   ✅ Time-in in timeout window: RECORDED AS LATE")
                                                else:
                                                    # Time-in completely outside all windows
                                                    new_status = "Absent"
                                                    print(f"   ❌ Time-in outside all windows: RECORDED AS ABSENT")
                                                    
                                        else:
                                            # No time-in = Absent (RULE 9 & 10)
                                            new_status = "Absent"
                                            print(f"   ❌ No time-in: RECORDED AS ABSENT")
                                    
                                    else:
                                        print(f"   ⚠️ No schedule validation possible, using basic logic")
                                        if not updated_timein or updated_timein.strftime('%H:%M:%S') == '00:00:00':
                                            new_status = "Absent"
                                        elif updated_timein and not updated_timeout:
                                            new_status = "Present"
                                        elif updated_timein and updated_timeout:
                                            new_status = "Present"
                                    
                                    if new_status:
                                        cursor.execute("""
                                            UPDATE attendance 
                                            SET attendancestatus = %s
                                            WHERE attendance_id = %s
                                        """, (new_status, attendance_id))
                                        print(f"   ✅ Updated status: {current_status} → {new_status}")
                                    
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

                                    before_timein_str = "None"
                                    if original_timein and original_timein.strftime('%H:%M:%S') != '00:00:00':
                                        before_timein_str = original_timein.strftime('%H:%M:%S')

                                    before_timeout_str = "None"
                                    if original_timeout and original_timeout.strftime('%H:%M:%S') != '00:00:00':
                                        before_timeout_str = original_timeout.strftime('%H:%M:%S')

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
                                    
                                print(f"   ✅ Successfully updated attendance_id: {attendance_id}")
                    else:
                        print(f"   ❌ No matching attendance record found!")
        
        conn.commit()
        cursor.close()
        return_db_connection(conn)
        
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
            return_db_connection(conn)
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
        
        conn = get_db_connection()
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
        return_db_connection(conn)
        
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

@app.route('/api/hr/rfid-logs')
@require_auth([20003])
def api_hr_rfid_logs():
    """Get all RFID logs for HR view with milliseconds"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                rl.log_id,
                rl.taptime,
                rl.personnel_id,
                rl.remarks,
                p.firstname,
                p.lastname,
                p.honorifics
            FROM rfidlogs rl
            LEFT JOIN personnel p ON rl.personnel_id = p.personnel_id
            ORDER BY rl.taptime DESC
        """)
        
        logs = cursor.fetchall()
        cursor.close()
        return_db_connection(conn)
        
        rfid_logs = []
        for log in logs:
            (log_id, taptime, personnel_id, remarks, firstname, lastname, honorifics) = log
            
            if personnel_id and firstname and lastname:
                personnel_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
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
                'remarks': remarks or ""
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
    cache_key = "hr_faculty_list"
    cached = get_cached(cache_key, ttl=300)
    if cached:
        return cached
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            WITH current_calendar AS (
                SELECT acadcalendar_id 
                FROM acadcalendar 
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC
                LIMIT 1
            )
            SELECT 
                p.personnel_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                p.role_id,
                c.collegename,
                COALESCE(SUM(sub.units), 0) as total_units
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN schedule sch ON p.personnel_id = sch.personnel_id 
                AND sch.acadcalendar_id = (SELECT acadcalendar_id FROM current_calendar)
            LEFT JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE p.role_id IN (20001, 20002)
            GROUP BY p.personnel_id, p.firstname, p.lastname, p.honorifics, p.role_id, c.collegename
            ORDER BY p.lastname, p.firstname
        """)
        
        faculty_records = cursor.fetchall()
        cursor.close()
        return_db_connection(conn)
        
        faculty_list = []
        for record in faculty_records:
            personnel_id, firstname, lastname, honorifics, role_id, collegename, total_units = record
            
            faculty_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
            
            faculty_list.append({
                'personnel_id': personnel_id,
                'name': faculty_name,
                'college': collegename or 'N/A',
                'teaching_load': int(total_units),
                'role_id': role_id
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
        conn = get_db_connection()
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
        return_db_connection(conn)
        
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
    Fetches comprehensive evaluation data for the currently logged-in faculty member.
    Includes KPIs, breakdown, comparison, and raw qualitative feedback.
    """
    try:
        user_id = session.get('user_id')
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        if not personnel_id:
            return jsonify({'success': False, 'error': 'Personnel record not found'}), 404
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Determine the current academic calendar ID (or the latest completed one)
        cursor.execute("""
            SELECT acadcalendar_id
            FROM acadcalendar 
            WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
            ORDER BY semesterstart DESC LIMIT 1
        """)
        current_term_result = cursor.fetchone()
        current_term_id = current_term_result[0] if current_term_result else '80001' # Fallback
        
        # 2. Fetch all personal scores and comparison data in one block
        cursor.execute("""
            WITH faculty_scores AS (
                -- Personal scores (Simple average per type for KPI & Weighted total)
                SELECT
                    fe.evaluator_type,
                    fe.score AS single_score,
                    fe.total_responses AS total_responses,
                    fe.qualitative_feedback -- NEW: Include feedback
                FROM faculty_evaluations fe
                WHERE fe.personnel_id = %s AND fe.acadcalendar_id = %s
            ),
            
            personal_aggregates AS (
                -- Calculate all required KPIs/Breakdown from the scores above
                SELECT
                    COALESCE(
                        SUM(CASE WHEN evaluator_type = 'student' THEN single_score * 0.55 ELSE 0 END) +
                        SUM(CASE WHEN evaluator_type = 'supervisor' THEN single_score * 0.35 ELSE 0 END) +
                        SUM(CASE WHEN evaluator_type = 'peer' THEN single_score * 0.10 ELSE 0 END),
                    0) AS overall_average,
                    
                    COALESCE(SUM(CASE WHEN evaluator_type = 'student' THEN single_score END) / COUNT(CASE WHEN evaluator_type = 'student' THEN 1 END), 0) AS student_score,
                    COALESCE(SUM(CASE WHEN evaluator_type = 'peer' THEN single_score END) / COUNT(CASE WHEN evaluator_type = 'peer' THEN 1 END), 0) AS peer_score,
                    COALESCE(SUM(CASE WHEN evaluator_type = 'supervisor' THEN single_score END) / COUNT(CASE WHEN evaluator_type = 'supervisor' THEN 1 END), 0) AS supervisor_score
                FROM faculty_scores
            ),
            
            comparison_base AS (
                -- All faculty weighted scores (for comparison)
                SELECT 
                    p.personnel_id,
                    p.college_id,
                    c.collegename,
                    COALESCE(
                        SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) +
                        SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) +
                        SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END),
                    0) AS overall_score
                FROM personnel p
                LEFT JOIN college c ON p.college_id = c.college_id
                LEFT JOIN faculty_evaluations fe ON fe.personnel_id = p.personnel_id AND fe.acadcalendar_id = %s
                WHERE p.role_id IN (20001, 20002) AND fe.score IS NOT NULL
                GROUP BY p.personnel_id, p.college_id, c.collegename
            )
            SELECT 
                -- 1. Individual Scores (KPIs)
                (SELECT overall_average FROM personal_aggregates),
                (SELECT student_score FROM personal_aggregates),
                (SELECT peer_score FROM personal_aggregates),
                (SELECT supervisor_score FROM personal_aggregates),
                
                -- 2. Comparison Data
                (SELECT collegename FROM personnel p JOIN college c ON p.college_id = c.college_id WHERE p.personnel_id = %s) AS faculty_college_name,
                (SELECT AVG(overall_score) FROM comparison_base WHERE overall_score > 0) AS college_wide_avg,
                (SELECT AVG(cb.overall_score) 
                 FROM comparison_base cb
                 WHERE cb.college_id = (SELECT college_id FROM personnel WHERE personnel_id = %s) AND cb.overall_score > 0
                ) AS department_avg,
                
                -- 3. Qualitative Feedback (Aggregate all non-null feedback)
                json_agg(json_build_object(
                    'type', evaluator_type, 
                    'feedback', qualitative_feedback
                )) FILTER (WHERE qualitative_feedback IS NOT NULL) AS all_feedback_json
            
            FROM faculty_scores
            
        """, (personnel_id, current_term_id, current_term_id, personnel_id, personnel_id))
        
        result = cursor.fetchone()
        cursor.close()
        return_db_connection(conn)

        if not result:
            return jsonify({'success': False, 'error': 'No personnel or active term found.'}), 404
        
        (overall_avg, student_score, peer_score, supervisor_score, 
         faculty_college_name, college_wide_avg, department_avg, all_feedback_json) = result

        # Ensure scores are floats for JSON serialization
        overall_avg = float(overall_avg) if overall_avg is not None else 0
        student_score = float(student_score) if student_score is not None else 0
        peer_score = float(peer_score) if peer_score is not None else 0
        supervisor_score = float(supervisor_score) if supervisor_score is not None else 0
        
        # Prepare comparison scores
        your_avg = overall_avg
        dept_avg = float(department_avg) if department_avg is not None else your_avg
        college_avg = float(college_wide_avg) if college_wide_avg is not None else your_avg

        # --- Process Feedback (Server-side cleanup) ---
        clean_feedback = []
        if all_feedback_json:
            for item in all_feedback_json:
                eval_type = item['type']
                feedback_str = item['feedback']
                
                if feedback_str and feedback_str.strip():
                    # Split by newline, strip, and filter out '---' delimiter
                    comments = [c.strip() for c in feedback_str.split('\n') if c.strip() and c.strip() != '---']
                    
                    for comment in comments:
                        clean_feedback.append({
                            'evaluator': eval_type.capitalize(),
                            'comment': comment
                        })
        
        # Calculate Breakdown data (for doughnut chart percentages)
        breakdown_labels = ['Students (55%)', 'Peers (10%)', 'Supervisors (35%)']
        breakdown_data = [55, 10, 35] # Fixed weights, sum to 100
        
        return jsonify({
            'success': True,
            'current_term_id': current_term_id,
            'kpis': {
                'overall_average': overall_avg,
                'student_score': student_score,
                'peer_score': peer_score,
                'supervisor_score': supervisor_score,
            },
            'breakdown_chart': {
                'labels': breakdown_labels,
                'data': breakdown_data 
            },
            'comparison': {
                'your_avg': your_avg,
                'dept_avg': dept_avg,
                'college_avg': college_avg,
                'college_name': faculty_college_name
            },
            'recent_feedback': clean_feedback # NEW: Cleaned feedback list
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
        conn = get_db_connection()
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
        return_db_connection(conn)
        
        employees_list = []
        for emp in employees:
            (personnel_id, firstname, lastname, honorifics, employee_no, 
             phone, collegename, rolename, position, employmentstatus, email) = emp
            
            full_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
            
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
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT user_id FROM users WHERE email = %s", (email,))
        if cursor.fetchone():
            cursor.close()
            return_db_connection(conn)
            return {'success': False, 'error': 'Email already exists'}
        
        cursor.execute("SELECT personnel_id FROM personnel WHERE employee_no = %s", (employee_no,))
        if cursor.fetchone():
            cursor.close()
            return_db_connection(conn)
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
        return_db_connection(conn)
        
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
        
        employee_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
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
            return_db_connection(conn)
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/subjects-list')
@require_auth([20003])
def api_hr_subjects_list():
    """Get all subjects for schedule dropdown"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT subject_id, subjectcode, subjectname, units 
            FROM subjects 
            ORDER BY subjectcode
        """)
        
        subjects = cursor.fetchall()
        cursor.close()
        return_db_connection(conn)
        
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
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT college_id FROM personnel WHERE personnel_id = %s", (personnel_id,))
        faculty_college = cursor.fetchone()
        
        if not faculty_college or not faculty_college[0]:
            cursor.close()
            return_db_connection(conn)
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
        return_db_connection(conn)
        
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
        
        conn = get_db_connection()
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
        return_db_connection(conn)
        
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
        
        required_fields = ['semester_id', 'personnel_id', 'subject_id', 'units', 
                          'classday_1', 'starttime_1', 'endtime_1', 'classroom', 'classsection']
        
        for field in required_fields:
            if not data.get(field):
                return {'success': False, 'error': f'All required fields must be filled. Missing: {field}'}
        
        conn = get_db_connection()
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
            return_db_connection(conn)
            return {'success': False, 'error': 'Invalid semester selected'}

        cursor.execute("""
            INSERT INTO schedule (
                class_id, personnel_id, subject_id,
                classday_1, starttime_1, endtime_1,
                classday_2, starttime_2, endtime_2,
                classroom, acadcalendar_id, classsection
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            new_class_id, personnel_id, subject_id,
            classday_1, starttime_1, endtime_1,
            classday_2, starttime_2, endtime_2,
            classroom, semester_id, classsection
        ))
        
        conn.commit()
        cursor.close()
        return_db_connection(conn)
        
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
            return_db_connection(conn)
        return {'success': False, 'error': str(e)}

@app.route('/hr_employee_profile/<int:personnel_id>')
@require_auth([20003])
def hr_employee_profile(personnel_id):
    """HR view of employee profile"""
    try:

        hr_info = get_personnel_info(session['user_id'])
        
        conn = get_db_connection()
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
        return_db_connection(conn)
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus = result
            
            full_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
            
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
            
            return render_template('hrmd/hr-profile.html', **employee_info)
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
        
        conn = get_db_connection()
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
        return_db_connection(conn)
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus = result
            
            full_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
            
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
        
        conn = get_db_connection()
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
        return_db_connection(conn)
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus = result
            
            full_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
            
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
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT college_id, collegename 
            FROM college 
            ORDER BY collegename
        """)
        
        colleges = cursor.fetchall()
        cursor.close()
        return_db_connection(conn)
        
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
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT user_id FROM users WHERE email = %s", (email,))
            user_exists = cursor.fetchone()
            
            if not user_exists:
                cursor.close()
                return_db_connection(conn)
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
                return_db_connection(conn)
                
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
                return_db_connection(conn)
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

# EDITED BY CARDS
@app.route('/faculty_promotion')
@require_auth([20001, 20002])
def faculty_promotion():
    user_id = session.get('user_id')
    if not user_id:
        return "Unauthorized", 401
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # ✅ Get faculty info for base template (name, college, profile image)
    cursor.execute("""
        SELECT 
            p.personnel_id, 
            p.hiredate, 
            pr.position,
            p.firstname,
            p.lastname,
            p.honorifics,
            c.collegename,
            pr.profilepic
        FROM personnel p
        LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
        LEFT JOIN college c ON p.college_id = c.college_id
        WHERE p.user_id = %s
    """, (user_id,))
    
    result = cursor.fetchone()
    
    if not result:
        cursor.close()
        return_db_connection(conn)
        return "Faculty record not found", 400
    
    faculty_id, hire_date, current_rank, firstname, lastname, honorifics, college, profilepic = result
    
    # ✅ Build faculty name for base template
    if honorifics:
        faculty_name = f"{firstname} {lastname}, {honorifics}"
    else:
        faculty_name = f"{firstname} {lastname}"
    
    # ✅ Convert profile pic to base64
    profile_image_base64 = ''
    if profilepic:
        import base64
        profile_image_base64 = f"data:image/jpeg;base64,{base64.b64encode(bytes(profilepic)).decode('utf-8')}"
    
    # Define rank hierarchy
    rank_hierarchy = [
        "Instructor",
        "Assistant Professor",
        "Associate Professor",
        "Professor"
    ]
    
    # Get available ranks (only higher ranks)
    available_ranks = []
    if current_rank in rank_hierarchy:
        current_index = rank_hierarchy.index(current_rank)
        available_ranks = rank_hierarchy[current_index + 1:]
    else:
        available_ranks = rank_hierarchy
    
    # === REGULARIZATION CALCULATION FROM HIRE DATE ===
    from datetime import date
    today = date.today()
    years_employed = 0
    months_employed = 0
    regularization_percentage = 0
    regularization_status = "No hire date"
    regularization_message = "Contact HRMD to update your hire date."
    tenure_type = "Unknown"
    can_apply_for_promotion = False
    
    if hire_date:
        # Calculate years and months
        years_employed = today.year - hire_date.year
        months_employed = today.month - hire_date.month
        
        if months_employed < 0:
            years_employed -= 1
            months_employed += 12
        
        # Total months employed
        total_months_employed = (years_employed * 12) + months_employed
        
        # Determine tenure type and calculate percentage based ONLY on hire date
        if years_employed < 3:
            # Probationary (0-3 years)
            tenure_type = "Probationary"
            probation_months = 36
            regularization_percentage = min(round((total_months_employed / probation_months) * 100), 100)
            regularization_status = f"Probationary (Year {years_employed + 1} of 3)"
            months_remaining = probation_months - total_months_employed
            regularization_message = f"ℹ️ {months_remaining} months until eligible for Regular status."
            can_apply_for_promotion = False
            
        elif 3 <= years_employed < 7:
            # Regular (3-7 years)
            tenure_type = "Regular"
            years_past_probation = years_employed - 3
            months_past_probation = (years_past_probation * 12) + months_employed
            regular_period_months = 48
            regularization_percentage = min(round((months_past_probation / regular_period_months) * 100), 100)
            regularization_status = f"Regular Employee (Year {years_past_probation + 1} of 4)"
            years_to_tenure = 7 - years_employed
            regularization_message = f"✅ You are a Regular employee. {years_to_tenure} year{'s' if years_to_tenure != 1 else ''} until eligible for Tenured status."
            can_apply_for_promotion = True
            
        else:
            # Tenured (7+ years)
            tenure_type = "Tenured"
            regularization_percentage = 100
            regularization_status = "Tenured Employee"
            regularization_message = "✅ You have achieved Tenured status."
            can_apply_for_promotion = True
    
    # === CHECK FOR ACTIVE REGULARIZATION APPLICATION ===
    cursor.execute("""
        SELECT 
            years_of_service,
            current_status,
            hrmd_endorsement_date,
            vpa_recommendation_date,
            pres_approval_date,
            final_decision,
            date_initiated
        FROM regularization_application
        WHERE faculty_id = %s AND final_decision IS NULL
        ORDER BY date_initiated DESC
        LIMIT 1
    """, (faculty_id,))
    
    reg_row = cursor.fetchone()
    regularization_status_data = None
    
    if reg_row:
        years_at_initiation = float(reg_row[0]) if reg_row[0] else years_employed
        
        # Calculate requested tenure based on years at initiation
        if years_at_initiation >= 7:
            requested_tenure = "Tenured"
        elif years_at_initiation >= 3:
            requested_tenure = "Regular"
        else:
            requested_tenure = "Regular"
        
        regularization_status_data = {
            'requested_tenure': requested_tenure,
            'current_status': reg_row[1],
            'hrmd_date': reg_row[2],
            'vpa_date': reg_row[3],
            'pres_date': reg_row[4],
            'final_decision': reg_row[5],
            'date_initiated': reg_row[6]
        }
    
    # === CURRENT PROMOTION APPLICATION (ONLY ACTIVE) ===
    cursor.execute("""
        SELECT application_id, current_status, date_submitted, 
               hrmd_approval_date, vpa_approval_date, pres_approval_date, 
               final_decision, resume, cover_letter, 
               resume_filename, cover_letter_filename, requested_rank
        FROM promotion_application 
        WHERE faculty_id = %s 
          AND final_decision IS NULL
        ORDER BY date_submitted DESC 
        LIMIT 1
    """, (faculty_id,))
    
    row = cursor.fetchone()
    
    # === APPLICATION HISTORY (ALL APPLICATIONS) ===
    cursor.execute("""
        SELECT date_submitted, current_status, final_decision,
               hrmd_approval_date, vpa_approval_date, pres_approval_date,
               requested_rank, rejection_reason
        FROM promotion_application 
        WHERE faculty_id = %s 
        ORDER BY date_submitted DESC
    """, (faculty_id,))
    
    history_rows = cursor.fetchall()
    
    cursor.close()
    return_db_connection(conn)
    
    # Build history list
    application_history = []
    for h in history_rows:
        date_sub = h[0]
        status = h[1]
        decision = h[2]
        hrmd_date = h[3]
        vpa_date = h[4]
        pres_date = h[5]
        requested_pos = h[6]
        rejection_reason = h[7]
        
        if decision == 1:
            decision_text = 'Approved'
            remarks = 'Application approved'
        elif decision == 0:
            decision_text = 'Rejected'
            remarks = 'Application rejected'
        else:
            decision_text = 'Pending'
            remarks = 'Pending review'
        
        application_history.append({
            'date_submitted': date_sub,
            'current_status': status,
            'final_decision': decision_text,
            'remarks': remarks,
            'hrmd_approval_date': hrmd_date,
            'vpa_approval_date': vpa_date,
            'pres_approval_date': pres_date,
            'requested_position': requested_pos,
            'rejection_reason': rejection_reason
        })
    
    # Build template data
    template_data = {
        # ✅ Base template variables
        'faculty_name': faculty_name,
        'college': college or 'College of Computer Studies',
        'profile_image_base64': profile_image_base64,
        
        # Promotion-specific data
        'regularization_percentage': regularization_percentage,
        'regularization_status': regularization_status,
        'regularization_message': regularization_message,
        'tenure_type': tenure_type,
        'years_employed': years_employed,
        'months_employed': months_employed,
        'hire_date': hire_date,
        'regularization_status_data': regularization_status_data,
        'current_rank': current_rank,
        'available_ranks': available_ranks,
        'application_history': application_history,
        'can_apply_for_promotion': can_apply_for_promotion,
        'application_id': None,
        'current_status': None,
        'date_submitted': None,
        'hrmd_approval_date': None,
        'vpa_approval_date': None,
        'pres_approval_date': None,
        'final_decision': None,
        'resume_cv': None,
        'cover_letter': None,
        'resume_filename': None,
        'cover_letter_filename': None,
        'requested_rank': None,
        'upload_locked': False
    }
    
    if row:
        template_data.update({
            'application_id': row[0],
            'current_status': row[1],
            'date_submitted': row[2],
            'hrmd_approval_date': row[3],
            'vpa_approval_date': row[4],
            'pres_approval_date': row[5],
            'final_decision': row[6],
            'resume_cv': row[7],
            'cover_letter': row[8],
            'resume_filename': row[9],
            'cover_letter_filename': row[10],
            'requested_rank': row[11],
            'upload_locked': row[1] in ['hrmd', 'vpa', 'pres']
        })
    
    return render_template('faculty&dean/faculty-promotion.html', **template_data)




@app.route('/faculty_profile')
@require_auth([20001, 20002])
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
    current_term_id = 80001
    personnel_info = get_personnel_info(session['user_id'])
    return render_template('hrmd/hr-evaluations.html', acadcalendar_id=current_term_id, **personnel_info)

@app.route('/api/hr/evaluation-dashboard-data')
@require_auth([20003])
def api_hr_evaluation_dashboard_data():
    """
    Fetches aggregated evaluation data for the HR dashboard KPIs and charts.
    FIXED: Removed the invalid GROUP BY clause and simplified the final SELECT logic.
    """
    try:
        conn = get_db_connection()
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
        return_db_connection(conn)
        
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
    
    conn = get_db_connection()
    cursor = conn.cursor()

    # Define the term ID for use in all queries
    current_term_id = term if term else '80001'

    # --- KPI 1, 2, 3, & 4 Calculation (Combined Query) ---
    cursor.execute("""
        WITH faculty_data AS (
            SELECT 
                p.personnel_id,
                p.college_id,
                -- Weighted Overall Score
                COALESCE(
                    SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) +
                    SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) +
                    SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END),
                0) AS overall_score,
                -- Student Response Count
                COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.total_responses ELSE 0 END), 0) AS student_responses_count
            FROM personnel p
            LEFT JOIN faculty_evaluations fe ON fe.personnel_id = p.personnel_id AND fe.acadcalendar_id = %s
            WHERE p.role_id IN (20001, 20002)
            GROUP BY p.personnel_id, p.college_id
        ),
        department_avg AS (
            SELECT 
                fd.college_id,
                AVG(fd.overall_score) AS dept_avg_score
            FROM faculty_data fd
            WHERE fd.overall_score > 0 -- Only consider departments with non-zero evaluation scores
            GROUP BY fd.college_id
            ORDER BY dept_avg_score DESC
            LIMIT 1
        )
        SELECT 
            -- General KPIs
            (SELECT COUNT(fd.personnel_id) FROM faculty_data fd) AS total_faculty,
            (SELECT COALESCE(AVG(fd.overall_score), 0) FROM faculty_data fd) AS average_rating,
            (SELECT SUM(CASE WHEN fd.student_responses_count >= 10 THEN 1 ELSE 0 END) FROM faculty_data fd) AS met_response_rate_count,
            (SELECT COUNT(CASE WHEN fd.student_responses_count > 0 THEN 1 ELSE NULL END) FROM faculty_data fd) AS faculty_with_data,
            
            -- Best Department KPI
            c.collegename AS best_department_name
        FROM department_avg da
        JOIN college c ON da.college_id = c.college_id
    """, (current_term_id,))
    
    kpi_results = cursor.fetchone()
    
    # Map results (handle case where no results are returned, typically if the table is empty)
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
            CONCAT(p.firstname, ' ', p.lastname) as name,
            c.collegename,
            pr.position,
            
            -- METRIC 1 (Response Rate): Student Response Count ONLY 
            COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.total_responses ELSE 0 END), 0) AS student_responses_count,
            
            -- METRIC 2 (Status): Weighted Overall Score 
            COALESCE(
                SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.score * 0.55 ELSE 0 END) +
                SUM(CASE WHEN fe.evaluator_type = 'supervisor' THEN fe.score * 0.35 ELSE 0 END) +
                SUM(CASE WHEN fe.evaluator_type = 'peer' THEN fe.score * 0.10 ELSE 0 END),
            0) AS overall_score,
            
            -- Store position for separate filtering/list
            pr.position AS faculty_position
            
        FROM personnel p
        LEFT JOIN faculty_evaluations fe ON fe.personnel_id = p.personnel_id AND fe.acadcalendar_id = %s
        LEFT JOIN college c ON p.college_id = c.college_id
        LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
        WHERE 1=1 AND p.role_id IN (20001, 20002)
    """
    params = [current_term_id]

    # Dynamic filtering 
    if dept:
        query += " AND c.collegename = %s"
        params.append(dept)
    if search:
        query += " AND (LOWER(p.firstname) LIKE %s OR LOWER(p.lastname) LIKE %s)"
        like = f"%{search.lower()}%"
        params.extend([like, like])
        
    # NEW: Position Filter
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
            
    # NEW: Response Rate Filter
    if response_rate_filter == 'met':
        query += " AND COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.total_responses ELSE 0 END), 0) >= 10"
    elif response_rate_filter == 'not-met':
        query += " AND COALESCE(SUM(CASE WHEN fe.evaluator_type = 'student' THEN fe.total_responses ELSE 0 END), 0) < 10"
            
    query += " ORDER BY overall_score DESC"
    
    cursor.execute(query, tuple(params))
    evaluations = cursor.fetchall()
    
    # Get all unique positions for the frontend filter dropdown
    cursor.execute("""
        SELECT DISTINCT pr.position
        FROM personnel p
        JOIN profile pr ON p.personnel_id = pr.personnel_id
        WHERE p.role_id IN (20001, 20002) AND pr.position IS NOT NULL
        ORDER BY pr.position
    """)
    unique_positions = [row[0] for row in cursor.fetchall() if row[0] and row[0].strip() != '']
    
    cursor.close()
    return_db_connection(conn)

    # Shape results to JSON
    evals = [{
        "personnelid": row[0],
        "name": row[1],
        "department": row[2],
        "position": row[3],
        "studentresponses": row[4],
        "avgscore": row[5]
    } for row in evaluations]

    # Combine table data and KPIs into the final JSON response
    return jsonify(
        success=True, 
        evaluations=evals, 
        kpis=kpis,
        unique_positions=unique_positions
    )


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
        conn = get_db_connection()
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
            return_db_connection(conn)
            return jsonify({'success': False, 'error': 'Faculty or Semester not found'}), 404
        
        firstname, lastname, honorifics, collegename, semester_name, acadyear = info_row
        faculty_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
        semester_display = f"{semester_name}, AY {acadyear}"

        # 2. Fetch all evaluation scores AND qualitative feedback
        cursor.execute("""
            SELECT 
                evaluator_type, 
                score, 
                total_responses,
                qualitative_feedback  -- NEW: Select feedback column
            FROM faculty_evaluations 
            WHERE personnel_id = %s AND acadcalendar_id = %s
        """, (personnel_id, term_id))
        
        evaluation_rows = cursor.fetchall()
        
        # 3. Aggregate data, calculate overall score, and collect feedback
        total_score = 0
        rating_breakdown = []
        qualitative_feedback = [] # Initialize empty list for actual feedback
        
        # Fixed weights based on business logic: Student(55%), Supervisor(35%), Peer(10%)
        weights = {'student': 0.55, 'supervisor': 0.35, 'peer': 0.10}

        for eval_type, score, total_responses, feedback in evaluation_rows:
            weight = weights.get(eval_type, 0)
            
            # Fix: Convert score (Decimal) to float before multiplication
            score_float = float(score) if score is not None else 0.0
            weighted_score = score_float * weight
            total_score += weighted_score
            
            rating_breakdown.append({
                'type': eval_type.capitalize(),
                'score': score_float,
                'weight': weight,
                'total_responses': total_responses
            })
            
            # --- NEW: Process Feedback ---
            if feedback and feedback.strip():
                # Split multiple comments if stored with a delimiter (e.g., newline)
                # Assuming feedback might contain multiple comments separated by newlines
                comments = [c.strip() for c in feedback.split('\n') if c.strip()]
                qualitative_feedback.extend(comments)
            # --- END NEW: Process Feedback ---


        cursor.close()
        return_db_connection(conn)

        return jsonify({
            'success': True,
            'report': {
                'faculty_name': faculty_name,
                'college': collegename,
                'semester_display': semester_display,
                'overall_rating': total_score,
                'rating_breakdown': rating_breakdown,
                'qualitative_feedback': qualitative_feedback # <-- Use actual data
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
    # We need to call the API function directly to get its data
    report_response = api_hr_faculty_evaluation_report(personnel_id)
    if report_response.status_code != 200:
        return report_response # Return JSON error from data fetch
    
    # Safely load JSON response data
    from flask import json
    report_data = json.loads(report_response.data.decode('utf-8'))['report']

    # 2. SETUP DOCUMENT
    buffer = BytesIO()
    
    # Use SimpleDocTemplate for Platypus Flowables (handles pagination automatically)
    doc = SimpleDocTemplate(
        buffer, 
        pagesize=(8.5 * inch, 11 * inch), # Letter size
        topMargin=0.75 * inch, 
        leftMargin=0.75 * inch, 
        rightMargin=0.75 * inch, 
        bottomMargin=0.5 * inch
    )
    
    styles = getSampleStyleSheet()
    story = []

    # 3. ADD HEADER & METADATA
    
    # Title Style
    title_style = ParagraphStyle(
        'Title', 
        parent=styles['h1'], 
        fontSize=16, 
        textColor=colors.HexColor('#7b1113'),
        spaceAfter=12
    )

    faculty_name = report_data.get('faculty_name', 'Faculty Report')
    semester = report_data.get('semester_display', 'N/A')
    overall_rating = report_data.get('overall_rating', 0.0)

    story.append(Paragraph("Saint Peter's College - Faculty Evaluation Report", title_style))
    story.append(Paragraph(f"<b>Faculty:</b> {faculty_name}", styles['Normal']))
    story.append(Paragraph(f"<b>Department:</b> {report_data.get('college', 'N/A')}", styles['Normal']))
    story.append(Paragraph(f"<b>Semester:</b> {semester}", styles['Normal']))
    story.append(Spacer(1, 0.2 * inch))

    # 4. OVERALL RATING TABLE
    
    # Data for the Overall Summary
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
    
    # Header for breakdown
    breakdown_header = [
        Paragraph("<b>Evaluator</b>", styles['Normal']), 
        Paragraph("<b>Weight</b>", styles['Normal']), 
        Paragraph("<b>Score</b>", styles['Normal']), 
        Paragraph("<b>Responses</b>", styles['Normal'])
    ]
    breakdown_data = [breakdown_header]

    for item in report_data.get('rating_breakdown', []):
        weight_percent = f"{item.get('weight', 0) * 100:.0f}%"
        breakdown_data.append([
            item.get('type').capitalize(),
            weight_percent,
            f"{item.get('score', 0.0):.2f}",
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

    # 8. SEND RESPONSE (Cleaned up to prevent double headers)
    buffer.seek(0)
    
    # 8a. Create the response object.
    response = make_response(buffer.getvalue())
    
    # 8b. Set the Content-Type header once.
    response.headers['Content-Type'] = 'application/pdf'
    
    # 8c. Set the Content-Disposition header once, ensuring it is the only one.
    # We use .set() or direct assignment to prevent duplicates.
    filename = f'Evaluation_Report_{faculty_name.replace(" ", "_")}_T{term_id}.pdf'
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    return response

# Helper function to determine the status label (place this outside the route, near getStatusInfo in JS)
def getStatusLabel(rating):
    if rating >= 3:
        return 'Above Average'
    elif rating >= 2:
        return 'Average'
    elif rating > 0:
        return 'Below Average'
    else:
        return 'Not Rated'

# --- NEW FEATURE: New Evaluation Cycle API STUB ---
@app.route('/api/hr/new-evaluation-cycle', methods=['POST'])
@require_auth([20003])
def api_hr_new_evaluation_cycle():
    """HR initiates a new evaluation cycle for the current academic calendar."""
    try:
        data = request.get_json()
        current_term_id = data.get('current_term_id')
        
        # 1. Simulate finding the next term (e.g., current is 80001, next is 80002)
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
        conn = get_db_connection()
        cursor = conn.cursor()
        print("--- [EVAL UPDATE] Starting evaluation fetch process ---")

        for source in sources:
            records = source['fetcher']()
            print(f"🟢 [EVAL UPDATE] Processing {len(records)} records for {source['type']}.")

            for row in records:
                
                # 1. Safely retrieve Class ID. Defaults to 0 if the key is missing from the dictionary.
                class_id_raw = row.get('Class ID', 0)
                try:
                    class_id = int(class_id_raw) if class_id_raw else 0
                except (ValueError, TypeError):
                    class_id = 0
                
                # 2. Extract Qualitative Feedback (NEW)
                qualitative_feedback = row.get('Qualitative Feedback') # Expecting this column from Sheets
                if qualitative_feedback == '':
                    qualitative_feedback = None # Ensure empty strings are stored as NULL

                # Ensure Personnel and Semester IDs are present
                if not row.get('Faculty Personnel ID') or not row.get('Semester_AY ID'):
                    print(f"    - [Record] 🛑 SKIP: Missing Faculty ID or Semester ID in {source['type']} row.")
                    continue
                
                # Database insertion logic
                cursor.execute("""
                    INSERT INTO faculty_evaluations (
                        personnel_id, acadcalendar_id, class_id, evaluator_type, score, total_responses, qualitative_feedback
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (personnel_id, acadcalendar_id, class_id, evaluator_type)
                    DO UPDATE SET 
                        score=EXCLUDED.score, 
                        total_responses=EXCLUDED.total_responses, 
                        qualitative_feedback=EXCLUDED.qualitative_feedback, -- NEW: Update feedback
                        last_updated=CURRENT_TIMESTAMP
                """, (
                    row['Faculty Personnel ID'],
                    row['Semester_AY ID'],
                    class_id,                 
                    source['type'], 
                    row['Score'],
                    row['Total Responses'],
                    qualitative_feedback      # <-- Use the new feedback field
                ))
                total_updated += 1
            
        conn.commit()
        cursor.close()
        return_db_connection(conn)
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
                return_db_connection(conn)
            except:
                pass
        return jsonify(message=f"Critical error processing evaluations. Check logs for details. Error: {str(e)}"), 500


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
        
        conn = get_db_connection()
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
                fullname = f"{honorifics} {firstname} {lastname}"
            else:
                fullname = f"{firstname} {lastname}"
            
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
        cursor.execute("""
            SELECT 
                p.personnel_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                p.hiredate,
                c.collegename,
                pr.position as current_rank,
                EXTRACT(YEAR FROM AGE(CURRENT_DATE, p.hiredate)) + 
                EXTRACT(MONTH FROM AGE(CURRENT_DATE, p.hiredate)) / 12.0 as years_of_service,
                NULL as reg_status,
                NULL as date_initiated
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE 
                p.hiredate IS NOT NULL
                AND EXTRACT(YEAR FROM AGE(CURRENT_DATE, p.hiredate)) >= 3
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
                p.hiredate,
                c.collegename,
                pr.position as current_rank,
                ra.years_of_service,
                ra.current_status as reg_status,
                ra.date_initiated
            FROM regularization_application ra
            JOIN personnel p ON ra.faculty_id = p.personnel_id
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE ra.final_decision IS NULL
            
            ORDER BY hiredate ASC
        """)
        
        all_faculty = cursor.fetchall()
        cursor.close()
        return_db_connection(conn)
        
        # Format regularization data
        regularizations_list = []
        for f in all_faculty:
            (personnel_id, firstname, lastname, honorifics, hiredate, 
             college, rank, years, reg_status, date_initiated) = f
            
            fullname = f"{honorifics} {firstname} {lastname}" if honorifics else f"{firstname} {lastname}"
            
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
                    status_display = "For VPAA Review"
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
                'years_of_service': round(float(years), 2) if years else 0,
                'hiredate': hiredate.strftime('%Y-%m-%d') if hiredate else 'N/A',
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
                             regularizations=regularizations_list)
        
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
                             regularizations=[])


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
@require_auth([20004, 20005])
def vp_promotions():
    """VP/President Promotions Dashboard with promotions and regularizations"""
    try:
        personnel_info = get_personnel_info(session['user_id'])
        user_role = session.get('user_role')
        
        conn = get_db_connection()
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
                fullname = f"{honorifics} {firstname} {lastname}"
            else:
                fullname = f"{firstname} {lastname}"
            
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
        
        # === FETCH ACTIVE REGULARIZATIONS ===
        cursor.execute("""
            SELECT 
                p.personnel_id,
                p.firstname,
                p.lastname,
                p.honorifics,
                p.hiredate,
                c.collegename,
                pr.position as current_rank,
                ra.years_of_service,
                ra.current_status as reg_status,
                ra.date_initiated,
                ra.regularization_id
            FROM regularization_application ra
            JOIN personnel p ON ra.faculty_id = p.personnel_id
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE ra.final_decision IS NULL
            ORDER BY ra.date_initiated DESC
        """)
        
        active_regs = cursor.fetchall()
        cursor.close()
        return_db_connection(conn)
        
        # Format regularization data
        regularizations_list = []
        for f in active_regs:
            (personnel_id, firstname, lastname, honorifics, hiredate, 
             college, rank, years, reg_status, date_initiated, regularization_id) = f
            
            fullname = f"{honorifics} {firstname} {lastname}" if honorifics else f"{firstname} {lastname}"
            
            if years >= 7:
                eligible_for = "Tenured"
                current_tenure = "Regular"
            elif years >= 3:
                eligible_for = "Regular"
                current_tenure = "Probationary"
            else:
                continue
            
            # Determine status display
            if reg_status == 'vpa':
                status_display = "For VPAA Review"
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
                status_display = "Pending"
                status_class = "pending"
            
            regularizations_list.append({
                'regularization_id': regularization_id,
                'personnel_id': personnel_id,
                'name': fullname,
                'department': college or 'N/A',
                'current_rank': rank or 'Instructor',
                'current_tenure': current_tenure,
                'years_of_service': round(float(years), 2) if years else 0,
                'hiredate': hiredate.strftime('%Y-%m-%d') if hiredate else 'N/A',
                'eligible_for': eligible_for,
                'status': status_display,
                'status_class': status_class,
                'reg_status': reg_status,
                'has_application': True
            })
        
        return render_template('vp&pres/vp-promotion.html',
                             vp_name=personnel_info.get('vp_name', 'VP Admin'),
                             college=personnel_info.get('college', 'Office of the VP'),
                             profile_image_base64=personnel_info.get('profile_image_base64', ''),
                             personnelinfo=personnel_info,
                             promotions=promotions_list,
                             regularizations=regularizations_list,
                             user_role=user_role)
        
    except Exception as e:
        print(f"Error in vp_promotions route: {e}")
        import traceback
        traceback.print_exc()
        
        personnel_info = get_personnel_info(session['user_id'])
        
        return render_template('vp&pres/vp-promotion.html',
                             vp_name=personnel_info.get('vp_name', 'VP Admin'),
                             college=personnel_info.get('college', 'Office of the VP'),
                             profile_image_base64=personnel_info.get('profile_image_base64', ''),
                             personnelinfo=personnel_info,
                             promotions=[],
                             regularizations=[],
                             user_role=session.get('user_role'))




# === REGULARIZATION API ROUTES FOR VP/PRESIDENT ===

@app.route('/api/promotion/forward-to-president', methods=['POST'])
@require_auth([20004])  # Only VPAA
def forward_to_president():
    """VPAA forwards promotion to President"""
    try:
        data = request.get_json()
        application_id = data.get('application_id')
        vpa_remarks = data.get('vpa_remarks', '').strip()
        
        if not application_id:
            return jsonify(success=False, error='Application ID is required'), 400
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
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
        
        conn.commit()
        
        # Audit log
        user_id = session.get('user_id')
        personnel_info = get_personnel_info(user_id)
        vp_personnel_id = personnel_info.get('personnel_id')
        
        if vp_personnel_id:
            log_audit_action(
                vp_personnel_id,
                'Promotion forwarded',
                f'VPAA forwarded promotion application ID {application_id} to President',
                before_value='Status: VPAA Review',
                after_value='Status: President Review'
            )
        
        cursor.close()
        return_db_connection(conn)
        
        return jsonify(success=True, message='Application forwarded to President successfully')
        
    except Exception as e:
        print(f"Error forwarding to President: {str(e)}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
        return jsonify(success=False, error=str(e)), 500

@app.route('/api/regularization/approve-by-president', methods=['POST'])
@require_auth([20004, 20005])  # President only
def approve_regularization_by_president():
    """President approves regularization"""
    try:
        data = request.get_json()
        regularization_id = data.get('regularization_id')
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz)
        
        cursor.execute("""
            UPDATE regularization_application 
            SET current_status = %s,
                pres_approval_date = %s,
                final_decision = %s
            WHERE regularization_id = %s
        """, ('approved', current_time, 1, regularization_id))
        
        conn.commit()
        
        # Log audit
        user_id = session.get('user_id')
        personnel_info = get_personnel_info(user_id)
        pres_personnel_id = personnel_info.get('personnel_id')
        
        if pres_personnel_id:
            log_audit_action(
                pres_personnel_id,
                'Regularization approved',
                f'President approved regularization ID {regularization_id}',
                before_value='Status: President Review',
                after_value='Status: Approved'
            )
        
        cursor.close()
        return_db_connection(conn)
        
        return jsonify({
            'success': True, 
            'message': 'Regularization approved successfully'
        })
    
    except Exception as e:
        print(f"Error approving regularization: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/regularization/reject', methods=['POST'])
@require_auth([20004, 20005])  # VPAA or President
def reject_regularization():
    """VP/President rejects regularization"""
    try:
        data = request.get_json()
        regularization_id = data.get('regularization_id')
        remarks = data.get('remarks', '')
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz)
        
        user_role = session.get('user_role')
        
        if user_role == 20004:  # VPAA
            cursor.execute("""
                UPDATE regularization_application 
                SET current_status = %s,
                    final_decision = %s,
                    vpa_notes = %s
                WHERE regularization_id = %s
            """, ('rejected', 0, remarks, regularization_id))
        else:  # President
            cursor.execute("""
                UPDATE regularization_application 
                SET current_status = %s,
                    final_decision = %s,
                    pres_notes = %s
                WHERE regularization_id = %s
            """, ('rejected', 0, remarks, regularization_id))
        
        conn.commit()
        cursor.close()
        return_db_connection(conn)
        
        return jsonify({
            'success': True, 
            'message': 'Regularization rejected'
        })
    
    except Exception as e:
        print(f"Error rejecting regularization: {e}")
        if conn:
            conn.rollback()
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
            conn = get_db_connection()
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
            return_db_connection(conn)
        except Exception as e:
            print(f"Error logging logout action: {e}")
    
    session.clear()
    return redirect(url_for('login'))

# Test database connection
@app.route('/test-db')
def test_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT version();")
        version = cursor.fetchone()
        cursor.close()
        return_db_connection(conn)
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

    conn = get_db_connection()
    cursor = conn.cursor()

    # Get faculty_id from personnel via user_id
    cursor.execute("""
        SELECT personnel_id FROM personnel WHERE user_id = %s
    """, (userid,))
    result = cursor.fetchone()
    if not result:
        cursor.close()
        conn.close()
        return "Faculty record not found for the current user.", 400

    faculty_id = result[0]

    # Get requested rank from form
    requested_rank = request.form.get('requested_rank')
    
    if not requested_rank:
        cursor.close()
        conn.close()
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
        conn.close()
        return "Both Resume/CV and Cover Letter are required.", 400

    # Insert new application row with requested_rank
    cursor.execute("""
        INSERT INTO promotion_application (
            faculty_id, cover_letter, resume, resume_filename, cover_letter_filename, 
            requested_rank, date_submitted, current_status
        ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s)
        RETURNING application_id
    """, (faculty_id, cover_letter_data, resume_cv_data, resume_cv_filename, 
          cover_letter_filename, requested_rank, 'hrmd'))
    
    conn.commit()
    cursor.close()
    conn.close()

    return redirect(url_for('faculty_promotion'))


@app.route('/faculty/promotion/view_resume')
@require_auth([20001, 20002])
def view_resume():
    userid = session.get("user_id")
    if not userid:
        return "Unauthorized", 401

    conn = get_db_connection()
    cursor = conn.cursor()

    # get faculty_id
    cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (userid,))
    res = cursor.fetchone()
    if not res:
        cursor.close()
        conn.close()
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
    conn.close()

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

    conn = get_db_connection()
    cursor = conn.cursor()

    # Get faculty_id from user_id
    cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (userid,))
    result = cursor.fetchone()
    if not result:
        cursor.close()
        conn.close()
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
    conn.close()

    if cover_letter and cover_letter[0]:
        return Response(
            cover_letter[0],
            mimetype='application/pdf',
            headers={"Content-Disposition": "inline; filename=cover_letter.pdf"}
        )
    else:
        return "Cover Letter not found", 404

    
@app.route('/delete_submission', methods=['POST'])  # Changed route and added POST
@require_auth([20001, 20002])
def delete_submission():
    userid = session.get("user_id")
    if not userid:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Get faculty_id
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (userid,))
        result = cursor.fetchone()
        if not result:
            cursor.close()
            return_db_connection(conn)
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
            return_db_connection(conn)
            return jsonify({'success': False, 'error': 'No active application found to delete'}), 400

        conn.commit()
        cursor.close()
        return_db_connection(conn)

        return jsonify({'success': True, 'message': 'Application deleted successfully'})
    
    except Exception as e:
        conn.rollback()
        cursor.close()
        return_db_connection(conn)
        return jsonify({'success': False, 'error': str(e)}), 500



@app.route('/api/promotion/details/<int:application_id>')
@require_auth([20003, 20004, 20005])
def get_promotion_details(application_id):
    """Get detailed promotion information - COMPLETE VERSION"""
    try:
        conn = get_db_connection()
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
                pa.rejection_reason
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
            return_db_connection(conn)
            return jsonify(success=False, error='Application not found'), 404
        
        (app_id, faculty_id, firstname, lastname, honorifics, phone, college, 
         currentrank, status, submitted, email, cover_filename, resume_filename, 
         cover_data, resume_data, requested_rank, hrmd_remarks, vpa_remarks, 
         pres_remarks, rejection_reason) = row
        
        fullname = f"{honorifics} {firstname} {lastname}" if honorifics else f"{firstname} {lastname}"
        
        # Get profile image
        profile_image_base64 = None
        cursor.execute("SELECT profilepic FROM profile WHERE personnel_id = %s", (faculty_id,))
        pic_row = cursor.fetchone()
        if pic_row and pic_row[0]:
            profile_image_base64 = f"data:image/jpeg;base64,{base64.b64encode(bytes(pic_row[0])).decode('utf-8')}"
        
        cursor.close()
        return_db_connection(conn)
        
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
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT profilepic FROM profile WHERE personnel_id = %s",
            (personnel_id,)
        )
        
        result = cursor.fetchone()
        cursor.close()
        return_db_connection(conn)
        
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
@require_auth([20003, 20004])  # HR only
def get_promotion_document(application_id, doc_type):
    """Serve PDF documents from promotion_application table"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Validate doc_type
        valid_types = ['cover_letter', 'resume']
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
        return_db_connection(conn)
        
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
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
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
        
        conn.commit()
        
        # Audit log
        user_id = session.get('user_id')
        personnel_info = get_personnel_info(user_id)
        hr_personnel_id = personnel_info.get('personnel_id')
        
        if hr_personnel_id:
            log_audit_action(
                hr_personnel_id,
                'Promotion forwarded',
                f'HR forwarded promotion application ID {application_id} to VPAA',
                before_value='Status: HRMD Review',
                after_value='Status: VPAA Review'
            )
        
        cursor.close()
        return_db_connection(conn)
        
        return jsonify(success=True, message='Application forwarded to VPAA successfully')
        
    except Exception as e:
        print(f"Error forwarding to VPAA: {str(e)}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
        return jsonify(success=False, error=str(e)), 500


@app.route('/api/promotion/approve', methods=['POST'])
@require_auth([20004])  # Only President
def approve_promotion():
    """Final approval of promotion application by President"""
    try:
        data = request.get_json()
        application_id = data.get('application_id')
        pres_remarks = data.get('pres_remarks', '').strip()
        
        if not application_id:
            return jsonify(success=False, error='Application ID is required'), 400
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get application details
        cursor.execute(
            "SELECT faculty_id, requested_rank FROM promotion_application WHERE application_id = %s",
            (application_id,)
        )
        result = cursor.fetchone()
        
        if not result:
            cursor.close()
            return_db_connection(conn)
            return jsonify(success=False, error='Application not found'), 404
        
        faculty_id, requested_rank = result
        
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
        
        conn.commit()
        
        # Audit log
        user_id = session.get('user_id')
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        if personnel_id:
            log_audit_action(
                personnel_id,
                'Promotion approved',
                f'President approved promotion to {requested_rank} for application ID {application_id}',
                before_value='Status: President Review',
                after_value=f'Status: Approved (Rank updated to {requested_rank})'
            )
        
        cursor.close()
        return_db_connection(conn)
        
        return jsonify(success=True, message=f'Promotion approved! Faculty rank updated to {requested_rank}')
        
    except Exception as e:
        print(f"Error approving promotion: {str(e)}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
        return jsonify(success=False, error=str(e)), 500



@app.route('/api/promotion/reject', methods=['POST'])
@require_auth([20003, 20004, 20005])  # HR, VPAA, and President can reject
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
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get current status
        cursor.execute(
            "SELECT current_status FROM promotion_application WHERE application_id = %s",
            (application_id,)
        )
        result = cursor.fetchone()
        
        if not result:
            cursor.close()
            return_db_connection(conn)
            return jsonify(success=False, error='Application not found'), 404
        
        current_status = result[0]
        
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
        
        # Audit log
        user_id = session.get('user_id')
        personnel_info = get_personnel_info(user_id)
        personnel_id = personnel_info.get('personnel_id')
        
        if personnel_id:
            log_audit_action(
                personnel_id,
                'Promotion rejected',
                f'Rejected promotion application ID {application_id}',
                before_value=f'Status: {current_status}',
                after_value=f'Status: Rejected (Reason: {rejection_reason})'
            )
        
        cursor.close()
        return_db_connection(conn)
        
        return jsonify(success=True, message='Promotion application rejected')
        
    except Exception as e:
        print(f"Error rejecting promotion: {str(e)}")
        import traceback
        traceback.print_exc()
        if conn:
            conn.rollback()
        return jsonify(success=False, error=str(e)), 500


@app.route('/api/regularization/eligible-faculty')
@require_auth([20003])
def get_eligible_faculty():
    """Get list of faculty eligible for regularization"""
    try:
        conn = get_db_connection()
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
                EXTRACT(YEAR FROM AGE(CURRENT_DATE, p.hiredate)) + 
                EXTRACT(MONTH FROM AGE(CURRENT_DATE, p.hiredate)) / 12.0 as years_of_service
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN profile pr ON p.personnel_id = pr.personnel_id
            WHERE 
                p.hiredate IS NOT NULL
                AND EXTRACT(YEAR FROM AGE(CURRENT_DATE, p.hiredate)) >= 3
                AND p.personnel_id NOT IN (
                    SELECT faculty_id 
                    FROM regularization_application 
                    WHERE final_decision IS NULL
                )
            ORDER BY p.hiredate ASC
        """)
        
        eligible_faculty = cursor.fetchall()
        cursor.close()
        return_db_connection(conn)
        
        faculty_list = []
        for f in eligible_faculty:
            (personnel_id, firstname, lastname, honorifics, hiredate, 
             college, rank, years) = f
            
            fullname = f"{honorifics} {firstname} {lastname}" if honorifics else f"{firstname} {lastname}"
            
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
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT hiredate FROM personnel WHERE personnel_id = %s", (faculty_id,))
        result = cursor.fetchone()
        
        if not result or not result[0]:
            cursor.close()
            return_db_connection(conn)
            return jsonify({'success': False, 'error': 'Faculty not found or no hire date'}), 400
        
        hire_date = result[0]
        
        from datetime import date
        today = date.today()
        years = today.year - hire_date.year
        months = today.month - hire_date.month
        if months < 0:
            years -= 1
            months += 12
        years_of_service = years + (months / 12.0)
        
        if years_of_service < 3:
            cursor.close()
            return_db_connection(conn)
            return jsonify({
                'success': False, 
                'error': f'Faculty not eligible. Only {years_of_service:.1f} years of service.'
            }), 400
        
        cursor.execute("""
            SELECT regularization_id FROM regularization_application 
            WHERE faculty_id = %s AND final_decision IS NULL
        """, (faculty_id,))
        
        if cursor.fetchone():
            cursor.close()
            return_db_connection(conn)
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
        conn.commit()
        
        user_id = session.get('user_id')
        personnel_info = get_personnel_info(user_id)
        hr_personnel_id = personnel_info.get('personnel_id')
        
        if hr_personnel_id:
            log_audit_action(
                hr_personnel_id,
                'Regularization initiated',
                f'HR initiated regularization for faculty ID {faculty_id}',
                before_value=f'Years of service: {years_of_service:.2f}',
                after_value='Status: Pending VPAA review'
            )
        
        cursor.close()
        return_db_connection(conn)
        
        next_tenure = "Tenured" if years_of_service >= 7 else "Regular"
        
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
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/regularization/approve', methods=['POST'])
@require_auth([20004])
def approve_regularization():
    """President approves regularization"""
    try:
        data = request.get_json()
        regularization_id = data.get('regularization_id')
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz)
        
        cursor.execute("""
            UPDATE regularization_application 
            SET current_status = %s,
                pres_approval_date = %s,
                final_decision = %s
            WHERE regularization_id = %s
        """, ('approved', current_time, 1, regularization_id))
        
        conn.commit()
        cursor.close()
        return_db_connection(conn)
        
        return jsonify({'success': True, 'message': 'Regularization approved successfully'})
    
    except Exception as e:
        print(f"Error approving regularization: {e}")
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/faculty/promotion/eligibility')
@require_auth([20001, 20002])
def api_promotion_eligibility():
    """Check if faculty is eligible for promotion"""
    try:
        user_id = session.get('user_id')
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get faculty hire date
        cursor.execute("""
            SELECT personnel_id, hiredate 
            FROM personnel 
            WHERE user_id = %s
        """, (user_id,))
        
        result = cursor.fetchone()
        if not result:
            cursor.close()
            return_db_connection(conn)
            return jsonify({'success': False, 'error': 'Faculty not found'})
        
        personnel_id, hire_date = result
        
        # Calculate years employed
        from datetime import date
        today = date.today()
        years_employed = 0
        can_apply = False
        tenure_type = "Unknown"
        
        if hire_date:
            years_employed = today.year - hire_date.year
            months_employed = today.month - hire_date.month
            
            if months_employed < 0:
                years_employed -= 1
            
            # Determine eligibility
            if years_employed >= 3:
                can_apply = True
                tenure_type = "Regular" if years_employed < 7 else "Tenured"
            else:
                can_apply = False
                tenure_type = "Probationary"
        
        # Check for active promotion application
        cursor.execute("""
            SELECT COUNT(*) 
            FROM promotion_application 
            WHERE faculty_id = %s AND final_decision IS NULL
        """, (personnel_id,))
        
        has_active = cursor.fetchone()[0] > 0
        
        cursor.close()
        return_db_connection(conn)
        
        return jsonify({
            'success': True,
            'can_apply': can_apply,
            'tenure_type': tenure_type,
            'years_employed': years_employed,
            'has_active_application': has_active
        })
        
    except Exception as e:
        print(f"Error checking promotion eligibility: {e}")
        return jsonify({'success': False, 'error': str(e)})



if __name__ == "__main__":
    app.run(debug=True)