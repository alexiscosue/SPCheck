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

# Helper function to get faculty info
def get_faculty_info(user_id):
    """Get faculty information from database"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # This is a placeholder query - adjust according to your actual database schema
        cursor.execute("""
            SELECT 
                COALESCE(first_name || ' ' || last_name, 'Prof. Santos') as full_name,
                COALESCE(department, 'IT Department') as department
            FROM users 
            WHERE user_id = %s
        """, (user_id,))
        
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if result:
            return {
                'faculty_name': result[0],
                'department': result[1]
            }
    except Exception as e:
        print(f"Error getting faculty info: {e}")
    
    # Default values if query fails or no data found
    return {
        'faculty_name': 'Prof. Santos',
        'department': 'IT Department'
    }

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
    return render_template('hrmd/hr-dashboard.html')

@app.route('/hr_employees')
@require_auth([20003])
def hr_employees():
    return render_template('hrmd/hr-employees.html')

@app.route('/hr_evaluations')
@require_auth([20003])
def hr_evaluations():
    return render_template('hrmd/hr-evaluations.html')

@app.route('/hr_attendance')
@require_auth([20003])
def hr_attendance():
    return render_template('hrmd/hr-attendance.html')

@app.route('/hr_promotions')
@require_auth([20003])
def hr_promotions():
    return render_template('hrmd/hr-promotions.html')

@app.route('/hr_settings')
@require_auth([20003])
def hr_settings():
    return render_template('hrmd/hr-settings.html')

# VP/President routes
@app.route('/vp_promotions')
@require_auth([20004])
def vp_promotions():
    return render_template('vp&pres/vp-promotion.html')

@app.route('/vp_profile')
@require_auth([20004])
def vp_profile():
    return render_template('vp&pres/vp-profile.html')

@app.route('/vp_settings')
@require_auth([20004])
def vp_settings():
    return render_template('vp&pres/vp-settings.html')

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