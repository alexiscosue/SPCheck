import os
from datetime import datetime
import pytz
from flask import Flask, render_template, request, redirect, url_for, session
from dotenv import load_dotenv
import pg8000

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = 'spc-faculty-system-2025-secret-key'

# Database connection function
def get_db_connection():
    return pg8000.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        port=int(os.getenv('DB_PORT', 5432)),
        database=os.getenv('DB_NAME', 'postgres'),
        user=os.getenv('DB_USER', 'postgres'),
        password=os.getenv('DB_PASSWORD')
    )

# Role mapping for redirections
ROLE_REDIRECTS = {
    20001: ('faculty', 'faculty_dashboard'),
    20002: ('dean', 'faculty_dashboard'),
    20003: ('hrmd', 'hr_dashboard'),
    20004: ('vppres', 'vp_promotions')
}

# Helper function to get personnel info (works for all roles)
def get_personnel_info(user_id):
    """Get personnel information from personnel, college, profile, and users tables"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Join personnel, college, profile, and users tables to get complete personnel info
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
            WHERE p.user_id = %s
        """, (user_id,))
        
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus = result
            
            # Format the full name with honorifics at the end
            if honorifics:
                full_name = f"{firstname} {lastname}, {honorifics}"
            else:
                full_name = f"{firstname} {lastname}"
            
            return {
                'personnel_name': full_name,
                'faculty_name': full_name,  # For backward compatibility with faculty templates
                'hr_name': full_name,       # For HR templates
                'vp_name': full_name,       # For VP templates
                'college': collegename or 'College of Computer Studies',
                'employee_no': employee_no,
                'firstname': firstname,
                'lastname': lastname,
                'honorifics': honorifics,
                'role_name': rolename or 'Staff',
                'email': email or 'email@spc.edu.ph',
                'position': position or 'Faculty Member',
                'employment_status': employmentstatus or 'Active'
            }
    except Exception as e:
        print(f"Error getting personnel info: {e}")
    
    # Default values if query fails or no data found
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
        'position': 'Faculty Member',
        'employment_status': 'Active'
    }

# Backward compatibility - keep the old function name
def get_faculty_info(user_id):
    """Get faculty information - wrapper for get_personnel_info"""
    return get_personnel_info(user_id)

# Authentication decorator
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
    """API endpoint to get faculty attendance data"""
    try:
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get personnel_id for the current user
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
        # Get current academic calendar
        cursor.execute("""
            SELECT acadcalendar_id, semesterstart, semesterend 
            FROM acadcalendar 
            WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
            ORDER BY semesterstart DESC
            LIMIT 1
        """)
        academic_calendar = cursor.fetchone()
        
        if not academic_calendar:
            return {'success': False, 'error': 'No active academic calendar found'}
        
        acadcalendar_id, semester_start, semester_end = academic_calendar
        
        # Get all scheduled classes for this faculty including class section
        cursor.execute("""
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
                sch.classsection
            FROM schedule sch
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE sch.personnel_id = %s AND sch.acadcalendar_id = %s
        """, (personnel_id, acadcalendar_id))
        
        scheduled_classes = cursor.fetchall()
        
        # Get existing attendance records including class section
        cursor.execute("""
            SELECT 
                a.class_id,
                a.attendancestatus,
                a.timein,
                a.timeout,
                sub.subjectcode,
                sub.subjectname,
                sch.classsection
            FROM attendance a
            JOIN schedule sch ON a.class_id = sch.class_id
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE a.personnel_id = %s
            ORDER BY COALESCE(a.timein, CURRENT_TIMESTAMP) DESC
        """, (personnel_id,))
        
        attendance_records = cursor.fetchall()
        
        # Create a map of existing attendance by class_id and date
        attendance_map = {}
        for record in attendance_records:
            class_id, status, timein, timeout, subject_code, subject_name, class_section = record
            if timein:
                date_key = f"{class_id}_{timein.date()}"
            else:
                # For absent records, we need to find the corresponding scheduled date
                # This will be handled when we process scheduled classes
                date_key = f"{class_id}_absent_{len(attendance_map)}"
            
            attendance_map[date_key] = {
                'class_id': class_id,
                'status': status,
                'timein': timein,
                'timeout': timeout,
                'subject_code': subject_code,
                'subject_name': subject_name,
                'class_section': class_section
            }
        
        # Generate all expected class dates and cross-reference with attendance
        attendance_logs = []
        class_attendance = []
        status_counts = {'present': 0, 'late': 0, 'absent': 0}
        
        from datetime import datetime, timedelta
        
        # Get Philippines timezone
        philippines_tz = pytz.timezone('Asia/Manila')
        current_date = datetime.now(philippines_tz).date()
        
        for scheduled_class in scheduled_classes:
            class_id, day1, start1, end1, day2, start2, end2, subject_code, subject_name, class_section = scheduled_class
            
            class_name = f"{subject_code} - {subject_name}"
            
            # Process both class days (day1 and day2)
            for day, start_time, end_time in [(day1, start1, end1), (day2, start2, end2)]:
                if not day:  # Skip if no second day
                    continue
                
                # Find all dates for this day of week since semester start
                weekday_map = {
                    'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
                    'Friday': 4, 'Saturday': 5, 'Sunday': 6
                }
                
                target_weekday = weekday_map.get(day)
                if target_weekday is None:
                    continue
                
                # Start from semester start date
                check_date = semester_start
                
                # Find first occurrence of this weekday
                days_ahead = target_weekday - check_date.weekday()
                if days_ahead <= 0:  # Target day already happened this week
                    days_ahead += 7
                check_date += timedelta(days=days_ahead)
                
                # If the first occurrence is before semester start, move to next week
                if check_date < semester_start:
                    check_date += timedelta(days=7)
                
                # Generate all class dates until current date
                while check_date <= current_date and check_date <= semester_end:
                    date_key = f"{class_id}_{check_date}"
                    
                    # Check if attendance record exists for this date
                    found_record = None
                    for key, record in attendance_map.items():
                        if record['class_id'] == class_id:
                            if record['timein'] and record['timein'].date() == check_date:
                                found_record = record
                                break
                            elif not record['timein'] and record['status'] == 'Absent':
                                # This could be our absent record for this date
                                found_record = record
                                break
                    
                    if found_record:
                        # Use existing attendance record
                        status = found_record['status']
                        timein = found_record['timein']
                        timeout = found_record['timeout']
                        record_class_section = found_record['class_section']
                        
                        time_in_str = timein.strftime('%H:%M') if timein else '—'
                        time_out_str = timeout.strftime('%H:%M') if timeout else '—'
                    else:
                        # No attendance record found - mark as absent
                        status = 'Absent'
                        time_in_str = '—'
                        time_out_str = '—'
                        record_class_section = class_section
                    
                    # Create log entry
                    log_entry = {
                        'date': check_date.strftime('%Y-%m-%d'),
                        'time_in': time_in_str,
                        'time_out': time_out_str,
                        'status': status.capitalize(),
                        'class_name': class_name,
                        'class_section': record_class_section or 'N/A'
                    }
                    attendance_logs.append(log_entry)
                    
                    # Create class attendance entry
                    class_entry = {
                        'class_name': class_name,
                        'class_section': record_class_section or 'N/A',
                        'date': check_date.strftime('%Y-%m-%d'),
                        'time_in': time_in_str,
                        'status': status.capitalize()
                    }
                    class_attendance.append(class_entry)
                    
                    # Count statuses
                    if status.lower() == 'present':
                        status_counts['present'] += 1
                    elif status.lower() == 'late':
                        status_counts['late'] += 1
                    elif status.lower() == 'absent':
                        status_counts['absent'] += 1
                    
                    # Move to next week
                    check_date += timedelta(days=7)
        
        # Sort logs by date (most recent first)
        attendance_logs.sort(key=lambda x: x['date'], reverse=True)
        class_attendance.sort(key=lambda x: x['date'], reverse=True)
        
        # Calculate KPIs
        total_classes = len(attendance_logs)
        attendance_percent = round((status_counts['present'] + status_counts['late']) / total_classes * 100, 1) if total_classes > 0 else 0
        
        kpis = {
            'attendance_percent': f'{attendance_percent}%',
            'late_count': status_counts['late'],
            'absence_count': status_counts['absent'],
            'total_classes': total_classes
        }
        
        cursor.close()
        conn.close()
        
        return {
            'success': True,
            'attendance_logs': attendance_logs,
            'class_attendance': class_attendance,
            'status_breakdown': status_counts,
            'kpis': kpis
        }
        
    except Exception as e:
        print(f"Error fetching attendance data: {e}")
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/simulate-rfid', methods=['POST'])
@require_auth([20001, 20002])
def api_simulate_rfid():
    """API endpoint to simulate RFID tap for testing"""
    try:
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get personnel_id for the current user
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
        # Get a random class for the faculty member
        cursor.execute("""
            SELECT class_id FROM schedule 
            WHERE personnel_id = %s 
            ORDER BY RANDOM() 
            LIMIT 1
        """, (personnel_id,))
        
        class_result = cursor.fetchone()
        
        if not class_result:
            return {'success': False, 'error': 'No classes found for this faculty member'}
        
        class_id = class_result[0]
        
        # Simulate random status (Present, Late, or Absent)
        import random
        statuses = ['Present', 'Late', 'Absent']
        status = random.choice(statuses)
        
        # Get current time in Philippines timezone
        philippines_tz = pytz.timezone('Asia/Manila')
        current_time = datetime.now(philippines_tz).replace(microsecond=0)
        
        # For absent, don't set timein/timeout
        if status == 'Absent':
            timein = None
            timeout = None
        else:
            # For present/late, set realistic times
            timein = current_time.replace(hour=8, minute=random.randint(0, 30 if status == 'Late' else 5))
            timeout = timein.replace(hour=12, minute=0)
        
        # Insert simulated attendance record
        cursor.execute("""
            INSERT INTO attendance (personnel_id, class_id, attendancestatus, timein, timeout)
            VALUES (%s, %s, %s, %s, %s)
        """, (personnel_id, class_id, status, timein, timeout))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return {'success': True, 'message': f'Simulated {status} attendance record created'}
        
    except Exception as e:
        print(f"Error simulating RFID: {e}")
        return {'success': False, 'error': str(e)}

# Add these new routes to your app.py file

@app.route('/api/faculty/semesters')
@require_auth([20001, 20002])
def api_faculty_semesters():
    """API endpoint to get available semesters"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get all available semesters, ordered by academic year and semester
        cursor.execute("""
            SELECT 
                acadcalendar_id,
                semester,
                acadyear,
                semesterstart,
                semesterend
            FROM acadcalendar 
            ORDER BY acadyear DESC, 
                     CASE 
                         WHEN semester LIKE '%First%' THEN 1
                         WHEN semester LIKE '%Second%' THEN 2
                         ELSE 3
                     END
        """)
        
        semesters = cursor.fetchall()
        
        # Format the semesters for dropdown
        semester_options = []
        current_semester_id = None
        
        for sem in semesters:
            acadcalendar_id, semester, acadyear, start_date, end_date = sem
            
            # Create display text
            display_text = f"{semester}, AY {acadyear}"
            
            # Check if this is the current semester
            from datetime import date
            today = date.today()
            is_current = start_date <= today <= end_date
            
            if is_current and current_semester_id is None:
                current_semester_id = acadcalendar_id
            
            semester_options.append({
                'id': acadcalendar_id,
                'text': display_text,
                'is_current': is_current,
                'start_date': start_date.isoformat(),
                'end_date': end_date.isoformat()
            })
        
        cursor.close()
        conn.close()
        
        return {
            'success': True,
            'semesters': semester_options,
            'current_semester_id': current_semester_id
        }
        
    except Exception as e:
        print(f"Error fetching semesters: {e}")
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/attendance/<int:semester_id>')
@require_auth([20001, 20002])
def api_faculty_attendance_by_semester(semester_id):
    """API endpoint to get faculty attendance data for specific semester"""
    try:
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get personnel_id for the current user
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
        # Get specific academic calendar
        cursor.execute("""
            SELECT acadcalendar_id, semester, acadyear, semesterstart, semesterend 
            FROM acadcalendar 
            WHERE acadcalendar_id = %s
        """, (semester_id,))
        academic_calendar = cursor.fetchone()
        
        if not academic_calendar:
            return {'success': False, 'error': 'Academic calendar not found'}
        
        acadcalendar_id, semester_name, acad_year, semester_start, semester_end = academic_calendar
        
        # Get all scheduled classes for this faculty in this semester
        cursor.execute("""
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
                sch.classsection
            FROM schedule sch
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE sch.personnel_id = %s AND sch.acadcalendar_id = %s
        """, (personnel_id, acadcalendar_id))
        
        scheduled_classes = cursor.fetchall()
        
        # Get existing attendance records for this semester
        cursor.execute("""
            SELECT 
                a.class_id,
                a.attendancestatus,
                a.timein,
                a.timeout,
                sub.subjectcode,
                sub.subjectname,
                sch.classsection
            FROM attendance a
            JOIN schedule sch ON a.class_id = sch.class_id
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE a.personnel_id = %s AND sch.acadcalendar_id = %s
            ORDER BY COALESCE(a.timein, CURRENT_TIMESTAMP) DESC
        """, (personnel_id, acadcalendar_id))
        
        attendance_records = cursor.fetchall()
        
        # Create a map of existing attendance by class_id and date
        attendance_map = {}
        for record in attendance_records:
            class_id, status, timein, timeout, subject_code, subject_name, class_section = record
            if timein:
                date_key = f"{class_id}_{timein.date()}"
            else:
                # For absent records without timein
                date_key = f"{class_id}_absent_{len(attendance_map)}"
            
            attendance_map[date_key] = {
                'class_id': class_id,
                'status': status,
                'timein': timein,
                'timeout': timeout,
                'subject_code': subject_code,
                'subject_name': subject_name,
                'class_section': class_section
            }
        
        # Generate all expected class dates and cross-reference with attendance
        attendance_logs = []
        class_attendance = []
        status_counts = {'present': 0, 'late': 0, 'absent': 0}
        unique_sections = set()
        total_units = 0
        
        from datetime import datetime, timedelta
        import pytz
        
        # Get Philippines timezone
        philippines_tz = pytz.timezone('Asia/Manila')
        current_date = datetime.now(philippines_tz).date()
        
        for scheduled_class in scheduled_classes:
            class_id, day1, start1, end1, day2, start2, end2, subject_code, subject_name, units, class_section = scheduled_class
            
            class_name = f"{subject_code} - {subject_name}"
            
            # Count unique sections and units
            section_key = f"{subject_code}_{class_section}"
            if section_key not in unique_sections:
                unique_sections.add(section_key)
                total_units += units or 3
            
            # Process both class days (day1 and day2)
            for day, start_time, end_time in [(day1, start1, end1), (day2, start2, end2)]:
                if not day:  # Skip if no second day
                    continue
                
                # Find all dates for this day of week within semester
                weekday_map = {
                    'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
                    'Friday': 4, 'Saturday': 5, 'Sunday': 6
                }
                
                target_weekday = weekday_map.get(day)
                if target_weekday is None:
                    continue
                
                # Start from semester start date
                check_date = semester_start
                
                # Find first occurrence of this weekday
                days_ahead = target_weekday - check_date.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                check_date += timedelta(days=days_ahead)
                
                # If the first occurrence is before semester start, move to next week
                if check_date < semester_start:
                    check_date += timedelta(days=7)
                
                # Generate all class dates within semester bounds and up to current date
                end_date = min(current_date, semester_end)
                while check_date <= end_date:
                    date_key = f"{class_id}_{check_date}"
                    
                    # Check if attendance record exists for this date
                    found_record = None
                    for key, record in attendance_map.items():
                        if record['class_id'] == class_id:
                            if record['timein'] and record['timein'].date() == check_date:
                                found_record = record
                                break
                            elif not record['timein'] and record['status'] == 'Absent':
                                # This could be our absent record for this date
                                found_record = record
                                break
                    
                    if found_record:
                        # Use existing attendance record
                        status = found_record['status']
                        timein = found_record['timein']
                        timeout = found_record['timeout']
                        record_class_section = found_record['class_section']
                        
                        time_in_str = timein.strftime('%H:%M') if timein else '—'
                        time_out_str = timeout.strftime('%H:%M') if timeout else '—'
                    else:
                        # No attendance record found - mark as absent
                        status = 'Absent'
                        time_in_str = '—'
                        time_out_str = '—'
                        record_class_section = class_section
                    
                    # Create log entry
                    log_entry = {
                        'date': check_date.strftime('%Y-%m-%d'),
                        'time_in': time_in_str,
                        'time_out': time_out_str,
                        'status': status.capitalize(),
                        'class_name': class_name,
                        'class_section': record_class_section or 'N/A'
                    }
                    attendance_logs.append(log_entry)
                    
                    # Create class attendance entry
                    class_entry = {
                        'class_name': class_name,
                        'class_section': record_class_section or 'N/A',
                        'date': check_date.strftime('%Y-%m-%d'),
                        'time_in': time_in_str,
                        'status': status.capitalize()
                    }
                    class_attendance.append(class_entry)
                    
                    # Count statuses
                    if status.lower() == 'present':
                        status_counts['present'] += 1
                    elif status.lower() == 'late':
                        status_counts['late'] += 1
                    elif status.lower() == 'absent':
                        status_counts['absent'] += 1
                    
                    # Move to next week
                    check_date += timedelta(days=7)
        
        # Sort logs by date (most recent first)
        attendance_logs.sort(key=lambda x: x['date'], reverse=True)
        class_attendance.sort(key=lambda x: x['date'], reverse=True)
        
        # Calculate KPIs
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
        
        # Add semester info
        semester_info = {
            'id': acadcalendar_id,
            'name': semester_name,
            'year': acad_year,
            'display': f"{semester_name}, AY {acad_year}"
        }
        
        cursor.close()
        conn.close()
        
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
        return {'success': False, 'error': str(e)}

# Add these new API routes to your app.py file

@app.route('/api/faculty/dashboard')
@require_auth([20001, 20002])
def api_faculty_dashboard():
    """API endpoint to get faculty dashboard data"""
    try:
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get personnel_id for the current user
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
        # Get current academic calendar
        cursor.execute("""
            SELECT acadcalendar_id, semester, acadyear, semesterstart, semesterend 
            FROM acadcalendar 
            WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
            ORDER BY semesterstart DESC
            LIMIT 1
        """)
        academic_calendar = cursor.fetchone()
        
        if not academic_calendar:
            return {'success': False, 'error': 'No active academic calendar found'}
        
        acadcalendar_id, semester_name, acad_year, semester_start, semester_end = academic_calendar
        
        # Calculate attendance rate for current semester
        attendance_rate = calculate_attendance_rate(cursor, personnel_id, acadcalendar_id, semester_start, semester_end)
        
        # Get class schedule for current week
        class_schedule = get_weekly_class_schedule(cursor, personnel_id, acadcalendar_id)
        
        # Get teaching load (total units)
        teaching_load = get_teaching_load(cursor, personnel_id, acadcalendar_id)
        
        cursor.close()
        conn.close()
        
        return {
            'success': True,
            'attendance_rate': attendance_rate,
            'class_schedule': class_schedule,
            'teaching_load': teaching_load,
            'semester_info': {
                'name': semester_name,
                'year': acad_year,
                'display': f"{semester_name}, AY {acad_year}"
            }
        }
        
    except Exception as e:
        print(f"Error fetching dashboard data: {e}")
        return {'success': False, 'error': str(e)}

def calculate_attendance_rate(cursor, personnel_id, acadcalendar_id, semester_start, semester_end):
    """Calculate attendance rate for current semester"""
    try:
        from datetime import datetime, timedelta
        import pytz
        
        # Get Philippines timezone
        philippines_tz = pytz.timezone('Asia/Manila')
        current_date = datetime.now(philippines_tz).date()
        
        # Get all scheduled classes for this faculty in current semester
        cursor.execute("""
            SELECT 
                sch.class_id,
                sch.classday_1,
                sch.classday_2
            FROM schedule sch
            WHERE sch.personnel_id = %s AND sch.acadcalendar_id = %s
        """, (personnel_id, acadcalendar_id))
        
        scheduled_classes = cursor.fetchall()
        
        # Get existing attendance records for current semester
        cursor.execute("""
            SELECT 
                a.class_id,
                a.attendancestatus,
                a.timein
            FROM attendance a
            JOIN schedule sch ON a.class_id = sch.class_id
            WHERE a.personnel_id = %s AND sch.acadcalendar_id = %s
        """, (personnel_id, acadcalendar_id))
        
        attendance_records = cursor.fetchall()
        
        # Create attendance map
        attendance_map = {}
        for record in attendance_records:
            class_id, status, timein = record
            if timein:
                date_key = f"{class_id}_{timein.date()}"
                attendance_map[date_key] = status
        
        # Count total expected classes and attendance
        total_classes = 0
        present_late_count = 0
        
        weekday_map = {
            'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
            'Friday': 4, 'Saturday': 5, 'Sunday': 6
        }
        
        for scheduled_class in scheduled_classes:
            class_id, day1, day2 = scheduled_class
            
            # Process both class days
            for day in [day1, day2]:
                if not day:
                    continue
                
                target_weekday = weekday_map.get(day)
                if target_weekday is None:
                    continue
                
                # Find first occurrence of this weekday after semester start
                check_date = semester_start
                days_ahead = target_weekday - check_date.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                check_date += timedelta(days=days_ahead)
                
                if check_date < semester_start:
                    check_date += timedelta(days=7)
                
                # Count classes until current date
                end_date = min(current_date, semester_end)
                while check_date <= end_date:
                    total_classes += 1
                    
                    # Check attendance for this date
                    date_key = f"{class_id}_{check_date}"
                    if date_key in attendance_map:
                        status = attendance_map[date_key].lower()
                        if status in ['present', 'late']:
                            present_late_count += 1
                    # If no record exists, assume absent (don't count towards present/late)
                    
                    check_date += timedelta(days=7)
        
        # Calculate attendance rate
        if total_classes > 0:
            attendance_rate = round((present_late_count / total_classes) * 100)
        else:
            attendance_rate = 0
        
        return {
            'percentage': attendance_rate,
            'total_classes': total_classes,
            'present_late': present_late_count
        }
        
    except Exception as e:
        print(f"Error calculating attendance rate: {e}")
        return {'percentage': 0, 'total_classes': 0, 'present_late': 0}

def get_weekly_class_schedule(cursor, personnel_id, acadcalendar_id):
    """Get class schedule for current week"""
    try:
        from datetime import datetime, timedelta
        import pytz
        
        # Get Philippines timezone
        philippines_tz = pytz.timezone('Asia/Manila')
        current_date = datetime.now(philippines_tz).date()
        
        # Get start of current week (Monday)
        days_since_monday = current_date.weekday()
        week_start = current_date - timedelta(days=days_since_monday)
        
        # Get all scheduled classes for this faculty
        cursor.execute("""
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
                sub.subjectname
            FROM schedule sch
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE sch.personnel_id = %s AND sch.acadcalendar_id = %s
            ORDER BY sch.starttime_1, sch.starttime_2
        """, (personnel_id, acadcalendar_id))
        
        scheduled_classes = cursor.fetchall()
        
        weekly_schedule = []
        
        for scheduled_class in scheduled_classes:
            (class_id, day1, start1, end1, day2, start2, end2, 
             classroom, section, subject_code, subject_name) = scheduled_class
            
            class_name = f"{subject_code} - {subject_name}"
            
            # Process first class day
            if day1 and start1 and end1:
                weekly_schedule.append({
                    'class_name': class_name,
                    'subject_code': subject_code,
                    'subject_name': subject_name,
                    'section': section or 'N/A',
                    'day': day1,
                    'start_time': start1.strftime('%H:%M') if start1 else 'N/A',
                    'end_time': end1.strftime('%H:%M') if end1 else 'N/A',
                    'time_display': f"{start1.strftime('%H:%M')}-{end1.strftime('%H:%M')}" if start1 and end1 else 'N/A',
                    'classroom': classroom or 'N/A'
                })
            
            # Process second class day
            if day2 and start2 and end2:
                weekly_schedule.append({
                    'class_name': class_name,
                    'subject_code': subject_code,
                    'subject_name': subject_name,
                    'section': section or 'N/A',
                    'day': day2,
                    'start_time': start2.strftime('%H:%M') if start2 else 'N/A',
                    'end_time': end2.strftime('%H:%M') if end2 else 'N/A',
                    'time_display': f"{start2.strftime('%H:%M')}-{end2.strftime('%H:%M')}" if start2 and end2 else 'N/A',
                    'classroom': classroom or 'N/A'
                })
        
        # Sort by day and time
        day_order = {'Monday': 1, 'Tuesday': 2, 'Wednesday': 3, 'Thursday': 4, 'Friday': 5, 'Saturday': 6, 'Sunday': 7}
        weekly_schedule.sort(key=lambda x: (day_order.get(x['day'], 8), x['start_time']))
        
        return weekly_schedule
        
    except Exception as e:
        print(f"Error getting weekly class schedule: {e}")
        return []

def get_teaching_load(cursor, personnel_id, acadcalendar_id):
    """Get total teaching load in units"""
    try:
        cursor.execute("""
            SELECT COALESCE(SUM(sub.units), 0) as total_units
            FROM schedule sch
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE sch.personnel_id = %s AND sch.acadcalendar_id = %s
        """, (personnel_id, acadcalendar_id))
        
        result = cursor.fetchone()
        return int(result[0]) if result and result[0] else 0
        
    except Exception as e:
        print(f"Error getting teaching load: {e}")
        return 0

# REPLACE the existing faculty profile API routes in your app.py with these updated versions
# Using your exact column names: licensesnames, degreesnames, etc.

@app.route('/api/faculty/profile')
@require_auth([20001, 20002])
def api_get_faculty_profile():
    """API endpoint to get faculty profile data"""
    try:
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get personnel_id for the current user
        cursor.execute("SELECT personnel_id, phone FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id, phone = personnel_result
        
        # Get profile data including filenames
        cursor.execute("""
            SELECT bio, profilepic, 
                   licenses, degrees, certificates, publications, awards,
                   licensesname, degreesname, certificatesname, 
                   publicationsname, awardsname
            FROM profile 
            WHERE personnel_id = %s
        """, (personnel_id,))
        
        profile_result = cursor.fetchone()
        
        if profile_result:
            (bio, profilepic, licenses, degrees, certificates, publications, awards,
             licenses_fn, degrees_fn, certificates_fn, publications_fn, awards_fn) = profile_result
        else:
            # Create empty profile if doesn't exist
            cursor.execute("""
                INSERT INTO profile (
                    personnel_id, bio, profilepic, 
                    licenses, degrees, certificates, publications, awards,
                    licensesname, degreesname, certificatesname,
                    publicationsname, awardsname
                )
                VALUES (%s, '', NULL, 
                        ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[],
                        ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[])
                RETURNING bio, profilepic, 
                          licenses, degrees, certificates, publications, awards,
                          licensesname, degreesname, certificatesname,
                          publicationsname, awardsname
            """, (personnel_id,))
            conn.commit()
            (bio, profilepic, licenses, degrees, certificates, publications, awards,
             licenses_fn, degrees_fn, certificates_fn, publications_fn, awards_fn) = cursor.fetchone()
        
        # Convert bytea to base64 for frontend
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
        
        # Convert profile picture to base64
        if profilepic:
            profile_data['profilepic'] = base64.b64encode(bytes(profilepic)).decode('utf-8')
        
        # Convert document arrays to base64
        for doc_type in ['licenses', 'degrees', 'certificates', 'publications', 'awards']:
            doc_array = locals()[doc_type]
            if doc_array and len(doc_array) > 0:
                profile_data[doc_type] = [base64.b64encode(bytes(doc)).decode('utf-8') for doc in doc_array]
        
        cursor.close()
        conn.close()
        
        return {'success': True, 'profile': profile_data}
        
    except Exception as e:
        print(f"Error fetching profile data: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/profile/personal', methods=['POST'])
@require_auth([20001, 20002])
def api_update_personal_info():
    """API endpoint to update personal information"""
    try:
        user_id = session['user_id']
        data = request.get_json()
        
        phone_str = data.get('phone', '').strip()
        bio = data.get('bio', '').strip()
        
        # Store phone as string in format: +63 9XXXXXXXXX (no spaces between digits)
        phone = None
        if phone_str and phone_str != '+63 ' and phone_str != '+63':
            # Remove all spaces from input
            phone_clean = phone_str.replace(' ', '')
            
            # Check if it starts with +63
            if phone_clean.startswith('+63'):
                # Extract digits after +63
                phone_digits = phone_clean[3:]
                
                # Validate: must be 10 digits starting with 9
                if len(phone_digits) == 10 and phone_digits[0] == '9' and phone_digits.isdigit():
                    # Store as: +63 9XXXXXXXXX (space after +63, no spaces between digits)
                    phone = '+63 ' + phone_digits
                else:
                    return {'success': False, 'error': 'Phone number must be +63 followed by 10 digits starting with 9'}
            else:
                return {'success': False, 'error': 'Phone number must start with +63'}
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get personnel_id
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
        # Update phone in personnel table (as string)
        cursor.execute("""
            UPDATE personnel SET phone = %s WHERE personnel_id = %s
        """, (phone, personnel_id))
        
        # Check if profile exists
        cursor.execute("SELECT profile_id FROM profile WHERE personnel_id = %s", (personnel_id,))
        profile_exists = cursor.fetchone()
        
        if profile_exists:
            # Update existing profile
            cursor.execute("""
                UPDATE profile SET bio = %s WHERE personnel_id = %s
            """, (bio, personnel_id))
        else:
            # Insert new profile
            cursor.execute("""
                INSERT INTO profile (
                    personnel_id, bio, profilepic, 
                    licenses, degrees, certificates, publications, awards,
                    licensesname, degreesname, certificatesname,
                    publicationsname, awardsname
                )
                VALUES (%s, %s, NULL, 
                        ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[],
                        ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[])
            """, (personnel_id, bio))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"Personal info updated for personnel_id: {personnel_id}, phone: {phone}")
        return {'success': True, 'message': 'Personal information updated successfully'}
        
    except Exception as e:
        print(f"Error updating personal info: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/profile/documents', methods=['POST'])
@require_auth([20001, 20002])
def api_update_documents():
    """API endpoint to update document uploads - APPENDS new files to existing ones"""
    try:
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get personnel_id
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
        # Ensure profile exists
        cursor.execute("SELECT profile_id FROM profile WHERE personnel_id = %s", (personnel_id,))
        profile_exists = cursor.fetchone()
        
        if not profile_exists:
            cursor.execute("""
                INSERT INTO profile (
                    personnel_id, bio, profilepic, 
                    licenses, degrees, certificates, publications, awards,
                    licensesname, degreesname, certificatesname,
                    publicationsname, awardsname
                )
                VALUES (%s, '', NULL, 
                        ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[],
                        ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[])
            """, (personnel_id,))
            conn.commit()
        
        # Handle profile picture upload (this replaces the old one)
        if 'profilepic' in request.files:
            file = request.files['profilepic']
            if file and file.filename:
                profilepic_data = file.read()
                cursor.execute("""
                    UPDATE profile SET profilepic = %s WHERE personnel_id = %s
                """, (profilepic_data, personnel_id))
                conn.commit()
                print(f"Profile picture updated: {len(profilepic_data)} bytes, filename: {file.filename}")
        
        # Mapping for database column names
        column_mapping = {
            'licenses': 'licensesname',
            'degrees': 'degreesname',
            'certificates': 'certificatesname',
            'publications': 'publicationsname',
            'awards': 'awardsname'
        }
        
        # Handle document arrays - APPEND new files to existing ones
        for doc_type in ['licenses', 'degrees', 'certificates', 'publications', 'awards']:
            files = request.files.getlist(doc_type)
            
            if files and any(f.filename for f in files):
                # Get existing documents and filenames first
                filename_col = column_mapping[doc_type]
                cursor.execute(f"""
                    SELECT {doc_type}, {filename_col}
                    FROM profile 
                    WHERE personnel_id = %s
                """, (personnel_id,))
                existing_result = cursor.fetchone()
                
                # Start with existing documents
                existing_docs = list(existing_result[0]) if existing_result and existing_result[0] else []
                existing_filenames = list(existing_result[1]) if existing_result and existing_result[1] else []
                
                # Read new files and append to existing
                new_docs = []
                new_filenames = []
                
                for f in files:
                    if f.filename:
                        new_docs.append(f.read())
                        new_filenames.append(f.filename)
                
                if new_docs:
                    # Combine existing and new
                    combined_docs = existing_docs + new_docs
                    combined_filenames = existing_filenames + new_filenames
                    
                    # Update database with combined arrays
                    cursor.execute(f"""
                        UPDATE profile 
                        SET {doc_type} = %s, {filename_col} = %s 
                        WHERE personnel_id = %s
                    """, (combined_docs, combined_filenames, personnel_id))
                    conn.commit()
                    
                    print(f"Appended to {doc_type}: {len(new_docs)} new documents (total now: {len(combined_docs)})")
                    print(f"New filenames: {new_filenames}")
        
        cursor.close()
        conn.close()
        
        return {'success': True, 'message': 'Documents updated successfully'}
        
    except Exception as e:
        print(f"Error updating documents: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/profile/password', methods=['POST'])
@require_auth([20001, 20002])
def api_update_password():
    """API endpoint to update password"""
    try:
        user_id = session['user_id']
        data = request.get_json()
        
        current_password = data.get('current_password', '')
        new_password = data.get('new_password', '')
        confirm_password = data.get('confirm_password', '')
        
        # Validate passwords
        if not current_password or not new_password or not confirm_password:
            return {'success': False, 'error': 'All password fields are required'}
        
        if new_password != confirm_password:
            return {'success': False, 'error': 'New passwords do not match'}
        
        if len(new_password) < 6:
            return {'success': False, 'error': 'Password must be at least 6 characters long'}
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Verify current password
        cursor.execute("SELECT password FROM users WHERE user_id = %s", (user_id,))
        current_pass_result = cursor.fetchone()
        
        if not current_pass_result or current_pass_result[0] != current_password:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'Current password is incorrect'}
        
        # Check if new password is same as current password
        if current_password == new_password:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'New password cannot be the same as current password'}
        
        # Update password
        cursor.execute("""
            UPDATE users SET password = %s WHERE user_id = %s
        """, (new_password, user_id))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"Password updated for user_id: {user_id}")
        return {'success': True, 'message': 'Password updated successfully'}
        
    except Exception as e:
        print(f"Error updating password: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/profile/document/<doc_type>/<int:index>', methods=['DELETE'])
@require_auth([20001, 20002])
def api_delete_document(doc_type, index):
    """API endpoint to delete a specific document from an array"""
    try:
        user_id = session['user_id']
        
        if doc_type not in ['licenses', 'degrees', 'certificates', 'publications', 'awards']:
            return {'success': False, 'error': 'Invalid document type'}
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get personnel_id
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
        # Mapping for database column names
        column_mapping = {
            'licenses': 'licensesname',
            'degrees': 'degreesname',
            'certificates': 'certificatesname',
            'publications': 'publicationsname',
            'awards': 'awardsname'
        }
        
        filename_col = column_mapping[doc_type]
        
        # Get current document array AND filenames
        cursor.execute(f"""
            SELECT {doc_type}, {filename_col} 
            FROM profile 
            WHERE personnel_id = %s
        """, (personnel_id,))
        doc_result = cursor.fetchone()
        
        if not doc_result or not doc_result[0] or len(doc_result[0]) == 0:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'No documents found'}
        
        doc_array = list(doc_result[0])
        filenames = list(doc_result[1]) if doc_result[1] else []
        
        if index < 0 or index >= len(doc_array):
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'Invalid document index'}
        
        # Remove document and filename at index
        deleted_filename = filenames[index] if index < len(filenames) else f"Document_{index+1}"
        doc_array.pop(index)
        if index < len(filenames):
            filenames.pop(index)
        
        # Update database
        cursor.execute(f"""
            UPDATE profile 
            SET {doc_type} = %s, {filename_col} = %s 
            WHERE personnel_id = %s
        """, (doc_array, filenames, personnel_id))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"Deleted {doc_type} document '{deleted_filename}' at index {index}")
        return {'success': True, 'message': 'Document deleted successfully'}
        
    except Exception as e:
        print(f"Error deleting document: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

# Login route
@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Query to get user with role
            cursor.execute("""
                SELECT u.user_id, u.email, u.role_id 
                FROM users u 
                WHERE u.email = %s AND u.password = %s
            """, (email, password))
            
            user = cursor.fetchone()
            
            if user and user[2] in ROLE_REDIRECTS:
                user_id, user_email, role_id = user
                
                # Update last login with correct timezone (Philippines) - no microseconds
                philippines_tz = pytz.timezone('Asia/Manila')
                current_time = datetime.now(philippines_tz).replace(microsecond=0)
                print(f"DEBUG: Current time being saved: {current_time}")
                cursor.execute("""
                    UPDATE users SET lastlogin = %s WHERE user_id = %s
                """, (current_time, user_id))
                
                conn.commit()
                cursor.close()
                conn.close()
                
                # Set session variables
                session['user_id'] = user_id
                session['email'] = user_email
                session['user_role'] = role_id
                session['user_type'] = ROLE_REDIRECTS[role_id][0]
                
                # Redirect based on role
                return redirect(url_for(ROLE_REDIRECTS[role_id][1]))
            else:
                cursor.close()
                conn.close()
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

@app.route('/faculty_promotion')
@require_auth([20001, 20002])
def faculty_promotion():
    faculty_info = get_faculty_info(session['user_id'])
    return render_template('faculty&dean/faculty-promotion.html', **faculty_info)

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

@app.route('/hr_settings')
@require_auth([20003])
def hr_settings():
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
        conn.close()
        return f"Database connected successfully! Version: {version[0]}"
    except Exception as e:
        return f"Database connection failed: {e}"

if __name__ == "__main__":
    app.run(debug=True)