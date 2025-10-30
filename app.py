import os
from datetime import datetime, timedelta, date
import pytz
from flask import Flask, render_template, request, redirect, url_for, session
from dotenv import load_dotenv
import pg8000
from pg8000 import dbapi

load_dotenv()

app = Flask(__name__)
app.secret_key = 'spc-faculty-system-2025-secret-key'

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
        
        # Initialize minimum connections
        for _ in range(min_connections):
            self.pool.put(self._create_connection())
            self.current_connections += 1
    
    def _create_connection(self):
        return pg8000.dbapi.connect(
            host=os.getenv('DB_HOST', 'dpg-d3ue6mbe5dus739khe70-a.oregon-postgres.render.com'),
            port=int(os.getenv('DB_PORT', 5432)),
            database=os.getenv('DB_NAME', 'spcheck'),
            user=os.getenv('DB_USER', 'spcheck_user'),
            password=os.getenv('DB_PASSWORD', 'Z2Z7rEXFpHmge1rgxM2qBXBERkDAuC7c'),
            ssl_context=True
        )
    
    def get_connection(self):
        try:
            conn = self.pool.get(block=False)
            # Test if connection is still alive
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.close()
                return conn
            except:
                # Connection dead, create new one
                conn.close()
                return self._create_connection()
        except Empty:
            with self.lock:
                if self.current_connections < self.max_connections:
                    self.current_connections += 1
                    return self._create_connection()
            # Wait for available connection
            return self.pool.get(block=True, timeout=5)
    
    def return_connection(self, conn):
        try:
            self.pool.put(conn, block=False)
        except:
            # Pool is full, close connection
            conn.close()
            with self.lock:
                self.current_connections -= 1

# Initialize connection pool
db_pool = ConnectionPool(min_connections=3, max_connections=15)

def get_db_connection():
    return db_pool.get_connection()

def return_db_connection(conn):
    db_pool.return_connection(conn)

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

# Role mapping for redirections
ROLE_REDIRECTS = {
    20001: ('faculty', 'faculty_dashboard'),
    20002: ('dean', 'faculty_dashboard'),
    20003: ('hrmd', 'hr_dashboard'),
    20004: ('vppres', 'vp_promotions')
}

def get_personnel_info(user_id):
    """Get personnel information - OPTIMIZED with single query"""
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
                p.personnel_id
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
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus, personnel_id = result
            
            full_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
            
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
                'personnel_id': personnel_id
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
        'employment_status': 'Regular'
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
        
        # OPTIMIZED: Single query to get everything needed
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
        
        # Build attendance map
        attendance_map = {}
        for record in attendance_records:
            class_id = record['class_id']
            timein = record['timein']
            if timein:
                date_key = f"{class_id}_{timein[:10]}"  # Extract date part
            else:
                date_key = f"{class_id}_absent_{len(attendance_map)}"
            attendance_map[date_key] = record
        
        attendance_logs = []
        class_attendance = []
        status_counts = {'present': 0, 'late': 0, 'absent': 0}
        
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
                        # Check for absent record
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

@app.route('/api/faculty/simulate-rfid', methods=['POST'])
@require_auth([20001, 20002])
def api_simulate_rfid():
    """API endpoint to simulate RFID tap for testing"""
    try:
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT p.personnel_id, sch.class_id
            FROM personnel p
            JOIN schedule sch ON p.personnel_id = sch.personnel_id
            WHERE p.user_id = %s
            ORDER BY RANDOM()
            LIMIT 1
        """, (user_id,))
        
        result = cursor.fetchone()
        
        if not result:
            cursor.close()
            return_db_connection(conn)
            return {'success': False, 'error': 'No classes found for this faculty member'}
        
        personnel_id, class_id = result
        
        import random
        statuses = ['Present', 'Late', 'Absent']
        status = random.choice(statuses)
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz).replace(microsecond=0)
        
        if status == 'Absent':
            timein = None
            timeout = None
        else:
            timein = current_time.replace(hour=8, minute=random.randint(0, 30 if status == 'Late' else 5))
            timeout = timein.replace(hour=12, minute=0)
        
        cursor.execute("""
            INSERT INTO attendance (personnel_id, class_id, attendancestatus, timein, timeout)
            VALUES (%s, %s, %s, %s, %s)
        """, (personnel_id, class_id, status, timein, timeout))
        
        conn.commit()
        cursor.close()
        return_db_connection(conn)
        
        return {'success': True, 'message': f'Simulated {status} attendance record created'}
        
    except Exception as e:
        print(f"Error simulating RFID: {e}")
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/semesters')
@require_auth([20001, 20002, 20003])
def api_faculty_semesters():
    """API endpoint to get available semesters - CACHED"""
    cache_key = "all_semesters"
    cached = get_cached(cache_key, ttl=1800)  # Cache for 30 minutes
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
            
            display_text = f"{semester}, AY {acadyear}"
            
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
    """OPTIMIZED: Get faculty attendance data for specific semester"""
    try:
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()

        # OPTIMIZED: Single complex query
        cursor.execute("""
            WITH semester_info AS (
                SELECT acadcalendar_id, semester, acadyear, semesterstart, semesterend 
                FROM acadcalendar 
                WHERE acadcalendar_id = %s
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
        
        semester_start = date.fromisoformat(semester_info_json['semesterstart'])
        semester_end = date.fromisoformat(semester_info_json['semesterend'])
        
        scheduled_classes = scheduled_classes_json or []
        attendance_records = attendance_records_json or []
        
        # Build attendance map
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
        status_counts = {'present': 0, 'late': 0, 'absent': 0}
        unique_sections = set()
        total_units = 0
        
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
            units = scheduled_class['units']
            class_section = scheduled_class['classsection']
            classroom = scheduled_class['classroom']
            
            class_name = f"{subject_code} - {subject_name}"
            
            section_key = f"{subject_code}_{class_section}"
            if section_key not in unique_sections:
                unique_sections.add(section_key)
                total_units += units or 3
            
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
                    date_key = f"{class_id}_{check_date}"
                    
                    found_record = attendance_map.get(date_key)
                    
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
                    
                    check_date += timedelta(days=7)
        
        attendance_logs.sort(key=lambda x: x['date'], reverse=True)
        class_attendance.sort(key=lambda x: x['date'], reverse=True)
        
        total_classes = len(attendance_logs)
        attendance_percent = round((status_counts['present'] + status_counts['late']) / total_classes * 100, 1) if total_classes > 0 else 0
        
        kpis = {
            'attendance_percent': f'{attendance_percent}%',
            'late_count': status_counts['late'],
            'absence_count': status_counts['absent'],
            'total_classes': total_classes,
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

@app.route('/api/faculty/teaching-schedule/<int:semester_id>')
@require_auth([20001, 20002, 20003])
def api_faculty_teaching_schedule(semester_id):
    """OPTIMIZED: Get faculty teaching schedule as a timetable"""
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

        # OPTIMIZED: Single query
        cursor.execute("""
            SELECT 
                ac.acadcalendar_id,
                ac.semester,
                ac.acadyear,
                json_agg(
                    json_build_object(
                        'classday_1', sch.classday_1,
                        'starttime_1', sch.starttime_1,
                        'endtime_1', sch.endtime_1,
                        'classday_2', sch.classday_2,
                        'starttime_2', sch.starttime_2,
                        'endtime_2', sch.endtime_2,
                        'subjectcode', sub.subjectcode,
                        'classroom', sch.classroom,
                        'classsection', sch.classsection
                    )
                ) as schedule_data
            FROM acadcalendar ac
            LEFT JOIN schedule sch ON sch.acadcalendar_id = ac.acadcalendar_id AND sch.personnel_id = %s
            LEFT JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE ac.acadcalendar_id = %s
            GROUP BY ac.acadcalendar_id, ac.semester, ac.acadyear
        """, (personnel_id, semester_id))
        
        result = cursor.fetchone()
        cursor.close()
        return_db_connection(conn)
        
        if not result:
            return {'success': False, 'error': 'Academic calendar not found'}
        
        acadcalendar_id, semester_name, acad_year, schedule_data = result
        scheduled_classes = schedule_data or []
        
        def format_time_12hr(time_val):
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
        
        def get_time_slot(start_time, end_time):
            if not start_time or not end_time:
                return None
            
            start_str = format_time_12hr(start_time)
            end_str = format_time_12hr(end_time)
            
            if not start_str or not end_str:
                return None
            
            time_slot = f"{start_str} - {end_str}"
            
            time_slot_mapping = {
                '7:30 AM - 9:00 AM': '7:30 AM - 9:00 AM',
                '9:15 AM - 10:45 AM': '9:15 AM - 10:45 AM', 
                '11:00 AM - 12:30 PM': '11:00 AM - 12:30 PM',
                '12:45 PM - 2:15 PM': '12:45 PM - 2:15 PM',
                '2:30 PM - 4:00 PM': '2:30 PM - 4:00 PM',
                '4:15 PM - 5:45 PM': '4:15 PM - 5:45 PM',
                '6:00 PM - 7:30 PM': '6:00 PM - 7:30 PM'
            }
            
            if time_slot in time_slot_mapping:
                return time_slot_mapping[time_slot]
            
            start_time_only = start_str
            for slot in time_slot_mapping.keys():
                if slot.startswith(start_time_only):
                    return slot
            
            return time_slot
        
        timetable = {}
        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
        
        for day in days:
            timetable[day] = {
                '7:30 AM - 9:00 AM': None,
                '9:15 AM - 10:45 AM': None,
                '11:00 AM - 12:30 PM': None,
                '12:45 PM - 2:15 PM': None,
                '2:30 PM - 4:00 PM': None,
                '4:15 PM - 5:45 PM': None,
                '6:00 PM - 7:30 PM': None
            }
        
        for scheduled_class in scheduled_classes:
            if not scheduled_class.get('subjectcode'):
                continue
                
            day1 = scheduled_class.get('classday_1')
            start1 = scheduled_class.get('starttime_1')
            end1 = scheduled_class.get('endtime_1')
            day2 = scheduled_class.get('classday_2')
            start2 = scheduled_class.get('starttime_2')
            end2 = scheduled_class.get('endtime_2')
            subject_code = scheduled_class['subjectcode']
            classroom = scheduled_class.get('classroom')
            section = scheduled_class.get('classsection')
            
            if day1 and start1 and end1:
                time_slot = get_time_slot(start1, end1)
                if time_slot and day1 in timetable:
                    timetable[day1][time_slot] = {
                        'subject_code': subject_code,
                        'classroom': classroom or 'TBA',
                        'section': section or 'N/A'
                    }
            
            if day2 and start2 and end2:
                time_slot = get_time_slot(start2, end2)
                if time_slot and day2 in timetable:
                    timetable[day2][time_slot] = {
                        'subject_code': subject_code,
                        'classroom': classroom or 'TBA',
                        'section': section or 'N/A'
                    }
        
        semester_info = {
            'id': acadcalendar_id,
            'name': semester_name,
            'year': acad_year,
            'display': f"{semester_name}, {acad_year}"
        }
        
        return {
            'success': True,
            'timetable': timetable,
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
        
        # OPTIMIZED: One mega query for all dashboard data
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
        
        # Calculate attendance rate
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
        
        # Build weekly schedule
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
                
                # Fetch again
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
        
        # Update phone in personnel table
        cursor.execute("""
            UPDATE personnel SET phone = %s WHERE personnel_id = %s
        """, (phone, personnel_id))
        
        # Check if profile exists
        cursor.execute("""
            SELECT profile_id FROM profile WHERE personnel_id = %s
        """, (personnel_id,))
        profile_exists = cursor.fetchone()
        
        if profile_exists:
            # Update existing profile
            cursor.execute("""
                UPDATE profile SET bio = %s WHERE personnel_id = %s
            """, (bio, personnel_id))
        else:
            # Create new profile
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
        
        # Clear cache
        cache_key = f"personnel_info_{user_id}"
        with _cache_lock:
            _cache.pop(cache_key, None)
        
        print(f"Personal info updated for personnel_id: {personnel_id}, phone: {phone}")
        return {'success': True, 'message': 'Personal information updated successfully'}
        
    except Exception as e:
        print(f"Error updating personal info: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/profile/documents', methods=['POST'])
@require_auth([20001, 20002, 20003, 20004])
def api_update_documents():
    """API endpoint to update document uploads"""
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
        
        # Ensure profile exists
        cursor.execute("""
            INSERT INTO profile (profile_id, personnel_id, bio, profilepic, 
                licenses, degrees, certificates, publications, awards,
                licensesname, degreesname, certificatesname, publicationsname, awardsname,
                employmentstatus, position)
            VALUES (
                (SELECT COALESCE(MAX(profile_id), 90000) + 1 FROM profile),
                %s, '', NULL,
                ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[],
                ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[],
                'Regular', 'Full-Time Employee'
            )
            ON CONFLICT (personnel_id) DO NOTHING
        """, (personnel_id,))
        conn.commit()
        
        if 'profilepic' in request.files:
            file = request.files['profilepic']
            if file and file.filename:
                profilepic_data = file.read()
                cursor.execute("""
                    UPDATE profile SET profilepic = %s WHERE personnel_id = %s
                """, (profilepic_data, personnel_id))
                conn.commit()
        
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
                        new_docs.append(f.read())
                        new_filenames.append(f.filename)
                
                if new_docs:
                    combined_docs = existing_docs + new_docs
                    combined_filenames = existing_filenames + new_filenames
                    
                    cursor.execute(f"""
                        UPDATE profile 
                        SET {doc_type} = %s, {filename_col} = %s 
                        WHERE personnel_id = %s
                    """, (combined_docs, combined_filenames, personnel_id))
                    conn.commit()
        
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
        
        cursor.execute("""
            UPDATE users SET password = %s WHERE user_id = %s
        """, (new_password, user_id))
        
        conn.commit()
        cursor.close()
        return_db_connection(conn)
        
        return {'success': True, 'message': 'Password updated successfully'}
        
    except Exception as e:
        print(f"Error updating password: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/profile/document/<doc_type>/<int:index>', methods=['DELETE'])
@require_auth([20001, 20002, 20003, 20004])
def api_delete_document(doc_type, index):
    """API endpoint to delete a specific document from an array"""
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
        doc_array.pop(index)
        if index < len(filenames):
            filenames.pop(index)
        
        cursor.execute(f"""
            UPDATE profile 
            SET {doc_type} = %s, {filename_col} = %s 
            WHERE personnel_id = %s
        """, (doc_array, filenames, personnel_id))
        
        conn.commit()
        cursor.close()
        return_db_connection(conn)
        
        return {'success': True, 'message': 'Document deleted successfully'}
        
    except Exception as e:
        print(f"Error deleting document: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}
    
@app.route('/api/hr/faculty-attendance')
@require_auth([20003])
def api_hr_faculty_attendance():
    """OPTIMIZED: Get all faculty and dean attendance data for HR"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        philippines_tz = pytz.timezone('Asia/Manila')
        today = datetime.now(philippines_tz).date()
        
        # OPTIMIZED: Single query with aggregation
        cursor.execute("""
            WITH current_calendar AS (
                SELECT acadcalendar_id, semesterstart, semesterend 
                FROM acadcalendar 
                WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                ORDER BY semesterstart DESC
                LIMIT 1
            ),
            faculty_count AS (
                SELECT COUNT(*) as total
                FROM personnel p
                WHERE p.role_id IN (20001, 20002)
            ),
            today_attendance AS (
                SELECT 
                    p.firstname,
                    p.lastname,
                    p.honorifics,
                    a.attendancestatus,
                    a.timein,
                    a.timeout,
                    sch.classroom,
                    CASE 
                        WHEN DATE(a.timein AT TIME ZONE 'Asia/Manila') = %s THEN 1 
                        ELSE 0 
                    END as is_today
                FROM attendance a
                JOIN schedule sch ON a.class_id = sch.class_id
                JOIN personnel p ON a.personnel_id = p.personnel_id
                CROSS JOIN current_calendar cc
                WHERE p.role_id IN (20001, 20002)
                AND sch.acadcalendar_id = cc.acadcalendar_id
                ORDER BY a.timein DESC
            )
            SELECT 
                (SELECT total FROM faculty_count),
                json_agg(row_to_json(today_attendance)) as attendance_data,
                (SELECT COUNT(*) FROM today_attendance WHERE is_today = 1 AND LOWER(attendancestatus) = 'present') as present_today,
                (SELECT COUNT(*) FROM today_attendance WHERE is_today = 1 AND LOWER(attendancestatus) = 'late') as late_today,
                (SELECT COUNT(*) FROM today_attendance WHERE is_today = 1 AND LOWER(attendancestatus) = 'absent') as absent_today,
                (SELECT COUNT(*) FROM today_attendance WHERE LOWER(attendancestatus) = 'present') as total_present,
                (SELECT COUNT(*) FROM today_attendance WHERE LOWER(attendancestatus) = 'late') as total_late,
                (SELECT COUNT(*) FROM today_attendance WHERE LOWER(attendancestatus) = 'absent') as total_absent
            FROM today_attendance
        """, (today,))
        
        result = cursor.fetchone()
        cursor.close()
        return_db_connection(conn)
        
        if not result:
            return {'success': False, 'error': 'No data found'}
        
        total_faculty, attendance_data, present_today, late_today, absent_today, total_present, total_late, total_absent = result
        
        attendance_records = attendance_data or []
        
        attendance_logs = []
        
        for record in attendance_records:
            firstname = record['firstname']
            lastname = record['lastname']
            honorifics = record['honorifics']
            status = record['attendancestatus']
            timein = record['timein']
            timeout = record['timeout']
            classroom = record['classroom']
            
            faculty_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
            
            if timein:
                date_str = timein[:10]
                time_in_str = timein[11:16]
            else:
                date_str = today.strftime('%Y-%m-%d')
                time_in_str = '—'
            
            time_out_str = timeout[11:16] if timeout else '—'
            
            log_entry = {
                'name': faculty_name,
                'date': date_str,
                'room': classroom or 'N/A',
                'time_in': time_in_str,
                'time_out': time_out_str,
                'status': status.capitalize()
            }
            attendance_logs.append(log_entry)
        
        kpis = {
            'total_faculty': total_faculty or 0,
            'present_today': present_today or 0,
            'late_today': late_today or 0,
            'absent_today': absent_today or 0
        }
        
        status_counts = {
            'present': total_present or 0,
            'late': total_late or 0,
            'absent': total_absent or 0
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
        
        # OPTIMIZED: Single query with aggregation
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
    """OPTIMIZED: Get faculty teaching schedule for HR view"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # OPTIMIZED: Single query
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
                json_agg(
                    json_build_object(
                        'classday_1', sch.classday_1,
                        'starttime_1', sch.starttime_1,
                        'endtime_1', sch.endtime_1,
                        'classday_2', sch.classday_2,
                        'starttime_2', sch.starttime_2,
                        'endtime_2', sch.endtime_2,
                        'subjectcode', sub.subjectcode,
                        'classroom', sch.classroom,
                        'classsection', sch.classsection
                    )
                ) as schedule_data
            FROM current_calendar cc
            LEFT JOIN schedule sch ON sch.acadcalendar_id = cc.acadcalendar_id AND sch.personnel_id = %s
            LEFT JOIN subjects sub ON sch.subject_id = sub.subject_id
            GROUP BY cc.acadcalendar_id, cc.semester, cc.acadyear
        """, (personnel_id,))
        
        result = cursor.fetchone()
        cursor.close()
        return_db_connection(conn)
        
        if not result:
            return {'success': False, 'error': 'Academic calendar not found'}
        
        acadcalendar_id, semester_name, acad_year, schedule_data = result
        scheduled_classes = schedule_data or []
        
        def format_time_12hr(time_val):
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
        
        def get_time_slot(start_time, end_time):
            if not start_time or not end_time:
                return None
            
            start_str = format_time_12hr(start_time)
            end_str = format_time_12hr(end_time)
            
            if not start_str or not end_str:
                return None
            
            time_slot = f"{start_str} - {end_str}"
            
            time_slot_mapping = {
                '7:30 AM - 9:00 AM': '7:30 AM - 9:00 AM',
                '9:15 AM - 10:45 AM': '9:15 AM - 10:45 AM', 
                '11:00 AM - 12:30 PM': '11:00 AM - 12:30 PM',
                '12:45 PM - 2:15 PM': '12:45 PM - 2:15 PM',
                '2:30 PM - 4:00 PM': '2:30 PM - 4:00 PM',
                '4:15 PM - 5:45 PM': '4:15 PM - 5:45 PM',
                '6:00 PM - 7:30 PM': '6:00 PM - 7:30 PM'
            }
            
            if time_slot in time_slot_mapping:
                return time_slot_mapping[time_slot]
            
            start_time_only = start_str
            for slot in time_slot_mapping.keys():
                if slot.startswith(start_time_only):
                    return slot
            
            return time_slot
        
        timetable = {}
        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
        
        for day in days:
            timetable[day] = {
                '7:30 AM - 9:00 AM': None,
                '9:15 AM - 10:45 AM': None,
                '11:00 AM - 12:30 PM': None,
                '12:45 PM - 2:15 PM': None,
                '2:30 PM - 4:00 AM': None,
                '4:15 PM - 5:45 PM': None,
                '6:00 PM - 7:30 PM': None
            }
        
        for scheduled_class in scheduled_classes:
            if not scheduled_class or not scheduled_class.get('subjectcode'):
                continue
            
            day1 = scheduled_class.get('classday_1')
            start1 = scheduled_class.get('starttime_1')
            end1 = scheduled_class.get('endtime_1')
            day2 = scheduled_class.get('classday_2')
            start2 = scheduled_class.get('starttime_2')
            end2 = scheduled_class.get('endtime_2')
            subject_code = scheduled_class['subjectcode']
            classroom = scheduled_class.get('classroom')
            section = scheduled_class.get('classsection')
            
            if day1 and start1 and end1:
                time_slot = get_time_slot(start1, end1)
                if time_slot and day1 in timetable:
                    timetable[day1][time_slot] = {
                        'subject_code': subject_code,
                        'classroom': classroom or 'TBA',
                        'section': section or 'N/A'
                    }
            
            if day2 and start2 and end2:
                time_slot = get_time_slot(start2, end2)
                if time_slot and day2 in timetable:
                    timetable[day2][time_slot] = {
                        'subject_code': subject_code,
                        'classroom': classroom or 'TBA',
                        'section': section or 'N/A'
                    }
        
        semester_info = {
            'id': acadcalendar_id,
            'name': semester_name,
            'year': acad_year,
            'display': f"{semester_name}, {acad_year}"
        }
        
        return {
            'success': True,
            'timetable': timetable,
            'semester_info': semester_info
        }
        
    except Exception as e:
        print(f"Error fetching faculty schedule for personnel {personnel_id}: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/employees-list')
@require_auth([20003])
def api_hr_employees_list():
    """OPTIMIZED: Get all employees data for HR directory"""
    cache_key = "hr_employees_list"
    cached = get_cached(cache_key, ttl=300)
    if cached:
        return cached
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # OPTIMIZED: Single query
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
                role_display = 'Admin'
            
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
        
        set_cached(cache_key, result)
        return result
        
    except Exception as e:
        print(f"Error fetching employees list: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/hr_employee_profile/<int:personnel_id>')
@require_auth([20003])
def hr_employee_profile(personnel_id):
    """HR view of employee profile"""
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
                'hr_name': full_name,
                'college': collegename or 'College of Computer Studies',
                'employee_no': employee_no,
                'email': email or 'email@spc.edu.ph',
                'position': position or 'Full-Time Employee',
                'employment_status': employmentstatus or 'Regular',
                'firstname': firstname,
                'is_hr_viewing': True
            }
            
            session['viewing_personnel_id'] = personnel_id
            
            return render_template('hrmd/hr-profile.html', **employee_info)
        else:
            return "Employee not found", 404
            
    except Exception as e:
        print(f"Error loading employee profile: {e}")
        return "Error loading profile", 500

@app.route('/faculty_employee_profile/<int:personnel_id>')
@require_auth([20003])
def faculty_employee_profile(personnel_id):
    """HR view of faculty/dean profile"""
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
                'faculty_name': full_name,
                'college': collegename or 'College of Computer Studies',
                'employee_no': employee_no,
                'email': email or 'email@spc.edu.ph',
                'position': position or 'Full-Time Employee',
                'employment_status': employmentstatus or 'Regular',
                'firstname': firstname,
                'is_hr_viewing': True
            }
            
            session['viewing_personnel_id'] = personnel_id
            
            return render_template('faculty&dean/faculty-profile.html', **employee_info)
        else:
            return "Employee not found", 404
            
    except Exception as e:
        print(f"Error loading employee profile: {e}")
        return "Error loading profile", 500
    
@app.route('/vp_employee_profile/<int:personnel_id>')
@require_auth([20003])
def vp_employee_profile(personnel_id):
    """HR view of VP/President profile"""
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
                'vp_name': full_name,
                'college': collegename or 'College of Computer Studies',
                'employee_no': employee_no,
                'email': email or 'email@spc.edu.ph',
                'position': position or 'Full-Time Employee',
                'employment_status': employmentstatus or 'Regular',
                'firstname': firstname,
                'is_hr_viewing': True
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

# Login route
@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT u.user_id, u.email, u.role_id 
                FROM users u 
                WHERE u.email = %s AND u.password = %s
            """, (email, password))
            
            user = cursor.fetchone()
            
            if user and user[2] in ROLE_REDIRECTS:
                user_id, user_email, role_id = user
    
                philippines_tz = pytz.timezone('Asia/Manila')
                current_time = datetime.now(philippines_tz).replace(microsecond=0)
                cursor.execute("""
                    UPDATE users SET lastlogin = %s WHERE user_id = %s
                """, (current_time, user_id))
                
                conn.commit()
                cursor.close()
                return_db_connection(conn)
                
                session['user_id'] = user_id
                session['email'] = user_email
                session['user_role'] = role_id
                session['user_type'] = ROLE_REDIRECTS[role_id][0]
                
                return redirect(url_for(ROLE_REDIRECTS[role_id][1]))
            else:
                cursor.close()
                return_db_connection(conn)
                return render_template('login.html', error="Invalid credentials. Please try again.")
                
        except Exception as e:
            print(f"Database error: {e}")
            return render_template('login.html', error="Database connection error. Please try again.")
    
    return render_template('login.html')

# Reset password page
@app.route('/reset_password')
def reset_password():
    return render_template('reset.html')

# Faculty/Dean routes with faculty info
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
    userid = session.get("user_id")
    if not userid:
        return "Unauthorized", 401

    conn = get_db_connection()
    cursor = conn.cursor()

    # Get faculty_id for this user
    cursor.execute("""
        SELECT personnel_id FROM personnel WHERE user_id = %s
    """, (userid,))
    result = cursor.fetchone()
    if not result:
        cursor.close()
        conn.close()
        return "Faculty record not found for the current user.", 400

    faculty_id = result[0]

    # Get the latest application for this faculty
    cursor.execute("""
        SELECT current_status, date_submitted, hrmd_approval_date, vpa_approval_date, pres_approval_date, final_decision
        FROM promotion_application
        WHERE faculty_id = %s
        ORDER BY date_submitted DESC
        LIMIT 1
    """, (faculty_id,))
    row = cursor.fetchone()

    cursor.close()
    conn.close()

    if row:
        statuses_in_progress = {"hrmd", "vpa", "pres"}
        upload_locked = row and row[0] in statuses_in_progress
        return render_template(
            'faculty&dean/faculty-promotion.html',
            current_status=row[0],
            date_submitted=row[1],
            hrmd_approval_date=row[2],
            vpa_approval_date=row[3],
            pres_approval_date=row[4],
            final_decision=row[5],
            upload_locked=upload_locked      # <--- add this to the context
        )
    else:
        return render_template(
            'faculty&dean/faculty-promotion.html',
            current_status=None,
            date_submitted=None,
            hrmd_approval_date=None,
            vpa_approval_date=None,
            pres_approval_date=None,
            final_decision=None,
            upload_locked=False              # <--- add this to the context
        )

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
    return render_template('hrmd/hr-evaluations.html', **personnel_info)

@app.route('/hr_attendance')
@require_auth([20003])
def hr_attendance():
    personnel_info = get_personnel_info(session['user_id'])
    return render_template('hrmd/hr-attendance.html', **personnel_info)

@app.route('/hr_promotions')
@require_auth([20003])
def hr_promotions():
    personnel_info = get_personnel_info(session['user_id'])
    return render_template('hrmd/hr-promotions.html', **personnel_info)

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

# VP/President routes
@app.route('/vp_promotions')
@require_auth([20004])
def vp_promotions():
    personnel_info = get_personnel_info(session['user_id'])
    return render_template('vp&pres/vp-promotion.html', **personnel_info)

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

    resume_cv_data = None
    cover_letter_data = None

    if 'resume_cv' in request.files:
        resume_file = request.files['resume_cv']
        if resume_file and resume_file.filename:
            resume_cv_data = resume_file.read()

    if 'cover_letter' in request.files:
        cover_letter_file = request.files['cover_letter']
        if cover_letter_file and cover_letter_file.filename:
            cover_letter_data = cover_letter_file.read()

    if resume_cv_data is None or cover_letter_data is None:
        cursor.close()
        conn.close()
        return "Both resume and cover letter must be uploaded.", 400

    # Insert new application row
    cursor.execute("""
        INSERT INTO promotion_application (
            faculty_id, cover_letter, resume, date_submitted, current_status
        ) VALUES (%s, %s, %s, NOW(), %s)
        RETURNING application_id
    """, (faculty_id, cover_letter_data, resume_cv_data, 'hrmd'))
    conn.commit()
    cursor.close()
    conn.close()

    return redirect(url_for('faculty_promotion'))


if __name__ == "__main__":
    app.run(debug=True)