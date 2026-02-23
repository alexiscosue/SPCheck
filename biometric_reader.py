import threading
import time
from datetime import datetime, timedelta
import pytz

# Prefixes the Arduino sends for biometric messages
_BIO_PREFIX = "Biometric ID:"
_BIO_STATUS_PREFIX = "BIOMETRIC:"


class BiometricReader:
    def __init__(self, db_pool, shared_serial):
        self.db_pool = db_pool
        self.shared_serial = shared_serial
        self.is_running = False
        self.port_name = None
        self.notification_callbacks = []
        self.manila_tz = pytz.timezone('Asia/Manila')

    def add_notification_callback(self, callback):
        self.notification_callbacks.append(callback)

    def _trigger_notification(self, notification_data):
        for callback in self.notification_callbacks:
            try:
                callback(notification_data)
            except Exception as e:
                print(f"Error triggering notification callback: {e}")

    def _truncate_timestamp(self, dt):
        truncated_microseconds = (dt.microsecond // 10000) * 10000
        return dt.replace(microsecond=truncated_microseconds)

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start_reading(self, port=None):
        """Start the biometric reader (opens shared serial port if needed)."""
        if self.is_running:
            return {"success": False, "error": "Biometric reader is already running"}

        result = self.shared_serial.start(port)
        if not result.get("success"):
            return result

        self.port_name = self.shared_serial.port_name
        self.shared_serial.register_handler(_BIO_PREFIX, self._handle_line)
        self.shared_serial.register_handler(_BIO_STATUS_PREFIX, self._handle_status_line)
        self.is_running = True

        print(f"Biometric reader started on {self.port_name}")
        return {"success": True, "message": f"Biometric reader started on {self.port_name}", "port": self.port_name}

    def stop_reading(self):
        """Stop the biometric reader (unregisters handlers; port stays open if RFID is still running)."""
        if not self.is_running:
            return {"success": False, "error": "Biometric reader is not running"}

        self.is_running = False
        self.shared_serial.unregister_handler(_BIO_PREFIX)
        self.shared_serial.unregister_handler(_BIO_STATUS_PREFIX)
        self.port_name = None

        print("Biometric reader stopped")
        return {"success": True, "message": "Biometric reader stopped"}

    def get_status(self):
        return {
            "is_running": self.is_running,
            "port": self.port_name
        }

    # ------------------------------------------------------------------
    # Message handling (called by SharedSerialPort read loop)
    # ------------------------------------------------------------------

    def _handle_line(self, line):
        """Called by SharedSerialPort when a line starts with 'Biometric ID:'."""
        if not self.is_running:
            return
        biometric_uid = line.replace(_BIO_PREFIX, "").strip()
        self._process_biometric(biometric_uid)

    def _handle_status_line(self, line):
        """Called by SharedSerialPort for 'BIOMETRIC:' status lines."""
        if not self.is_running:
            return
        print(f"📟 {line}")

    def _send_to_arduino(self, message):
        """Send a message back to the Arduino via the shared serial port."""
        self.shared_serial.write(f"{message}\n".encode('utf-8'))
        print(f"📤 Sent to Arduino: {message}")

    # ------------------------------------------------------------------
    # Business logic (unchanged from original)
    # ------------------------------------------------------------------

    def _process_biometric(self, biometric_uid):
        conn = None
        cursor = None
        try:
            conn = self.db_pool.get_connection()
            cursor = conn.cursor()

            current_time = self._truncate_timestamp(datetime.now(self.manila_tz))
            day_name = current_time.strftime('%A')

            if biometric_uid == "UNKNOWN":
                remarks = f"[Unknown] Unregistered fingerprint scanned on {day_name}"
                self._log_biometric_scan(cursor, None, current_time, None, remarks)
                conn.commit()
                notification_data = {
                    'biometric_uid': 'UNKNOWN',
                    'person_name': 'Unknown',
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                    'action': 'unknown_biometric',
                    'status': 'error',
                    'message': 'Unregistered fingerprint detected'
                }
                self._trigger_notification(notification_data)
                print("⚠️ Unknown fingerprint scanned")
                return

            cursor.execute("""
                SELECT b.biometric_id, b.personnel_id, p.firstname, p.lastname
                FROM biometric b
                LEFT JOIN personnel p ON b.personnel_id = p.personnel_id
                WHERE b.biometric_uid = %s
            """, (biometric_uid,))

            result = cursor.fetchone()

            if not result:
                remarks = f"[Unknown] Fingerprint ID {biometric_uid} not registered - scanned on {day_name}"
                self._log_biometric_scan(cursor, None, current_time, None, remarks)
                conn.commit()
                notification_data = {
                    'biometric_uid': biometric_uid,
                    'person_name': 'Unknown',
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                    'action': 'unknown_biometric',
                    'status': 'error',
                    'message': f'Fingerprint ID {biometric_uid} not registered in system'
                }
                self._trigger_notification(notification_data)
                print(f"⚠️ Unregistered biometric UID: {biometric_uid}")
                return

            biometric_id, personnel_id, firstname, lastname = result
            person_name = f"{firstname} {lastname}" if firstname and lastname else f"Personnel #{personnel_id}"

            status, remarks, is_buffer = self._determine_status(cursor, biometric_id, current_time, day_name)
            self._log_biometric_scan(cursor, biometric_id, current_time, status, remarks)

            if not is_buffer:
                cursor.execute("""
                    UPDATE biometric SET lastused = %s WHERE biometric_id = %s
                """, (current_time, biometric_id))

            conn.commit()

            if is_buffer:
                self._send_to_arduino(f"BUFFER:{person_name}")
                notification_data = {
                    'biometric_uid': biometric_uid,
                    'biometric_id': biometric_id,
                    'personnel_id': personnel_id,
                    'person_name': person_name,
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                    'action': 'buffer_period',
                    'status': 'buffer',
                    'message': f'{person_name} - Already scanned. Wait 15 minutes.'
                }
                self._trigger_notification(notification_data)
                print(f"⏳ {person_name}: {status} - {remarks}")
            else:
                self._send_to_arduino(f"LOGGED:{status}:{person_name}")
                notification_data = {
                    'biometric_uid': biometric_uid,
                    'biometric_id': biometric_id,
                    'personnel_id': personnel_id,
                    'person_name': person_name,
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
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
        today_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        cursor.execute("""
            SELECT status, taptime FROM biometriclogs
            WHERE biometric_id = %s AND taptime >= %s
            ORDER BY taptime DESC
            LIMIT 1
        """, (biometric_id, today_start))
        last_scan = cursor.fetchone()

        if not last_scan:
            status = "Entry"
            remarks = f"[Entry] First scan of the day on {day_name}"
            is_buffer = False
        else:
            last_status, last_taptime = last_scan
            buffer_end = last_taptime + timedelta(minutes=15)
            if current_time <= buffer_end:
                minutes_left = int((buffer_end - current_time).total_seconds() / 60)
                status = last_status
                remarks = f"[{last_status}] Within 15-min buffer. Wait {minutes_left} more minutes."
                is_buffer = True
            elif last_status == "Entry":
                status = "Exit"
                remarks = f"[Exit] Exit scan on {day_name}"
                is_buffer = False
            else:
                status = "Entry"
                remarks = f"[Entry] Re-entry scan on {day_name}"
                is_buffer = False

        if not is_buffer and day_name in ('Saturday', 'Sunday'):
            remarks = f"[{status}] Scanned on {day_name} (Weekend)"

        return status, remarks, is_buffer

    def _log_biometric_scan(self, cursor, biometric_id, taptime, status, remarks):
        try:
            cursor.execute("SELECT COALESCE(MAX(biometriclog_id), 150000) + 1 FROM biometriclogs")
            new_log_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO biometriclogs (biometriclog_id, biometric_id, taptime, status, remarks)
                VALUES (%s, %s, %s, %s, %s)
            """, (new_log_id, biometric_id, taptime, status, remarks))
            print(f"📋 Logged biometric scan: {status} - {remarks}")
        except Exception as e:
            print(f"⚠️ Failed to log biometric scan: {e}")
