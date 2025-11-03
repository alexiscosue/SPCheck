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
        """Process detected RFID tag and record attendance - 15 min buffer"""
        conn = None
        try:
            conn = self.db_pool.get_connection()
            cursor = conn.cursor()
            
            philippines_tz = pytz.timezone('Asia/Manila')
            current_time = datetime.now(philippines_tz).replace(microsecond=0)
            
            cursor.execute("SELECT personnel_id FROM rfid WHERE rfid_uid = %s", (rfid_uid,))
            result = cursor.fetchone()
            
            # CASE 1: RFID UID not found in database
            if not result:
                print(f"⚠️ RFID UID {rfid_uid} not found in database")
                
                notification_data = {
                    'personnel_id': 0, 
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                    'action': 'unknown_rfid',
                    'status': 'error',
                    'rfid_uid': rfid_uid,
                    'subject_code': None,
                    'subject_name': None,
                    'class_section': None,
                    'classroom': None,
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
            print(f"✓ Personnel ID: {personnel_id}")
            
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
            
            # CASE 2: Personnel has RFID but no teaching load/class schedule
            if not schedules:
                notification_data = {
                    'personnel_id': personnel_id,
                    'person_name': person_name,
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                    'action': 'no_schedule',
                    'status': 'warning',
                    'subject_code': None,
                    'subject_name': None,
                    'class_section': None,
                    'classroom': None,
                    'message': f'{person_name} has no teaching load or class schedule for the current semester'
                }
                self._trigger_notification(notification_data)
                
                print(f"⚠️ No schedule for personnel {personnel_id} ({person_name})")
                self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, None, 'no_schedule',
                                  f"Personnel has RFID but no classes scheduled for current semester")
                
                conn.commit()
                cursor.close()
                self.db_pool.return_connection(conn)
                return
            
            current_day = current_time.strftime('%A')
            current_time_only = current_time.time()
            
            print(f"🕐 Current: {current_day} at {current_time_only}")
            
            matching_class = None
            
            for schedule in schedules:
                class_id, day1, start1, end1, day2, start2, end2, subject_code, subject_name, class_section, classroom = schedule
                
                if day1 == current_day and start1 and end1:
                    if isinstance(start1, str):
                        start_time = datetime.strptime(start1[:8], '%H:%M:%S').time()
                    else:
                        start_time = start1
                    
                    if isinstance(end1, str):
                        end_time = datetime.strptime(end1[:8], '%H:%M:%S').time()
                    else:
                        end_time = end1
                    
                    buffer_start = (datetime.combine(datetime.today(), start_time) - timedelta(minutes=15)).time()
                    buffer_end = (datetime.combine(datetime.today(), end_time) + timedelta(minutes=15)).time()
                    
                    if buffer_start <= current_time_only <= buffer_end:
                        matching_class = (class_id, start_time, subject_code, subject_name, class_section, classroom)
                        print(f"✓ Matched: {subject_code} - {subject_name}, Section: {class_section or 'N/A'}, Room: {classroom or 'N/A'} ({start_time} - {end_time})")
                        break
                
                if day2 == current_day and start2 and end2:
                    if isinstance(start2, str):
                        start_time = datetime.strptime(start2[:8], '%H:%M:%S').time()
                    else:
                        start_time = start2
                    
                    if isinstance(end2, str):
                        end_time = datetime.strptime(end2[:8], '%H:%M:%S').time()
                    else:
                        end_time = end2
                    
                    buffer_start = (datetime.combine(datetime.today(), start_time) - timedelta(minutes=15)).time()
                    buffer_end = (datetime.combine(datetime.today(), end_time) + timedelta(minutes=15)).time()
                    
                    if buffer_start <= current_time_only <= buffer_end:
                        matching_class = (class_id, start_time, subject_code, subject_name, class_section, classroom)
                        print(f"✓ Matched: {subject_code} - {subject_name}, Section: {class_section or 'N/A'}, Room: {classroom or 'N/A'} ({start_time} - {end_time})")
                        break
            
            # CASE 3: No class within 15-minute buffer
            if not matching_class:
                notification_data = {
                    'personnel_id': personnel_id,
                    'person_name': person_name,
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                    'action': 'tap',
                    'status': 'outside_buffer',
                    'subject_code': None,
                    'subject_name': None,
                    'class_section': None,
                    'classroom': None,
                    'message': f'{person_name} tapped outside class schedule - No class within 15 minutes'
                }
                self._trigger_notification(notification_data)
                
                print(f"⚠️ No matching class for {current_day} at {current_time_only}")
                self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, None, 'outside_buffer',
                                  f"Tapped on {current_day} at {current_time.strftime('%Y-%m-%d %H:%M:%S%z')}, no class within 15min buffer")
                
                conn.commit()
                cursor.close()
                self.db_pool.return_connection(conn)
                return
            
            class_id, class_start_time, subject_code, subject_name, class_section, classroom = matching_class
            
            cursor.execute("""
                SELECT attendance_id, timein, timeout, attendancestatus
                FROM attendance 
                WHERE personnel_id = %s AND class_id = %s 
                AND DATE(timein AT TIME ZONE 'Asia/Manila') = %s
            """, (personnel_id, class_id, current_time.date()))
            
            existing = cursor.fetchone()
            
            if existing:
                attendance_id, timein, timeout, status = existing
                
                if timeout is None:
                    notification_data = {
                        'personnel_id': personnel_id,
                        'person_name': person_name,
                        'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                        'action': 'timeout',
                        'status': status,
                        'subject_code': subject_code,
                        'subject_name': subject_name,
                        'class_section': class_section,
                        'classroom': classroom,
                        'message': f'Time-out recorded for {subject_code}'
                    }
                    self._trigger_notification(notification_data)
                    
                    cursor.execute("UPDATE attendance SET timeout = %s WHERE attendance_id = %s", (current_time, attendance_id))
                    self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, class_id, 'timeout_recorded',
                                      f"Time-out for {subject_code} - {subject_name}, Section: {class_section or 'N/A'}, Room: {classroom or 'N/A'} at {current_time.strftime('%H:%M')}")
                    
                    conn.commit()
                    print(f"✓ TIME-OUT: {subject_code} - {subject_name}, Section: {class_section or 'N/A'}, Room: {classroom or 'N/A'} at {current_time.strftime('%H:%M')}")
                else:
                    notification_data = {
                        'personnel_id': personnel_id,
                        'person_name': person_name,
                        'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                        'action': 'duplicate',
                        'status': status,
                        'subject_code': subject_code,
                        'subject_name': subject_name,
                        'class_section': class_section,
                        'classroom': classroom,
                        'message': f'Attendance already complete for {subject_code}'
                    }
                    self._trigger_notification(notification_data)
                    
                    print(f"⚠️ Attendance already complete for {subject_code} - {subject_name}, Section: {class_section or 'N/A'}, Room: {classroom or 'N/A'}")
                    self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, class_id, 'already_complete',
                                      f"Attendance already complete for {subject_code} - {subject_name}, Section: {class_section or 'N/A'}, Room: {classroom or 'N/A'} (In: {timein}, Out: {timeout})")
                    
                    conn.commit()
            else:
                current_dt = datetime.combine(datetime.today(), current_time_only)
                class_start_dt = datetime.combine(datetime.today(), class_start_time)
                minutes_diff = (current_dt - class_start_dt).total_seconds() / 60
                
                status = "Present" if minutes_diff <= 15 else "Late"
                timing = f"{int(abs(minutes_diff))} mins {'early' if minutes_diff < 0 else 'after start'}"
                
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
                    'message': f'Time-in recorded - {status} ({timing})'
                }
                self._trigger_notification(notification_data)
                
                cursor.execute("SELECT COALESCE(MAX(attendance_id), 70000) FROM attendance")
                new_id = cursor.fetchone()[0] + 1
                
                cursor.execute("""
                    INSERT INTO attendance (attendance_id, personnel_id, class_id, attendancestatus, timein, timeout)
                    VALUES (%s, %s, %s, %s, %s, NULL)
                """, (new_id, personnel_id, class_id, status, current_time))
                
                self._log_rfid_tap(cursor, rfid_uid, personnel_id, current_time, class_id, 'timein_recorded',
                                  f"Time-in for {subject_code} - {subject_name}, Section: {class_section or 'N/A'}, Room: {classroom or 'N/A'} - {status} ({timing})")
                
                conn.commit()
                print(f"✓ TIME-IN: {subject_code} - {subject_name}, Section: {class_section or 'N/A'}, Room: {classroom or 'N/A'} - {status} ({timing})")
            
            cursor.close()
            self.db_pool.return_connection(conn)
            
        except Exception as e:
            print(f"❌ Error processing RFID {rfid_uid}: {e}")
            import traceback
            traceback.print_exc()
            
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