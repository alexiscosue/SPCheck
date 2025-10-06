import os
from datetime import datetime
import pytz
from flask import Flask, render_template, request, redirect, url_for, session
from dotenv import load_dotenv
import pg8000

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

def get_personnel_info(user_id):
    """Get personnel information from personnel, college, profile, and users tables"""
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
            WHERE p.user_id = %s
        """, (user_id,))
        
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus = result
            
            if honorifics:
                full_name = f"{firstname} {lastname}, {honorifics}"
            else:
                full_name = f"{firstname} {lastname}"
            
            return {
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
                'employment_status': employmentstatus or 'Regular'
            }
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
    """API endpoint to get faculty attendance data"""
    try:
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
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
                sch.classsection,
                sch.classroom
            FROM schedule sch
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE sch.personnel_id = %s AND sch.acadcalendar_id = %s
        """, (personnel_id, acadcalendar_id))
        
        scheduled_classes = cursor.fetchall()
        
        cursor.execute("""
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
            WHERE a.personnel_id = %s
            ORDER BY COALESCE(a.timein, CURRENT_TIMESTAMP) DESC
        """, (personnel_id,))
        
        attendance_records = cursor.fetchall()
        
        attendance_map = {}
        for record in attendance_records:
            class_id, status, timein, timeout, subject_code, subject_name, class_section, classroom = record
            if timein:
                date_key = f"{class_id}_{timein.date()}"
            else:
                date_key = f"{class_id}_absent_{len(attendance_map)}"
            
            attendance_map[date_key] = {
                'class_id': class_id,
                'status': status,
                'timein': timein,
                'timeout': timeout,
                'subject_code': subject_code,
                'subject_name': subject_name,
                'class_section': class_section,
                'classroom': classroom
            }
        
        attendance_logs = []
        class_attendance = []
        status_counts = {'present': 0, 'late': 0, 'absent': 0}
        
        from datetime import datetime, timedelta
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_date = datetime.now(philippines_tz).date()
        
        for scheduled_class in scheduled_classes:
            class_id, day1, start1, end1, day2, start2, end2, subject_code, subject_name, class_section, classroom = scheduled_class
            
            class_name = f"{subject_code} - {subject_name}"
            
            for day, start_time, end_time in [(day1, start1, end1), (day2, start2, end2)]:
                if not day:
                    continue
                
                weekday_map = {
                    'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
                    'Friday': 4, 'Saturday': 5, 'Sunday': 6
                }
                
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
                    
                    found_record = None
                    for key, record in attendance_map.items():
                        if record['class_id'] == class_id:
                            if record['timein'] and record['timein'].date() == check_date:
                                found_record = record
                                break
                            elif not record['timein'] and record['status'] == 'Absent':
                                found_record = record
                                break
                    
                    if found_record:
                        status = found_record['status']
                        timein = found_record['timein']
                        timeout = found_record['timeout']
                        record_class_section = found_record['class_section']
                        record_classroom = found_record['classroom']
                        
                        time_in_str = timein.strftime('%H:%M') if timein else '—'
                        time_out_str = timeout.strftime('%H:%M') if timeout else '—'
                    else:
                        status = 'Absent'
                        time_in_str = '—'
                        time_out_str = '—'
                        record_class_section = class_section
                        record_classroom = classroom
                    
                    log_entry = {
                        'date': check_date.strftime('%Y-%m-%d'),
                        'time_in': time_in_str,
                        'time_out': time_out_str,
                        'status': status.capitalize(),
                        'class_name': class_name,
                        'class_section': record_class_section or 'N/A',
                        'classroom': record_classroom or 'N/A'
                    }
                    attendance_logs.append(log_entry)
                    
                    class_entry = {
                        'class_name': class_name,
                        'class_section': record_class_section or 'N/A',
                        'classroom': record_classroom or 'N/A',
                        'date': check_date.strftime('%Y-%m-%d'),
                        'time_in': time_in_str,
                        'status': status.capitalize()
                    }
                    class_attendance.append(class_entry)
                    
                    if status.lower() == 'present':
                        status_counts['present'] += 1
                    elif status.lower() == 'late':
                        status_counts['late'] += 1
                    elif status.lower() == 'absent':
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
        
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
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
        conn.close()
        
        return {'success': True, 'message': f'Simulated {status} attendance record created'}
        
    except Exception as e:
        print(f"Error simulating RFID: {e}")
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/semesters')
@require_auth([20001, 20002, 20003])
def api_faculty_semesters():
    """API endpoint to get available semesters"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
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
        
        semester_options = []
        current_semester_id = None
        
        for sem in semesters:
            acadcalendar_id, semester, acadyear, start_date, end_date = sem
            
            display_text = f"{semester}, AY {acadyear}"
            
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
@require_auth([20001, 20002, 20003])
def api_faculty_attendance_by_semester(semester_id):
    """API endpoint to get faculty attendance data for specific semester"""
    try:
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
        cursor.execute("""
            SELECT acadcalendar_id, semester, acadyear, semesterstart, semesterend 
            FROM acadcalendar 
            WHERE acadcalendar_id = %s
        """, (semester_id,))
        academic_calendar = cursor.fetchone()
        
        if not academic_calendar:
            return {'success': False, 'error': 'Academic calendar not found'}
        
        acadcalendar_id, semester_name, acad_year, semester_start, semester_end = academic_calendar
        
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
                sch.classsection,
                sch.classroom
            FROM schedule sch
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE sch.personnel_id = %s AND sch.acadcalendar_id = %s
        """, (personnel_id, acadcalendar_id))
        
        scheduled_classes = cursor.fetchall()
        
        cursor.execute("""
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
            WHERE a.personnel_id = %s AND sch.acadcalendar_id = %s
            ORDER BY COALESCE(a.timein, CURRENT_TIMESTAMP) DESC
        """, (personnel_id, acadcalendar_id))
        
        attendance_records = cursor.fetchall()
        
        attendance_map = {}
        for record in attendance_records:
            class_id, status, timein, timeout, subject_code, subject_name, class_section, classroom = record
            if timein:
                date_key = f"{class_id}_{timein.date()}"
            else:
                date_key = f"{class_id}_absent_{len(attendance_map)}"
            
            attendance_map[date_key] = {
                'class_id': class_id,
                'status': status,
                'timein': timein,
                'timeout': timeout,
                'subject_code': subject_code,
                'subject_name': subject_name,
                'class_section': class_section,
                'classroom': classroom
            }
        
        attendance_logs = []
        class_attendance = []
        status_counts = {'present': 0, 'late': 0, 'absent': 0}
        unique_sections = set()
        total_units = 0
        
        from datetime import datetime, timedelta
        import pytz
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_date = datetime.now(philippines_tz).date()
        
        for scheduled_class in scheduled_classes:
            class_id, day1, start1, end1, day2, start2, end2, subject_code, subject_name, units, class_section, classroom = scheduled_class
            
            class_name = f"{subject_code} - {subject_name}"
            
            section_key = f"{subject_code}_{class_section}"
            if section_key not in unique_sections:
                unique_sections.add(section_key)
                total_units += units or 3
            
            for day, start_time, end_time in [(day1, start1, end1), (day2, start2, end2)]:
                if not day:
                    continue
                
                weekday_map = {
                    'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
                    'Friday': 4, 'Saturday': 5, 'Sunday': 6
                }
                
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
                    
                    found_record = None
                    for key, record in attendance_map.items():
                        if record['class_id'] == class_id:
                            if record['timein'] and record['timein'].date() == check_date:
                                found_record = record
                                break
                            elif not record['timein'] and record['status'] == 'Absent':
                                found_record = record
                                break
                    
                    if found_record:
                        status = found_record['status']
                        timein = found_record['timein']
                        timeout = found_record['timeout']
                        record_class_section = found_record['class_section']
                        record_classroom = found_record['classroom']
                        
                        time_in_str = timein.strftime('%H:%M') if timein else '—'
                        time_out_str = timeout.strftime('%H:%M') if timeout else '—'
                    else:
                        status = 'Absent'
                        time_in_str = '—'
                        time_out_str = '—'
                        record_class_section = class_section
                        record_classroom = classroom
                    
                    log_entry = {
                        'date': check_date.strftime('%Y-%m-%d'),
                        'time_in': time_in_str,
                        'time_out': time_out_str,
                        'status': status.capitalize(),
                        'class_name': class_name,
                        'class_section': record_class_section or 'N/A',
                        'classroom': record_classroom or 'N/A'
                    }
                    attendance_logs.append(log_entry)
                    
                    class_entry = {
                        'class_name': class_name,
                        'class_section': record_class_section or 'N/A',
                        'classroom': record_classroom or 'N/A',
                        'date': check_date.strftime('%Y-%m-%d'),
                        'time_in': time_in_str,
                        'status': status.capitalize()
                    }
                    class_attendance.append(class_entry)
                    
                    if status.lower() == 'present':
                        status_counts['present'] += 1
                    elif status.lower() == 'late':
                        status_counts['late'] += 1
                    elif status.lower() == 'absent':
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

@app.route('/api/faculty/teaching-schedule/<int:semester_id>')
@require_auth([20001, 20002, 20003])
def api_faculty_teaching_schedule(semester_id):
    """API endpoint to get faculty teaching schedule as a timetable"""
    try:
        viewing_personnel_id = session.get('viewing_personnel_id')
        if viewing_personnel_id:
            personnel_id = viewing_personnel_id
        else:
            user_id = session['user_id']
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
            personnel_result = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if not personnel_result:
                return {'success': False, 'error': 'Personnel record not found'}
            personnel_id = personnel_result[0]
        
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT acadcalendar_id, semester, acadyear
            FROM acadcalendar 
            WHERE acadcalendar_id = %s
        """, (semester_id,))
        academic_calendar = cursor.fetchone()
        
        if not academic_calendar:
            return {'success': False, 'error': 'Academic calendar not found'}
        
        acadcalendar_id, semester_name, acad_year = academic_calendar
        
        cursor.execute("""
            SELECT 
                sch.classday_1,
                sch.starttime_1,
                sch.endtime_1,
                sch.classday_2,
                sch.starttime_2,
                sch.endtime_2,
                sub.subjectcode,
                sch.classroom,
                sch.classsection
            FROM schedule sch
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE sch.personnel_id = %s AND sch.acadcalendar_id = %s
        """, (personnel_id, acadcalendar_id))
        
        scheduled_classes = cursor.fetchall()
        
        def format_time_12hr(time_val):
            if not time_val:
                return None
            
            if hasattr(time_val, 'tzinfo') and time_val.tzinfo is not None:
                ph_tz = pytz.timezone('Asia/Manila')
                time_val = time_val.astimezone(ph_tz)
                time_str = time_val.strftime('%H:%M')
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
            (day1, start1, end1, day2, start2, end2, 
             subject_code, classroom, section) = scheduled_class
            
            print(f"DEBUG: Processing class - Day1: {day1}, Start1: {start1}, End1: {end1}, Day2: {day2}, Start2: {start2}, End2: {end2}")
            
            if day1 and start1 and end1:
                time_slot = get_time_slot(start1, end1)
                if time_slot and day1 in timetable:
                    timetable[day1][time_slot] = {
                        'subject_code': subject_code,
                        'classroom': classroom or 'TBA',
                        'section': section or 'N/A'
                    }
                    print(f"DEBUG: Added to timetable - {day1} at {time_slot}: {subject_code}")
            
            if day2 and start2 and end2:
                time_slot = get_time_slot(start2, end2)
                if time_slot and day2 in timetable:
                    timetable[day2][time_slot] = {
                        'subject_code': subject_code,
                        'classroom': classroom or 'TBA',
                        'section': section or 'N/A'
                    }
                    print(f"DEBUG: Added to timetable - {day2} at {time_slot}: {subject_code}")
        
        semester_info = {
            'id': acadcalendar_id,
            'name': semester_name,
            'year': acad_year,
            'display': f"{semester_name}, {acad_year}"
        }
        
        cursor.close()
        conn.close()
        
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
    """API endpoint to get faculty dashboard data"""
    try:
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
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

        attendance_rate = calculate_attendance_rate(cursor, personnel_id, acadcalendar_id, semester_start, semester_end)
        class_schedule = get_weekly_class_schedule(cursor, personnel_id, acadcalendar_id)
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
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_date = datetime.now(philippines_tz).date()

        cursor.execute("""
            SELECT 
                sch.class_id,
                sch.classday_1,
                sch.classday_2
            FROM schedule sch
            WHERE sch.personnel_id = %s AND sch.acadcalendar_id = %s
        """, (personnel_id, acadcalendar_id))
        
        scheduled_classes = cursor.fetchall()

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
        
        attendance_map = {}
        for record in attendance_records:
            class_id, status, timein = record
            if timein:
                date_key = f"{class_id}_{timein.date()}"
                attendance_map[date_key] = status
        
        total_classes = 0
        present_late_count = 0
        
        weekday_map = {
            'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
            'Friday': 4, 'Saturday': 5, 'Sunday': 6
        }
        
        for scheduled_class in scheduled_classes:
            class_id, day1, day2 = scheduled_class
            
            for day in [day1, day2]:
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
        
        philippines_tz = pytz.timezone('Asia/Manila')
        current_datetime = datetime.now(philippines_tz)
        current_date = current_datetime.date()
        current_day = current_date.strftime('%A')
        
        def format_time_ampm(time_val):
            if not time_val:
                return 'N/A'
            
            if hasattr(time_val, 'strftime'):
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
        
        current_weekday = current_date.weekday() 
        days_until_saturday = (5 - current_weekday) % 7 
        saturday_date = current_date + timedelta(days=days_until_saturday)
        
        day_order = {'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3, 'Friday': 4, 'Saturday': 5, 'Sunday': 6}
        
        weekly_schedule = []
        
        for scheduled_class in scheduled_classes:
            (class_id, day1, start1, end1, day2, start2, end2, 
             classroom, section, subject_code, subject_name) = scheduled_class
            
            class_name = f"{subject_code} - {subject_name}"
            
            if day1 and start1 and end1:
                start1_str = format_time_ampm(start1)
                end1_str = format_time_ampm(end1)
                
                day1_num = day_order.get(day1, -1)
                is_this_week = current_weekday <= day1_num <= 5 
                
                weekly_schedule.append({
                    'class_name': class_name,
                    'subject_code': subject_code,
                    'subject_name': subject_name,
                    'section': section or 'N/A',
                    'day': day1,
                    'start_time': start1_str,
                    'end_time': end1_str,
                    'time_display': f"{start1_str}-{end1_str}",
                    'classroom': classroom or 'N/A',
                    'is_today': day1 == current_day,
                    'is_this_week': is_this_week
                })
            
            if day2 and start2 and end2:
                start2_str = format_time_ampm(start2)
                end2_str = format_time_ampm(end2)
                
                day2_num = day_order.get(day2, -1)
                is_this_week = current_weekday <= day2_num <= 5 
                
                weekly_schedule.append({
                    'class_name': class_name,
                    'subject_code': subject_code,
                    'subject_name': subject_name,
                    'section': section or 'N/A',
                    'day': day2,
                    'start_time': start2_str,
                    'end_time': end2_str,
                    'time_display': f"{start2_str}-{end2_str}",
                    'classroom': classroom or 'N/A',
                    'is_today': day2 == current_day,
                    'is_this_week': is_this_week
                })
        
        weekly_schedule.sort(key=lambda x: (day_order.get(x['day'], 8), x['start_time']))
        
        return weekly_schedule
        
    except Exception as e:
        print(f"Error getting weekly class schedule: {e}")
        import traceback
        traceback.print_exc()
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

@app.route('/api/faculty/profile')
@require_auth([20001, 20002, 20003, 20004])
def api_get_faculty_profile():
    """API endpoint to get faculty profile data - AUTO CREATES PROFILE IF NOT EXISTS"""
    try:
        viewing_personnel_id = session.get('viewing_personnel_id')
        
        if viewing_personnel_id:
            personnel_id = viewing_personnel_id
        else:
            user_id = session['user_id']
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
            personnel_result = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if not personnel_result:
                return {'success': False, 'error': 'Personnel record not found'}
            personnel_id = personnel_result[0]
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT phone FROM personnel WHERE personnel_id = %s", (personnel_id,))
        phone_result = cursor.fetchone()
        phone = phone_result[0] if phone_result else None
        
        cursor.execute("""
            SELECT bio, profilepic, 
                   licenses, degrees, certificates, publications, awards,
                   licensesname, degreesname, certificatesname, 
                   publicationsname, awardsname
            FROM profile 
            WHERE personnel_id = %s
        """, (personnel_id,))
        
        profile_result = cursor.fetchone()
        
        if not profile_result and not viewing_personnel_id:
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
                RETURNING bio, profilepic, 
                          licenses, degrees, certificates, publications, awards,
                          licensesname, degreesname, certificatesname,
                          publicationsname, awardsname
            """, (new_profile_id, personnel_id))
            conn.commit()
            
            profile_result = cursor.fetchone()
            print(f"New profile created with ID: {new_profile_id} for personnel_id: {personnel_id}")
        
        (bio, profilepic, licenses, degrees, certificates, publications, awards,
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
        conn.close()
        
        return {'success': True, 'profile': profile_data}
        
    except Exception as e:
        print(f"Error fetching profile data: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/faculty/profile/stats')
@require_auth([20001, 20002, 20003, 20004])
def api_get_profile_stats():
    """API endpoint to get profile statistics - works for faculty, dean, HR, and VP"""
    try:
        viewing_personnel_id = session.get('viewing_personnel_id')
        if viewing_personnel_id:
            personnel_id = viewing_personnel_id
        else:
            user_id = session['user_id']
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT personnel_id, hiredate FROM personnel WHERE user_id = %s", (user_id,))
            personnel_result = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if not personnel_result:
                return {'success': False, 'error': 'Personnel record not found'}
            personnel_id, hire_date = personnel_result
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if not viewing_personnel_id:
            cursor.execute("SELECT hiredate FROM personnel WHERE personnel_id = %s", (personnel_id,))
            hire_date_result = cursor.fetchone()
            hire_date = hire_date_result[0] if hire_date_result else None
        else:
            cursor.execute("SELECT hiredate FROM personnel WHERE personnel_id = %s", (personnel_id,))
            hire_date_result = cursor.fetchone()
            hire_date = hire_date_result[0] if hire_date_result else None
        
        years_of_service = 0
        if hire_date:
            from datetime import datetime
            today = datetime.now().date()
            years_of_service = today.year - hire_date.year
            if today.month < hire_date.month or (today.month == hire_date.month and today.day < hire_date.day):
                years_of_service -= 1
        
        cursor.execute("""
            SELECT 
                certificates, publications, awards
            FROM profile 
            WHERE personnel_id = %s
        """, (personnel_id,))
        
        profile_result = cursor.fetchone()
        
        certificates_count = 0
        publications_count = 0
        awards_count = 0
        
        if profile_result:
            certificates, publications, awards = profile_result
            
            if certificates:
                certificates_count = len(certificates)
            if publications:
                publications_count = len(publications)
            if awards:
                awards_count = len(awards)
        
        cursor.close()
        conn.close()
        
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
    """API endpoint to update personal information - AUTO CREATES PROFILE IF NOT EXISTS"""
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
        
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
        cursor.execute("""
            UPDATE personnel SET phone = %s WHERE personnel_id = %s
        """, (phone, personnel_id))
        
        cursor.execute("SELECT profile_id FROM profile WHERE personnel_id = %s", (personnel_id,))
        profile_exists = cursor.fetchone()
        
        if profile_exists:
            cursor.execute("""
                UPDATE profile SET bio = %s WHERE personnel_id = %s
            """, (bio, personnel_id))
        else:
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
                VALUES (%s, %s, %s, NULL, 
                        ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[], ARRAY[]::bytea[],
                        ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[],
                        'Regular', 'Full-Time Employee)
            """, (new_profile_id, personnel_id, bio))
            print(f"New profile created with ID: {new_profile_id} for personnel_id: {personnel_id}")
        
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
@require_auth([20001, 20002, 20003, 20004])
def api_update_documents():
    """API endpoint to update document uploads - AUTO CREATES PROFILE IF NOT EXISTS"""
    try:
        user_id = session['user_id']
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
        cursor.execute("SELECT profile_id FROM profile WHERE personnel_id = %s", (personnel_id,))
        profile_exists = cursor.fetchone()
        
        if not profile_exists:
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
            print(f"New profile created with ID: {new_profile_id} for personnel_id: {personnel_id}")
        
        if 'profilepic' in request.files:
            file = request.files['profilepic']
            if file and file.filename:
                profilepic_data = file.read()
                cursor.execute("""
                    UPDATE profile SET profilepic = %s WHERE personnel_id = %s
                """, (profilepic_data, personnel_id))
                conn.commit()
                print(f"Profile picture updated: {len(profilepic_data)} bytes, filename: {file.filename}")
        
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
            conn.close()
            return {'success': False, 'error': 'Current password is incorrect'}
        
        if current_password == new_password:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'New password cannot be the same as current password'}
        
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
@require_auth([20001, 20002, 20003, 20004])
def api_delete_document(doc_type, index):
    """API endpoint to delete a specific document from an array"""
    try:
        user_id = session['user_id']
        
        if doc_type not in ['licenses', 'degrees', 'certificates', 'publications', 'awards']:
            return {'success': False, 'error': 'Invalid document type'}
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT personnel_id FROM personnel WHERE user_id = %s", (user_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'Personnel record not found'}
        
        personnel_id = personnel_result[0]
        
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
            conn.close()
            return {'success': False, 'error': 'No documents found'}
        
        doc_array = list(doc_result[0])
        filenames = list(doc_result[1]) if doc_result[1] else []
        
        if index < 0 or index >= len(doc_array):
            cursor.close()
            conn.close()
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
        conn.close()
        
        print(f"Deleted {doc_type} document '{deleted_filename}' at index {index}")
        return {'success': True, 'message': 'Document deleted successfully'}
        
    except Exception as e:
        print(f"Error deleting document: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}
    
@app.route('/api/hr/faculty-attendance')
@require_auth([20003])
def api_hr_faculty_attendance():
    """API endpoint to get all faculty and dean attendance data for HR"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT acadcalendar_id, semesterstart, semesterend 
            FROM acadcalendar 
            WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
            ORDER BY semesterstart DESC
            LIMIT 1
        """)
        academic_calendar = cursor.fetchone()
        
        if not academic_calendar:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'No active academic calendar found'}
        
        acadcalendar_id, semester_start, semester_end = academic_calendar
        
        cursor.execute("""
            SELECT COUNT(*)
            FROM personnel p
            WHERE p.role_id IN (20001, 20002)
        """)
        total_faculty = cursor.fetchone()[0]
        
        cursor.execute("""
            SELECT 
                p.firstname,
                p.lastname,
                p.honorifics,
                a.attendancestatus,
                a.timein,
                a.timeout,
                sch.classroom
            FROM attendance a
            JOIN schedule sch ON a.class_id = sch.class_id
            JOIN personnel p ON a.personnel_id = p.personnel_id
            WHERE p.role_id IN (20001, 20002)
            AND sch.acadcalendar_id = %s
            ORDER BY a.timein DESC
        """, (acadcalendar_id,))
        
        attendance_records = cursor.fetchall()
        
        attendance_logs = []
        status_counts = {'present': 0, 'late': 0, 'absent': 0}
        
        philippines_tz = pytz.timezone('Asia/Manila')
        today = datetime.now(philippines_tz).date()
        
        present_today = 0
        late_today = 0
        absent_today = 0
        
        for record in attendance_records:
            firstname, lastname, honorifics, status, timein, timeout, classroom = record
            
            if honorifics:
                faculty_name = f"{firstname} {lastname}, {honorifics}"
            else:
                faculty_name = f"{firstname} {lastname}"

            if timein:
                if hasattr(timein, 'tzinfo') and timein.tzinfo is not None:
                    timein_local = timein.astimezone(philippines_tz)
                else:
                    timein_local = philippines_tz.localize(timein) if timein.tzinfo is None else timein
                
                date_str = timein_local.strftime('%Y-%m-%d')
                time_in_str = timein_local.strftime('%I:%M %p')
                
                if timein_local.date() == today:
                    status_lower = status.lower()
                    if status_lower == 'present':
                        present_today += 1
                    elif status_lower == 'late':
                        late_today += 1
                    elif status_lower == 'absent':
                        absent_today += 1
            else:
                date_str = today.strftime('%Y-%m-%d')
                time_in_str = '—'
                absent_today += 1
            
            if timeout:
                if hasattr(timeout, 'tzinfo') and timeout.tzinfo is not None:
                    timeout_local = timeout.astimezone(philippines_tz)
                else:
                    timeout_local = philippines_tz.localize(timeout) if timeout.tzinfo is None else timeout

                time_out_str = timeout_local.strftime('%I:%M %p')
            else:
                time_out_str = '—'
            
            log_entry = {
                'name': faculty_name,
                'date': date_str,
                'room': classroom or 'N/A',
                'time_in': time_in_str,
                'time_out': time_out_str,
                'status': status.capitalize()
            }
            attendance_logs.append(log_entry)
            
            status_lower = status.lower()
            if status_lower == 'present':
                status_counts['present'] += 1
            elif status_lower == 'late':
                status_counts['late'] += 1
            elif status_lower == 'absent':
                status_counts['absent'] += 1
        
        kpis = {
            'total_faculty': total_faculty,
            'present_today': present_today,
            'late_today': late_today,
            'absent_today': absent_today
        }
        
        cursor.close()
        conn.close()
        
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
    """API endpoint to get all faculty and dean list with teaching load - EXCLUDES HR"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT acadcalendar_id, semesterstart, semesterend 
            FROM acadcalendar 
            WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
            ORDER BY semesterstart DESC
            LIMIT 1
        """)
        academic_calendar = cursor.fetchone()
        
        if not academic_calendar:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'No active academic calendar found'}
        
        acadcalendar_id, semester_start, semester_end = academic_calendar
        
        cursor.execute("""
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
            LEFT JOIN schedule sch ON p.personnel_id = sch.personnel_id AND sch.acadcalendar_id = %s
            LEFT JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE p.role_id IN (20001, 20002)  -- ONLY faculty and dean
            GROUP BY p.personnel_id, p.firstname, p.lastname, p.honorifics, p.role_id, c.collegename
            ORDER BY p.lastname, p.firstname
        """, (acadcalendar_id,))
        
        faculty_records = cursor.fetchall()
        
        faculty_list = []
        for record in faculty_records:
            personnel_id, firstname, lastname, honorifics, role_id, collegename, total_units = record
            
            if honorifics:
                faculty_name = f"{firstname} {lastname}, {honorifics}"
            else:
                faculty_name = f"{firstname} {lastname}"
            
            faculty_list.append({
                'personnel_id': personnel_id,
                'name': faculty_name,
                'college': collegename or 'N/A',
                'teaching_load': int(total_units),
                'role_id': role_id
            })
        
        cursor.close()
        conn.close()
        
        return {
            'success': True,
            'faculty_list': faculty_list
        }
        
    except Exception as e:
        print(f"Error fetching faculty list: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/faculty-schedule/<int:personnel_id>')
@require_auth([20003])
def api_hr_faculty_schedule(personnel_id):
    """API endpoint to get faculty teaching schedule for HR view"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT acadcalendar_id, semester, acadyear
            FROM acadcalendar 
            WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
            ORDER BY semesterstart DESC
            LIMIT 1
        """)
        academic_calendar = cursor.fetchone()
        
        if not academic_calendar:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'No active academic calendar found'}
        
        acadcalendar_id, semester_name, acad_year = academic_calendar
        
        cursor.execute("""
            SELECT 
                sch.classday_1,
                sch.starttime_1,
                sch.endtime_1,
                sch.classday_2,
                sch.starttime_2,
                sch.endtime_2,
                sub.subjectcode,
                sch.classroom,
                sch.classsection
            FROM schedule sch
            JOIN subjects sub ON sch.subject_id = sub.subject_id
            WHERE sch.personnel_id = %s AND sch.acadcalendar_id = %s
        """, (personnel_id, acadcalendar_id))
        
        scheduled_classes = cursor.fetchall()
        
        def format_time_12hr(time_val):
            if not time_val:
                return None
            
            if hasattr(time_val, 'tzinfo') and time_val.tzinfo is not None:
                ph_tz = pytz.timezone('Asia/Manila')
                time_val = time_val.astimezone(ph_tz)
                time_str = time_val.strftime('%H:%M')
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
            (day1, start1, end1, day2, start2, end2, 
             subject_code, classroom, section) = scheduled_class
            
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
        
        cursor.close()
        conn.close()
        
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
    """API endpoint to get all employees data for HR directory"""
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
        
        employees_list = []
        for emp in employees:
            (personnel_id, firstname, lastname, honorifics, employee_no, 
             phone, collegename, rolename, position, employmentstatus, email) = emp
            
            if honorifics:
                full_name = f"{firstname} {lastname}, {honorifics}"
            else:
                full_name = f"{firstname} {lastname}"
            
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
        
        cursor.close()
        conn.close()
        
        return {
            'success': True,
            'employees': employees_list
        }
        
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
        conn.close()
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus = result
            
            if honorifics:
                full_name = f"{firstname} {lastname}, {honorifics}"
            else:
                full_name = f"{firstname} {lastname}"
            
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
        conn.close()
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus = result
            
            if honorifics:
                full_name = f"{firstname} {lastname}, {honorifics}"
            else:
                full_name = f"{firstname} {lastname}"
            
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
        conn.close()
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, rolename, email, position, employmentstatus = result
            
            if honorifics:
                full_name = f"{firstname} {lastname}, {honorifics}"
            else:
                full_name = f"{firstname} {lastname}"
            
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

# Modified API endpoints to support viewing other profiles
@app.route('/api/hr/employee/profile/<int:personnel_id>')
@require_auth([20003])
def api_hr_employee_profile(personnel_id):
    """API endpoint to get employee profile data for HR viewing"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT bio, profilepic, 
                   licenses, degrees, certificates, publications, awards,
                   licensesname, degreesname, certificatesname, 
                   publicationsname, awardsname
            FROM profile 
            WHERE personnel_id = %s
        """, (personnel_id,))
        
        profile_result = cursor.fetchone()
        
        import base64
        profile_data = {
            'bio': '',
            'profilepic': None,
            'licenses': [],
            'degrees': [],
            'certificates': [],
            'publications': [],
            'awards': [],
            'licenses_filename': [],
            'degrees_filename': [],
            'certificates_filename': [],
            'publications_filename': [],
            'awards_filename': []
        }
        
        if profile_result:
            (bio, profilepic, licenses, degrees, certificates, publications, awards,
             licenses_fn, degrees_fn, certificates_fn, publications_fn, awards_fn) = profile_result
            
            profile_data['bio'] = bio or ''
            
            if profilepic:
                profile_data['profilepic'] = base64.b64encode(bytes(profilepic)).decode('utf-8')
            
            for doc_type in ['licenses', 'degrees', 'certificates', 'publications', 'awards']:
                doc_array = locals()[doc_type]
                if doc_array and len(doc_array) > 0:
                    profile_data[doc_type] = [base64.b64encode(bytes(doc)).decode('utf-8') for doc in doc_array]
            
            profile_data['licenses_filename'] = licenses_fn or []
            profile_data['degrees_filename'] = degrees_fn or []
            profile_data['certificates_filename'] = certificates_fn or []
            profile_data['publications_filename'] = publications_fn or []
            profile_data['awards_filename'] = awards_fn or []
        
        cursor.close()
        conn.close()
        
        return {'success': True, 'profile': profile_data}
        
    except Exception as e:
        print(f"Error fetching employee profile data: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/employee/profile/stats/<int:personnel_id>')
@require_auth([20003])
def api_hr_employee_profile_stats(personnel_id):
    """API endpoint to get employee profile statistics for HR viewing"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT hiredate FROM personnel WHERE personnel_id = %s", (personnel_id,))
        personnel_result = cursor.fetchone()
        
        if not personnel_result:
            cursor.close()
            conn.close()
            return {'success': False, 'error': 'Personnel record not found'}
        
        hire_date = personnel_result[0]
        
        years_of_service = 0
        if hire_date:
            from datetime import datetime
            today = datetime.now().date()
            years_of_service = today.year - hire_date.year
            if today.month < hire_date.month or (today.month == hire_date.month and today.day < hire_date.day):
                years_of_service -= 1
        
        cursor.execute("""
            SELECT 
                certificates, publications, awards
            FROM profile 
            WHERE personnel_id = %s
        """, (personnel_id,))
        
        profile_result = cursor.fetchone()
        
        certificates_count = 0
        publications_count = 0
        awards_count = 0
        
        if profile_result:
            certificates, publications, awards = profile_result
            
            if certificates:
                certificates_count = len(certificates)
            if publications:
                publications_count = len(publications)
            if awards:
                awards_count = len(awards)
        
        cursor.close()
        conn.close()
        
        stats = {
            'years_of_service': years_of_service,
            'professional_certifications': certificates_count,
            'research_publications': publications_count,
            'awards_count': awards_count
        }
        
        return {'success': True, 'stats': stats}
        
    except Exception as e:
        print(f"Error fetching employee profile stats: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

@app.route('/api/hr/colleges-list')
@require_auth([20003])
def api_hr_colleges_list():
    """API endpoint to get all colleges for filter dropdown"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT college_id, collegename 
            FROM college 
            ORDER BY collegename
        """)
        
        colleges = cursor.fetchall()
        
        colleges_list = []
        for college in colleges:
            college_id, collegename = college
            colleges_list.append({
                'college_id': college_id,
                'collegename': collegename
            })
        
        cursor.close()
        conn.close()
        
        return {
            'success': True,
            'colleges': colleges_list
        }
        
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
                print(f"DEBUG: Current time being saved: {current_time}")
                cursor.execute("""
                    UPDATE users SET lastlogin = %s WHERE user_id = %s
                """, (current_time, user_id))
                
                conn.commit()
                cursor.close()
                conn.close()
                
                session['user_id'] = user_id
                session['email'] = user_email
                session['user_role'] = role_id
                session['user_type'] = ROLE_REDIRECTS[role_id][0]
                
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
        conn.close()
        return f"Database connected successfully! Version: {version[0]}"
    except Exception as e:
        return f"Database connection failed: {e}"

if __name__ == "__main__":
    app.run(debug=True)