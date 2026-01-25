import serial
import serial.tools.list_ports
import threading
import time
from datetime import datetime, timedelta
import pytz

class RFIDReader:
    def __init__(self, db_pool):
        self.db_pool = db_pool
        self.serial_port = None
        self.is_running = False
        self.reader_thread = None
        self.port_name = None
        self.notification_callbacks = []
        
    def add_notification_callback(self, callback):
        """Add a callback to be called when RFID is tapped"""
        self.notification_callbacks.append(callback)
    
    def _trigger_notification(self, notification_data):
        """Trigger all registered notification callbacks"""
        for callback in self.notification_callbacks:
            try:
                callback(notification_data)
            except Exception as e:
                print(f"Error triggering notification callback: {e}")
        
    def find_arduino_port(self):
        """Automatically detect Arduino COM port"""
        ports = serial.tools.list_ports.comports()
        for port in ports:
            if 'Arduino' in port.description or 'CH340' in port.description or 'USB Serial' in port.description:
                return port.device
            if 'usbserial' in port.device.lower() or 'usbmodem' in port.device.lower():
                return port.device
            if 'USB' in port.hwid and any(x in port.hwid for x in ['VID:PID', 'FTDI', 'CP210', 'CH340']):
                return port.device
        return None
    
    def start_reading(self, port=None):
        """Start the RFID reader"""
        if self.is_running:
            return {"success": False, "error": "RFID reader is already running"}
        
        try:
            if port is None:
                port = self.find_arduino_port()
            
            if port is None:
                available_ports = [p.device for p in serial.tools.list_ports.comports()]
                return {
                    "success": False, 
                    "error": f"Arduino not found. Available ports: {', '.join(available_ports) if available_ports else 'None'}"
                }
            
            self.port_name = port
            
            try:
                self.serial_port = serial.Serial(port, 9600, timeout=1, exclusive=True)
            except serial.SerialException as e:
                if "Resource busy" in str(e) or "Permission denied" in str(e):
                    try:
                        temp = serial.Serial(port, 9600, timeout=0.5)
                        temp.close()
                        time.sleep(1)
                    except:
                        pass
                    self.serial_port = serial.Serial(port, 9600, timeout=1)
                else:
                    raise
            
            time.sleep(2)
            self.serial_port.reset_input_buffer()
            self.serial_port.reset_output_buffer()
            
            self.is_running = True
            self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.reader_thread.start()
            
            return {"success": True, "message": f"RFID reader started on {port}", "port": port}
            
        except serial.SerialException as e:
            error_msg = str(e)
            if "Resource busy" in error_msg:
                return {"success": False, "error": "Port is busy. Please close Arduino IDE Serial Monitor and try again."}
            elif "Permission denied" in error_msg:
                return {"success": False, "error": "Permission denied. On Mac/Linux, you may need to run: sudo chmod 666 " + port}
            else:
                return {"success": False, "error": f"Failed to open serial port: {error_msg}"}
        except Exception as e:
            return {"success": False, "error": f"Unexpected error: {str(e)}"}
    
    def stop_reading(self):
        """Stop the RFID reader"""
        if not self.is_running:
            return {"success": False, "error": "RFID reader is not running"}
        
        self.is_running = False
        
        if self.reader_thread:
            self.reader_thread.join(timeout=2)
        
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
        
        return {"success": True, "message": "RFID reader stopped"}
    
    def _read_loop(self):
        """Main reading loop - runs in separate thread"""
        print(f"RFID Reader started on {self.port_name}")
        
        while self.is_running:
            try:
                if self.serial_port and self.serial_port.in_waiting:
                    line = self.serial_port.readline().decode('utf-8').strip()
                    
                    if line.startswith("RFID Tag UID:"):
                        rfid_uid = line.replace("RFID Tag UID:", "").strip()
                        print(f"RFID Detected: {rfid_uid}")
                        self._process_rfid(rfid_uid)
                
                time.sleep(0.1)
                
            except Exception as e:
                print(f"Error in RFID read loop: {e}")
                time.sleep(1)
        
        print("RFID Reader stopped")
    
    def _log_rfid_tap(self, cursor, rfid_uid, personnel_id, taptime, matched_class_id, status, remarks):
        """Log every RFID tap to rfidlogs table"""
        try:
            cursor.execute("SELECT COALESCE(MAX(log_id), 110000) + 1 FROM rfidlogs")
            new_log_id = cursor.fetchone()[0]
            
            cursor.execute("""
                INSERT INTO rfidlogs (log_id, rfid_uid, personnel_id, taptime, matched_class_id, status, remarks)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (new_log_id, rfid_uid, personnel_id, taptime, matched_class_id, status, remarks))
            
            print(f"📋 Logged tap: {status} - {remarks}")
        except Exception as e:
            print(f"⚠️ Failed to log RFID tap: {e}")
    
    def _process_rfid(self, rfid_uid):
        """Process detected RFID tag and record attendance with specific time windows"""
        conn = None
        try:
            conn = self.db_pool.get_connection()
            cursor = conn.cursor()
            
            philippines_tz = pytz.timezone('Asia/Manila')
            current_time = datetime.now(philippines_tz).replace(microsecond=0)
            current_time_only = current_time.time()
            current_date = current_time.date()
            current_day = current_time.strftime('%A')
            
            print(f"Processing RFID: {rfid_uid} on {current_day} at {current_time_only}")
            
            cursor.execute("SELECT personnel_id FROM rfid WHERE rfid_uid = %s", (rfid_uid,))
            result = cursor.fetchone()
            
            # CASE 1: RFID UID not found in database
            if not result:
                print(f"RFID UID {rfid_uid} not found in database")
                notification_data = {
                    'personnel_id': 0, 
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                    'action': 'unknown_rfid',
                    'status': 'error',
                    'rfid_uid': rfid_uid,
                    'message': f'Unknown RFID card (UID: {rfid_uid}) - Not registered in system'
                }
                self._trigger_notification(notification_data)
                self._log_rfid_tap(cursor, rfid_uid, None, current_time, None, 'unknown_rfid', 
                                f"RFID UID not registered in system")
                conn.commit()
                cursor.close()
                self.db_pool.return_connection(conn)
                return
            
            personnel_id = result[0]
            print(f"Personnel ID: {personnel_id}")
            
            cursor.execute("""
                SELECT firstname, lastname, honorifics
                FROM personnel
                WHERE personnel_id = %s
            """, (personnel_id,))
            person_result = cursor.fetchone()
            
            if person_result:
                firstname, lastname, honorifics = person_result
                person_name = f"{firstname} {lastname}, {honorifics}" if honorifics else f"{firstname} {lastname}"
            else:
                person_name = f"Personnel ID {personnel_id}"
            
            cursor.execute("UPDATE rfid SET lastused = %s WHERE rfid_uid = %s", (current_time, rfid_uid))
            
            cursor.execute("""
                WITH current_calendar AS (
                    SELECT acadcalendar_id 
                    FROM acadcalendar 
                    WHERE CURRENT_DATE BETWEEN semesterstart AND semesterend
                    ORDER BY semesterstart DESC LIMIT 1
                )
                SELECT 
                    sch.class_id, sch.classday_1, sch.starttime_1, sch.endtime_1,
                    sch.classday_2, sch.starttime_2, sch.endtime_2,
                    sub.subjectcode, sub.subjectname, sch.classsection, sch.classroom
                FROM schedule sch
                JOIN subjects sub ON sch.subject_id = sub.subject_id
                CROSS JOIN current_calendar cc
                WHERE sch.personnel_id = %s AND sch.acadcalendar_id = cc.acadcalendar_id
            """, (personnel_id,))
            
            schedules = cursor.fetchall()
            
            # CASE 2: No teaching schedule
            if not schedules:
                notification_data = {
                    'personnel_id': personnel_id,
                    'person_name': person_name,
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                    'action': 'no_schedule',
                    'status': 'warning',
                    'message': f'{person_name} has no teaching load for current semester'
                }
                self._trigger_notification(notification_data)
                self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, None, 'no_schedule',
                                f"No teaching schedule")
                
                cursor.execute("""
                    INSERT INTO auditlogs (personnel_id, action, details, created_at)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                """, (personnel_id, "RFID tap - No schedule", 
                    f"RFID tap but no teaching schedule found\nTime: {current_time.strftime('%H:%M:%S')}\nRFID UID: {rfid_uid}"))
                
                conn.commit()
                cursor.close()
                self.db_pool.return_connection(conn)
                return
            
            print(f"Found {len(schedules)} schedule(s) for {person_name}")
            
            matching_class = None
            time_window_type = None
            
            for schedule in schedules:
                class_id, day1, start1, end1, day2, start2, end2, subject_code, subject_name, class_section, classroom = schedule
                
                print(f"  Checking: {subject_code} - Day1: {day1} {start1}-{end1}, Day2: {day2} {start2}-{end2}")
                
                for day_idx, (day, start, end) in enumerate([(day1, start1, end1), (day2, start2, end2)], 1):
                    if not day or not start or not end:
                        continue
                        
                    if day != current_day:
                        continue
                    
                    if isinstance(start, str):
                        start_time = datetime.strptime(start[:8], '%H:%M:%S').time()
                    else:
                        start_time = start
                    
                    if isinstance(end, str):
                        end_time = datetime.strptime(end[:8], '%H:%M:%S').time()
                    else:
                        end_time = end
                    
                    print(f"    Day{day_idx}: {start_time} - {end_time}, Current: {current_time_only}")
                    
                    timein_window_start = (datetime.combine(current_date, start_time) - timedelta(minutes=15)).time()
                    timein_window_end = (datetime.combine(current_date, end_time) - timedelta(minutes=15)).time()
                    timeout_window_start = (datetime.combine(current_date, end_time) - timedelta(minutes=15)).time()
                    timeout_window_end = (datetime.combine(current_date, end_time) + timedelta(minutes=15)).time()
                    
                    print(f"    Time-in window: {timein_window_start} to {timein_window_end}")
                    print(f"    Time-out window: {timeout_window_start} to {timeout_window_end}")
                    
                    if timein_window_start <= current_time_only <= timein_window_end:
                        matching_class = (class_id, start_time, end_time, subject_code, subject_name, class_section, classroom, 'timein_window')
                        print(f"    MATCHED Time-in window!")
                        break
                    
                    # Check time-out window  
                    elif timeout_window_start <= current_time_only <= timeout_window_end:
                        matching_class = (class_id, start_time, end_time, subject_code, subject_name, class_section, classroom, 'timeout_window')
                        print(f"    MATCHED Time-out window!")
                        break
                
                if matching_class:
                    break
            
            # CASE 3: No matching time window
            if not matching_class:
                notification_data = {
                    'personnel_id': personnel_id,
                    'person_name': person_name,
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                    'action': 'outside_buffer',
                    'status': 'outside_buffer',
                    'message': f'Tapped outside valid class time windows'
                }
                self._trigger_notification(notification_data)
                self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, None, 'outside_buffer',
                                f"Outside time windows on {current_day} at {current_time_only}")
                
                # Enhanced audit logging for outside buffer
                cursor.execute("""
                    INSERT INTO auditlogs (personnel_id, action, details, created_at)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                """, (personnel_id, "RFID tap - Outside buffer", 
                    f"RFID tap outside valid time windows\nDay: {current_day}\nTime: {current_time.strftime('%H:%M:%S')}\nRFID UID: {rfid_uid}"))
                
                conn.commit()
                cursor.close()
                self.db_pool.return_connection(conn)
                return
            
            class_id, class_start_time, class_end_time, subject_code, subject_name, class_section, classroom, window_type = matching_class
            
            print(f"Processing: {subject_code} in {window_type}")
            
            # Check for existing attendance for THIS SPECIFIC CLASS AND DATE
            cursor.execute("""
                SELECT attendance_id, timein, timeout, attendancestatus
                FROM attendance 
                WHERE personnel_id = %s AND class_id = %s 
                AND DATE(timein AT TIME ZONE 'Asia/Manila') = %s
            """, (personnel_id, class_id, current_date))
            
            existing_record = cursor.fetchone()
            
            print(f"Existing record: {existing_record}")
            
            # TIME-IN WINDOW PROCESSING
            if window_type == 'timein_window':
                if not existing_record:
                    # NO EXISTING RECORD - CREATE NEW TIME-IN
                    current_dt = datetime.combine(current_date, current_time_only)
                    class_start_dt = datetime.combine(current_date, class_start_time)
                    late_threshold = class_start_dt + timedelta(minutes=15)
                    
                    # Determine status
                    if current_dt <= late_threshold:
                        status = "Present"
                        timing_msg = "on time"
                    else:
                        status = "Late"
                        minutes_late = int((current_dt - late_threshold).total_seconds() / 60)
                        timing_msg = f"{minutes_late} minutes late"
                    
                    # Create new attendance record
                    cursor.execute("""
                        INSERT INTO attendance (personnel_id, class_id, attendancestatus, timein, timeout)
                        VALUES (%s, %s, %s, %s, NULL)
                    """, (personnel_id, class_id, status, current_time))

                    try:
                        cursor.execute("SELECT acadcalendar_id FROM schedule WHERE class_id = %s", (class_id,))
                        acadcal_result = cursor.fetchone()
                        if acadcal_result:
                            acadcalendar_id = acadcal_result[0]
                            
                            # Calculate stats
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
                                
                                # Check if report exists
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
                                
                                print(f"📊 Updated attendance report: Rate={attendance_rate:.2f}%")
                                
                    except Exception as e:
                        print(f"⚠️ Could not update attendance report: {e}")
                    
                    notification_data = {
                        'personnel_id': personnel_id,
                        'person_name': person_name,
                        'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                        'action': 'timein',
                        'status': status,
                        'subject_code': subject_code,
                        'subject_name': subject_name,
                        'class_section': class_section,
                        'classroom': classroom,
                        'message': f'Time-in recorded - {status} ({timing_msg})'
                    }
                    self._trigger_notification(notification_data)
                    
                    self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, class_id, 'timein_recorded',
                                    f"Time-in for {subject_code} - {status}")
                    
                    # Enhanced audit logging for time-in
                    cursor.execute("""
                        INSERT INTO auditlogs (personnel_id, action, details, created_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                    """, (personnel_id, "RFID time-in recorded", 
                        f"Time-in for {subject_code} - {class_section}\nStatus: {status}\nTime: {current_time.strftime('%H:%M:%S')}\nClassroom: {classroom}\nTiming: {timing_msg}\nRFID UID: {rfid_uid}"))
                    
                    print(f"NEW TIME-IN: {subject_code} - {status}")
                    
                else:
                    # EXISTING RECORD FOUND - handle based on current state
                    attendance_id, timein, timeout, existing_status = existing_record
                    
                    if timeout is None:
                        # Has time-in but no time-out yet
                        timein_dt = timein.astimezone(philippines_tz)
                        buffer_end = timein_dt + timedelta(minutes=15)
                        
                        if current_time <= buffer_end:
                            # Within 15-minute buffer - prevent double time-in
                            notification_data = {
                                'personnel_id': personnel_id,
                                'person_name': person_name,
                                'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                                'action': 'buffer_period',
                                'status': existing_status,
                                'subject_code': subject_code,
                                'subject_name': subject_name,
                                'class_section': class_section,
                                'classroom': classroom,
                                'message': f'Already recorded time-in. Wait 15 minutes for time-out.'
                            }
                            self._trigger_notification(notification_data)
                            self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, class_id, 'buffer_period',
                                            f"Within 15-min buffer after time-in")
                            
                            # Enhanced audit logging for buffer period
                            cursor.execute("""
                                INSERT INTO auditlogs (personnel_id, action, details, created_at)
                                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                            """, (personnel_id, "RFID tap - Buffer period", 
                                f"Attempted time-in during buffer period\nSubject: {subject_code}\nExisting status: {existing_status}\nTime: {current_time.strftime('%H:%M:%S')}\nRFID UID: {rfid_uid}"))
                            
                            print(f"BUFFER PERIOD: Wait for time-out")
                        else:
                            # Outside buffer but trying to time-in again
                            notification_data = {
                                'personnel_id': personnel_id,
                                'person_name': person_name,
                                'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                                'action': 'duplicate_timein',
                                'status': existing_status,
                                'subject_code': subject_code,
                                'subject_name': subject_name,
                                'class_section': class_section,
                                'classroom': classroom,
                                'message': f'Already recorded time-in for this class'
                            }
                            self._trigger_notification(notification_data)
                            self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, class_id, 'duplicate_timein',
                                            f"Already has time-in record")
                            
                            # Enhanced audit logging for duplicate time-in
                            cursor.execute("""
                                INSERT INTO auditlogs (personnel_id, action, details, created_at)
                                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                            """, (personnel_id, "RFID tap - Duplicate time-in", 
                                f"Attempted duplicate time-in\nSubject: {subject_code}\nExisting status: {existing_status}\nTime: {current_time.strftime('%H:%M:%S')}\nRFID UID: {rfid_uid}"))
                            
                            print(f"DUPLICATE TIME-IN: Already recorded")
                    else:
                        # Already completed both time-in and time-out
                        notification_data = {
                            'personnel_id': personnel_id,
                            'person_name': person_name,
                            'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                            'action': 'already_complete',
                            'status': existing_status,
                            'subject_code': subject_code,
                            'subject_name': subject_name,
                            'class_section': class_section,
                            'classroom': classroom,
                            'message': f'Attendance already complete for {subject_code}'
                        }
                        self._trigger_notification(notification_data)
                        self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, class_id, 'already_complete',
                                        f"Attendance complete")
                        
                        # Enhanced audit logging for already complete
                        cursor.execute("""
                            INSERT INTO auditlogs (personnel_id, action, details, created_at)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        """, (personnel_id, "RFID tap - Already complete", 
                            f"Attempted time-in but attendance already complete\nSubject: {subject_code}\nStatus: {existing_status}\nTime: {current_time.strftime('%H:%M:%S')}\nRFID UID: {rfid_uid}"))
                        
                        print(f"ALREADY COMPLETE: Both time-in and time-out recorded")
            
            # TIME-OUT WINDOW PROCESSING  
            elif window_type == 'timeout_window':
                if not existing_record:
                    # No time-in record exists - cannot time-out
                    notification_data = {
                        'personnel_id': personnel_id,
                        'person_name': person_name,
                        'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                        'action': 'no_timein',
                        'status': 'error',
                        'subject_code': subject_code,
                        'subject_name': subject_name,
                        'class_section': class_section,
                        'classroom': classroom,
                        'message': f'Cannot time-out without time-in first'
                    }
                    self._trigger_notification(notification_data)
                    self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, class_id, 'no_timein_first',
                                    f"Attempted time-out without time-in")
                    
                    # Enhanced audit logging for no time-in
                    cursor.execute("""
                        INSERT INTO auditlogs (personnel_id, action, details, created_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                    """, (personnel_id, "RFID tap - No time-in", 
                        f"Attempted time-out without time-in\nSubject: {subject_code}\nTime: {current_time.strftime('%H:%M:%S')}\nRFID UID: {rfid_uid}"))
                    
                    print(f"NO TIME-IN: Cannot time-out without time-in")
                    
                else:
                    attendance_id, timein, timeout, existing_status = existing_record
                    
                    if timeout is None:
                        # Valid time-out - record it
                        cursor.execute("UPDATE attendance SET timeout = %s WHERE attendance_id = %s", 
                                    (current_time, attendance_id))
                        
                        try:
                            cursor.execute("SELECT acadcalendar_id FROM schedule WHERE class_id = %s", (class_id,))
                            acadcal_result = cursor.fetchone()
                            if acadcal_result:
                                acadcalendar_id = acadcal_result[0]
                                
                                # Calculate stats
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
                                    
                                    # Check if report exists
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
                                    
                                    print(f"📊 Updated attendance report: Rate={attendance_rate:.2f}%")
                                    
                        except Exception as e:
                            print(f"⚠️ Could not update attendance report: {e}")
                        
                        notification_data = {
                            'personnel_id': personnel_id,
                            'person_name': person_name,
                            'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                            'action': 'timeout',
                            'status': existing_status,
                            'subject_code': subject_code,
                            'subject_name': subject_name,
                            'class_section': class_section,
                            'classroom': classroom,
                            'message': f'Time-out recorded for {subject_code}'
                        }
                        self._trigger_notification(notification_data)
                        self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, class_id, 'timeout_recorded',
                                        f"Time-out recorded")
                        
                        # Enhanced audit logging for time-out
                        timein_str = timein.astimezone(philippines_tz).strftime('%H:%M:%S') if timein else "N/A"
                        timeout_str = current_time.strftime('%H:%M:%S')
                        
                        cursor.execute("""
                            INSERT INTO auditlogs (personnel_id, action, details, created_at)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        """, (personnel_id, "RFID time-out recorded", 
                            f"Time-out for {subject_code} - {class_section}\nStatus: {existing_status}\nTime-in: {timein_str}\nTime-out: {timeout_str}\nDuration: Calculated\nClassroom: {classroom}\nRFID UID: {rfid_uid}"))
                        
                        print(f"TIME-OUT: Recorded successfully")
                        
                    else:
                        # Already timed out
                        notification_data = {
                            'personnel_id': personnel_id,
                            'person_name': person_name,
                            'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                            'action': 'duplicate_timeout',
                            'status': existing_status,
                            'subject_code': subject_code,
                            'subject_name': subject_name,
                            'class_section': class_section,
                            'classroom': classroom,
                            'message': f'Already timed out for {subject_code}'
                        }
                        self._trigger_notification(notification_data)
                        self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, class_id, 'duplicate_timeout',
                                        f"Already timed out")
                        
                        # Enhanced audit logging for duplicate time-out
                        cursor.execute("""
                            INSERT INTO auditlogs (personnel_id, action, details, created_at)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        """, (personnel_id, "RFID tap - Duplicate time-out", 
                            f"Attempted duplicate time-out\nSubject: {subject_code}\nStatus: {existing_status}\nTime: {current_time.strftime('%H:%M:%S')}\nRFID UID: {rfid_uid}"))
                        
                        print(f"DUPLICATE TIME-OUT: Already recorded")
            
            conn.commit()
            cursor.close()
            self.db_pool.return_connection(conn)
            
        except Exception as e:
            print(f"Error processing RFID {rfid_uid}: {e}")
            if conn:
                try:
                    conn.rollback()
                    cursor.close()
                    self.db_pool.return_connection(conn)
                except:
                    pass
    
    def get_status(self):
        """Get current status of RFID reader"""
        return {
            "success": True,
            "is_running": self.is_running,
            "port": self.port_name if self.is_running else None
        }