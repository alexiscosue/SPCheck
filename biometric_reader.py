import serial
import serial.tools.list_ports
import threading
import time
from datetime import datetime, timedelta
import pytz

class BiometricReader:
    def __init__(self, db_pool):
        self.db_pool = db_pool
        self.serial_port = None
        self.is_running = False
        self.reader_thread = None
        self.port_name = None
        self.notification_callbacks = []
        self.manila_tz = pytz.timezone('Asia/Manila')

    def add_notification_callback(self, callback):
        """Add a callback to be called when biometric is scanned"""
        self.notification_callbacks.append(callback)

    def _trigger_notification(self, notification_data):
        """Trigger all registered notification callbacks"""
        for callback in self.notification_callbacks:
            try:
                callback(notification_data)
            except Exception as e:
                print(f"Error triggering notification callback: {e}")

    def _truncate_timestamp(self, dt):
        """Truncate timestamp to 2 decimal places (centiseconds)"""
        # Round microseconds to nearest 10000 (keeps only 2 decimal places)
        truncated_microseconds = (dt.microsecond // 10000) * 10000
        return dt.replace(microsecond=truncated_microseconds)

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
        """Start the biometric reader"""
        if self.is_running:
            return {"success": False, "error": "Biometric reader is already running"}

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

            return {"success": True, "message": f"Biometric reader started on {port}", "port": port}

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
        """Stop the biometric reader"""
        if not self.is_running:
            return {"success": False, "error": "Biometric reader is not running"}

        self.is_running = False

        if self.reader_thread:
            self.reader_thread.join(timeout=5)

        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()

        self.serial_port = None
        self.port_name = None

        return {"success": True, "message": "Biometric reader stopped"}

    def get_status(self):
        """Get the current status of the biometric reader"""
        return {
            "is_running": self.is_running,
            "port": self.port_name
        }

    def _read_loop(self):
        """Main reading loop that runs in a separate thread"""
        print("🔬 Biometric reader loop started")

        while self.is_running:
            try:
                if self.serial_port and self.serial_port.in_waiting:
                    line = self.serial_port.readline().decode('utf-8').strip()

                    if line.startswith("Biometric ID:"):
                        biometric_uid = line.replace("Biometric ID:", "").strip()
                        self._process_biometric(biometric_uid)
                    elif line.startswith("BIOMETRIC:"):
                        # Handle status messages from Arduino
                        print(f"📟 {line}")

                time.sleep(0.05)

            except serial.SerialException as e:
                print(f"Serial error: {e}")
                self.is_running = False
                break
            except Exception as e:
                print(f"Error in read loop: {e}")
                time.sleep(0.1)

        print("🔬 Biometric reader loop stopped")

    def _process_biometric(self, biometric_uid):
        """Process a biometric scan"""
        conn = None
        cursor = None

        try:
            conn = self.db_pool.get_connection()
            cursor = conn.cursor()

            current_time = self._truncate_timestamp(datetime.now(self.manila_tz))
            day_name = current_time.strftime('%A')

            # Handle unknown fingerprint
            if biometric_uid == "UNKNOWN":
                remarks = f"[Unknown] Unregistered fingerprint scanned on {day_name}"
                self._log_biometric_scan(cursor, None, current_time, None, remarks)
                conn.commit()

                notification_data = {
                    'biometric_uid': 'UNKNOWN',
                    'person_name': 'Unknown',
                    'tap_time': current_time.isoformat(),
                    'action': 'unknown_biometric',
                    'status': 'error',
                    'message': 'Unregistered fingerprint detected'
                }
                self._trigger_notification(notification_data)
                print(f"⚠️ Unknown fingerprint scanned")
                return

            # Look up the biometric in the database
            cursor.execute("""
                SELECT b.biometric_id, b.personnel_id, p.firstname, p.lastname
                FROM biometric b
                LEFT JOIN personnel p ON b.personnel_id = p.personnel_id
                WHERE b.biometric_uid = %s
            """, (biometric_uid,))

            result = cursor.fetchone()

            if not result:
                # Biometric UID not registered in database
                remarks = f"[Unknown] Fingerprint ID {biometric_uid} not registered - scanned on {day_name}"
                self._log_biometric_scan(cursor, None, current_time, None, remarks)
                conn.commit()

                notification_data = {
                    'biometric_uid': biometric_uid,
                    'person_name': 'Unknown',
                    'tap_time': current_time.isoformat(),
                    'action': 'unknown_biometric',
                    'status': 'error',
                    'message': f'Fingerprint ID {biometric_uid} not registered in system'
                }
                self._trigger_notification(notification_data)
                print(f"⚠️ Unregistered biometric UID: {biometric_uid}")
                return

            biometric_id, personnel_id, firstname, lastname = result
            person_name = f"{firstname} {lastname}" if firstname and lastname else f"Personnel #{personnel_id}"

            # Determine entry/exit status based on last scan (with 15-min buffer check)
            status, remarks, is_buffer = self._determine_status(cursor, biometric_id, current_time, day_name)

                # Log the biometric scan (including buffer period attempts)
            self._log_biometric_scan(cursor, biometric_id, current_time, status, remarks)

            # Update lastused timestamp in biometric table (only for actual entry/exit)
            if not is_buffer:
                cursor.execute("""
                    UPDATE biometric SET lastused = %s WHERE biometric_id = %s
                """, (current_time, biometric_id))

            conn.commit()

            if is_buffer:
                # Buffer period - notify but don't count as entry/exit
                self._send_to_arduino(f"BUFFER:{person_name}")

                notification_data = {
                    'biometric_uid': biometric_uid,
                    'biometric_id': biometric_id,
                    'personnel_id': personnel_id,
                    'person_name': person_name,
                    'tap_time': current_time.isoformat(),
                    'action': 'buffer_period',
                    'status': 'buffer',
                    'message': f'{person_name} - Already scanned. Wait 15 minutes.'
                }
                self._trigger_notification(notification_data)
                print(f"⏳ {person_name}: {status} - {remarks}")
            else:
                # Send confirmation back to Arduino
                self._send_to_arduino(f"LOGGED:{status}:{person_name}")

                # Trigger notification
                notification_data = {
                    'biometric_uid': biometric_uid,
                    'biometric_id': biometric_id,
                    'personnel_id': personnel_id,
                    'person_name': person_name,
                    'tap_time': current_time.isoformat(),
                    'action': status.lower(),
                    'status': 'success',
                    'message': f'{person_name} - {status}'
                }
                self._trigger_notification(notification_data)

                print(f"✅ {person_name}: {status} - {remarks}")

        except Exception as e:
            print(f"❌ Error processing biometric: {e}")
            if conn:
                conn.rollback()
        finally:
            if cursor:
                cursor.close()
            if conn:
                self.db_pool.return_connection(conn)

    def _determine_status(self, cursor, biometric_id, current_time, day_name):
        """Determine if this scan is an entry or exit based on last scan.
        Returns (status, remarks, is_buffer) where is_buffer is True if within 15-min buffer."""

        # Get the last scan for this biometric today
        today_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)

        cursor.execute("""
            SELECT status, taptime FROM biometriclogs
            WHERE biometric_id = %s AND taptime >= %s
            ORDER BY taptime DESC
            LIMIT 1
        """, (biometric_id, today_start))

        last_scan = cursor.fetchone()

        if not last_scan:
            # First scan of the day - it's an entry
            status = "Entry"
            remarks = f"[Entry] First scan of the day on {day_name}"
            is_buffer = False
        else:
            last_status, last_taptime = last_scan

            # Check if within 15-minute buffer
            buffer_end = last_taptime + timedelta(minutes=15)
            if current_time <= buffer_end:
                # Within buffer - reject this scan
                minutes_left = int((buffer_end - current_time).total_seconds() / 60)
                status = last_status
                remarks = f"[{last_status}] Within 15-min buffer. Wait {minutes_left} more minutes."
                is_buffer = True
            elif last_status == "Entry":
                # Last scan was entry, so this is exit
                status = "Exit"
                remarks = f"[Exit] Exit scan on {day_name}"
                is_buffer = False
            else:
                # Last scan was exit, so this is entry
                status = "Entry"
                remarks = f"[Entry] Re-entry scan on {day_name}"
                is_buffer = False

        # Add special remarks for weekends (only if not buffer)
        if not is_buffer and day_name in ['Saturday', 'Sunday']:
            remarks = f"[{status}] Scanned on {day_name} (Weekend)"

        return status, remarks, is_buffer

    def _send_to_arduino(self, message):
        """Send a message back to Arduino"""
        try:
            if self.serial_port and self.serial_port.is_open:
                self.serial_port.write(f"{message}\n".encode('utf-8'))
                print(f"📤 Sent to Arduino: {message}")
        except Exception as e:
            print(f"⚠️ Failed to send to Arduino: {e}")

    def _log_biometric_scan(self, cursor, biometric_id, taptime, status, remarks):
        """Log the biometric scan to biometriclogs table"""
        try:
            # Generate new log ID (starting from 150001 as specified)
            cursor.execute("SELECT COALESCE(MAX(biometriclog_id), 150000) + 1 FROM biometriclogs")
            new_log_id = cursor.fetchone()[0]

            cursor.execute("""
                INSERT INTO biometriclogs (biometriclog_id, biometric_id, taptime, status, remarks)
                VALUES (%s, %s, %s, %s, %s)
            """, (new_log_id, biometric_id, taptime, status, remarks))

            print(f"📋 Logged biometric scan: {status} - {remarks}")
        except Exception as e:
            print(f"⚠️ Failed to log biometric scan: {e}")
