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
    """Get personnel information from personnel and college tables"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Join personnel and college tables to get complete personnel info
        cursor.execute("""
            SELECT 
                p.firstname,
                p.lastname,
                p.honorifics,
                c.collegename,
                p.employee_no,
                p.employmentstatus,
                r.rolename
            FROM personnel p
            LEFT JOIN college c ON p.college_id = c.college_id
            LEFT JOIN roles r ON p.role_id = r.role_id
            WHERE p.user_id = %s
        """, (user_id,))
        
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if result:
            firstname, lastname, honorifics, collegename, employee_no, employment_status, rolename = result
            
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
                'employment_status': employment_status,
                'firstname': firstname,
                'lastname': lastname,
                'honorifics': honorifics,
                'role_name': rolename or 'Staff'
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
        'employment_status': 'Active',
        'firstname': 'Staff',
        'lastname': 'Member',
        'honorifics': None,
        'role_name': 'Staff'
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

# API Routes for Faculty Attendance
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
        
        # Get attendance logs with class information
        cursor.execute("""
            SELECT 
                a.attendance_id,
                a.attendancestatus,
                a.timein,
                a.timeout,
                s.subjectname,
                sub.subjectcode,
                sch.classday_1,
                sch.classday_2
            FROM attendance a
            LEFT JOIN schedule sch ON a.class_id = sch.class_id
            LEFT JOIN subjects sub ON sch.subject_id = sub.subject_id
            LEFT JOIN subjects s ON sch.subject_id = s.subject_id
            WHERE a.personnel_id = %s
            ORDER BY a.timein DESC
        """, (personnel_id,))
        
        attendance_records = cursor.fetchall()
        
        # Process attendance logs
        attendance_logs = []
        class_attendance = []
        status_counts = {'present': 0, 'late': 0, 'absent': 0}
        
        for record in attendance_records:
            attendance_id, status, timein, timeout, subject_name, subject_code, classday_1, classday_2 = record
            
            # Format dates and times
            date_str = timein.strftime('%Y-%m-%d') if timein else 'N/A'
            time_in_str = timein.strftime('%H:%M') if timein else '—'
            time_out_str = timeout.strftime('%H:%M') if timeout else '—'
            
            # Create log entry
            log_entry = {
                'date': date_str,
                'time_in': time_in_str,
                'time_out': time_out_str,
                'status': status.capitalize(),
                'class_name': f"{subject_code} - {subject_name}" if subject_code and subject_name else 'N/A'
            }
            attendance_logs.append(log_entry)
            
            # Create class attendance entry
            class_entry = {
                'class_name': f"{subject_code} - {subject_name}" if subject_code and subject_name else 'N/A',
                'date': date_str,
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
        
        # Calculate KPIs
        total_records = len(attendance_records)
        attendance_percent = round((status_counts['present'] + status_counts['late']) / total_records * 100, 1) if total_records > 0 else 0
        
        kpis = {
            'attendance_percent': f'{attendance_percent}%',
            'late_count': status_counts['late'],
            'absence_count': status_counts['absent'],
            'total_classes': total_records
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
    return render_template('faculty&dean/faculty-dashboard.html', 
                         email=session['email'],
                         **faculty_info)

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
