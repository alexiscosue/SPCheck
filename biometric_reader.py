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

    def _get_tap_window(self, dt):
        """Return (session, window_type) based on tap time.

        Morning   time_in  window:  7:00 AM – 11:44 AM (420–704 mins)
        Morning   time_out window: 11:45 AM – 12:44 PM (705–764 mins)
        Afternoon time_in  window: 12:45 PM –  5:14 PM (765–1034 mins)
        Afternoon time_out window:  5:15 PM –  5:45 PM (1035–1065 mins)
        Saturday: no Afternoon sessions at all.

        Returns (session, window_type) where window_type is 'time_in', 'time_out',
        or (None, None) if outside all valid windows.
        """
        tap_mins = dt.hour * 60 + dt.minute
        is_saturday = dt.weekday() == 5

        if 420 <= tap_mins <= 704:
            return 'Morning', 'time_in'
        elif 705 <= tap_mins <= 764:
            return 'Morning', 'time_out'
        elif 765 <= tap_mins <= 1034:
            return (None, None) if is_saturday else ('Afternoon', 'time_in')
        elif 1035 <= tap_mins <= 1065:
            return (None, None) if is_saturday else ('Afternoon', 'time_out')
        return None, None

    def _process_biometric(self, biometric_uid):
        conn = None
        cursor = None
        try:
            conn = self.db_pool.get_connection()
            cursor = conn.cursor()

            current_time = self._truncate_timestamp(datetime.now(self.manila_tz))
            day_name = current_time.strftime('%A')
            session, window_type = self._get_tap_window(current_time)

            # ── Reject Sunday and Saturday Afternoon entirely ──────────────
            if day_name == 'Sunday':
                remarks = f"[Rejected] Scanned on Sunday — no campus attendance on Sundays"
                self._log_biometric_scan(cursor, None, current_time, 'outside_buffer', remarks)
                conn.commit()
                self._trigger_notification({
                    'biometric_uid': biometric_uid,
                    'person_name': 'Unknown',
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                    'action': 'outside_buffer',
                    'status': 'outside_buffer',
                    'session': None,
                    'message': 'No campus attendance recorded on Sundays'
                })
                self._send_to_arduino("OUTSIDE:Sunday")
                print(f"🚫 Scan rejected — Sunday, no campus attendance")
                return
            # ──────────────────────────────────────────────────────────────

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
                    'session': session,
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
                    'session': session,
                    'message': f'Fingerprint ID {biometric_uid} not registered in system'
                }
                self._trigger_notification(notification_data)
                print(f"⚠️ Unregistered biometric UID: {biometric_uid}")
                return

            biometric_id, personnel_id, firstname, lastname = result
            person_name = f"{lastname}, {firstname}" if firstname and lastname else f"Personnel #{personnel_id}"

            # ── Outside session window guard ───────────────────────────────
            if session is None:
                remarks = f"[Outside Window] Scanned outside session hours on {day_name} at {current_time.strftime('%H:%M')}"
                self._log_biometric_scan(cursor, biometric_id, current_time, 'outside_buffer', remarks)
                conn.commit()
                notification_data = {
                    'biometric_uid': biometric_uid,
                    'biometric_id': biometric_id,
                    'personnel_id': personnel_id,
                    'person_name': person_name,
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                    'action': 'outside_buffer',
                    'status': 'outside_buffer',
                    'session': None,
                    'message': f'{person_name} - Scanned outside session hours ({current_time.strftime("%H:%M")})'
                }
                self._trigger_notification(notification_data)
                self._send_to_arduino(f"OUTSIDE:{person_name}")
                print(f"⏰ {person_name}: Tapped outside session time windows at {current_time.strftime('%H:%M')}")
                return
            # ──────────────────────────────────────────────────────────────

            status, remarks, is_rejected, attendance_status = self._determine_status(
                cursor, personnel_id, session, window_type, current_time, day_name
            )

            # Log to biometriclogs: use 'buffer' for rejections so the sync ignores them
            log_status = status if not is_rejected else 'buffer'
            self._log_biometric_scan(cursor, biometric_id, current_time, log_status, remarks)

            if not is_rejected:
                cursor.execute("""
                    UPDATE biometric SET lastused = %s WHERE biometric_id = %s
                """, (current_time, biometric_id))

                # Write directly to campus_attendance for real-time display
                today = current_time.date()
                if status == 'Entry':
                    cursor.execute("""
                        UPDATE campus_attendance
                        SET time_in = %s, status = %s
                        WHERE personnel_id = %s AND attendance_date = %s AND session = %s
                    """, (current_time, attendance_status, personnel_id, today, session))
                elif status == 'Exit':
                    cursor.execute("""
                        UPDATE campus_attendance
                        SET time_out = %s
                        WHERE personnel_id = %s AND attendance_date = %s AND session = %s
                          AND time_in IS NOT NULL
                    """, (current_time, personnel_id, today, session))

            conn.commit()

            if is_rejected:
                # Distinguish "already timed in" (time_in window) from other rejections
                if window_type == 'time_in':
                    self._send_to_arduino(f"BUFFER:{person_name}")
                    action = 'already_timed_in'
                    msg = f'{person_name} - Already timed in for {session} session. Wait for the time-out window.'
                else:
                    self._send_to_arduino(f"OUTSIDE:{person_name}")
                    action = 'outside_buffer'
                    msg = f'{person_name} - {remarks}'
                notification_data = {
                    'biometric_uid': biometric_uid,
                    'biometric_id': biometric_id,
                    'personnel_id': personnel_id,
                    'person_name': person_name,
                    'tap_time': current_time.strftime('%A, %Y-%m-%d %H:%M:%S.%f')[:29],
                    'action': action,
                    'status': 'buffer',
                    'session': session,
                    'message': msg
                }
                self._trigger_notification(notification_data)
                print(f"⏳ {person_name}: {remarks}")
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
                    'session': session,
                    'attendance_status': attendance_status,
                    'class_section': session,
                    'classroom': attendance_status,
                    'message': f'{person_name} - {status} ({session})'
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

    def _determine_status(self, cursor, personnel_id, session, window_type, current_time, day_name):
        """Decide Entry / Exit / rejected based on which sub-window the tap falls in
        and whether campus_attendance already has a time_in for this session.

        Returns (status, remarks, is_rejected, attendance_status):
          - status          : 'Entry', 'Exit', or 'outside_buffer'
          - remarks         : human-readable log string
          - is_rejected     : True  → don't count as a valid scan
          - attendance_status: 'Present', 'Late', or None
        """
        today = current_time.date()
        tap_mins = current_time.hour * 60 + current_time.minute

        cursor.execute("""
            SELECT time_in FROM campus_attendance
            WHERE personnel_id = %s AND attendance_date = %s AND session = %s
        """, (personnel_id, today, session))
        row = cursor.fetchone()
        has_time_in = row is not None and row[0] is not None

        if window_type == 'time_in':
            if not has_time_in:
                # ≤ 8:14 AM (494 mins) = Present for Morning
                # ≤ 1:44 PM (824 mins) = Present for Afternoon
                late_threshold = 494 if session == 'Morning' else 824
                attendance_status = 'Present' if tap_mins <= late_threshold else 'Late'
                return (
                    "Entry",
                    f"[Entry] {attendance_status} – {session} time-in on {day_name}",
                    False,
                    attendance_status,
                )
            else:
                return (
                    "Entry",
                    f"[Already Timed In] {session} time-in already recorded on {day_name}. Wait for the time-out window.",
                    True,
                    None,
                )

        elif window_type == 'time_out':
            if has_time_in:
                return (
                    "Exit",
                    f"[Exit] {session} time-out on {day_name}",
                    False,
                    None,
                )
            else:
                return (
                    "outside_buffer",
                    f"[Rejected] No {session} time-in recorded on {day_name} — cannot time out",
                    True,
                    None,
                )

        return (
            "outside_buffer",
            f"[Outside Window] Scanned outside valid session windows on {day_name}",
            True,
            None,
        )

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
