#!/usr/bin/env python3
"""
Simple test script for biometric reader - no authentication required
Run this directly: python3 test_biometric.py

IMPORTANT: Close Arduino Serial Monitor before running this!
"""

import serial
import serial.tools.list_ports
import time
from datetime import datetime, timedelta
import pg8000
import os
import pytz
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Database connection
def get_db_connection():
    conn = pg8000.dbapi.connect(
        host=os.getenv('DB_HOST'),
        port=int(os.getenv('DB_PORT', 5432)),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        ssl_context=True
    )
    cursor = conn.cursor()
    cursor.execute("SET TIME ZONE 'Asia/Manila'")
    cursor.close()
    return conn

def find_arduino_port():
    """Find Arduino port"""
    ports = serial.tools.list_ports.comports()
    print("\nAvailable ports:")
    for port in ports:
        print(f"   - {port.device}: {port.description}")
        if 'usbserial' in port.device.lower() or 'usbmodem' in port.device.lower():
            return port.device
        if 'Arduino' in port.description or 'CH340' in port.description:
            return port.device
    return None

def main():
    manila_tz = pytz.timezone('Asia/Manila')

    print("=" * 50)
    print("BIOMETRIC TEST SCRIPT")
    print("=" * 50)

    # Find Arduino
    port = find_arduino_port()
    if not port:
        print("ERROR: Arduino not found! Make sure it's connected.")
        print("       Also close Arduino Serial Monitor if it's open.")
        return

    print(f"\nFound Arduino on {port}")

    # Test database connection
    print("\nTesting database connection...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Check biometric table
        cursor.execute("SELECT COUNT(*) FROM biometric")
        count = cursor.fetchone()[0]
        print(f"Database connected! Biometric table has {count} records.")

        if count == 0:
            print("\nWARNING: No records in biometric table!")
            print("You need to add records first. Example:")
            print("INSERT INTO biometric (biometric_id, biometric_uid, personnel_id)")
            print("VALUES (140001, '1', YOUR_PERSONNEL_ID);")
            cursor.close()
            conn.close()
            return
        else:
            cursor.execute("""
                SELECT b.biometric_uid, p.firstname, p.lastname
                FROM biometric b
                LEFT JOIN personnel p ON b.personnel_id = p.personnel_id
            """)
            print("\nRegistered biometrics:")
            for row in cursor.fetchall():
                name = f"{row[1]} {row[2]}" if row[1] else "No personnel linked"
                print(f"   - UID '{row[0]}': {name}")

        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Database error: {e}")
        return

    # Connect to Arduino
    print(f"\nConnecting to Arduino on {port}...")
    try:
        ser = serial.Serial(port, 9600, timeout=1)
        time.sleep(2)  # Wait for Arduino to reset
        ser.reset_input_buffer()
        print("Connected to Arduino!")
    except serial.SerialException as e:
        print(f"Cannot open serial port: {e}")
        print("Close Arduino Serial Monitor and try again!")
        return

    print("\n" + "=" * 50)
    print("SCAN YOUR FINGER NOW")
    print("Press Ctrl+C to stop")
    print("=" * 50 + "\n")

    try:
        while True:
            if ser.in_waiting:
                line = ser.readline().decode('utf-8').strip()
                print(f"Arduino: {line}")

                if line.startswith("Biometric ID:"):
                    biometric_uid = line.replace("Biometric ID:", "").strip()
                    print(f"\nProcessing fingerprint ID: {biometric_uid}")

                    # Process and log to database
                    try:
                        conn = get_db_connection()
                        cursor = conn.cursor()
                        current_time = datetime.now(manila_tz)
                        # Truncate timestamp to 2 decimal places
                        truncated_microseconds = (current_time.microsecond // 10000) * 10000
                        current_time = current_time.replace(microsecond=truncated_microseconds)
                        day_name = current_time.strftime('%A')

                        if biometric_uid == "UNKNOWN":
                            # Log unknown fingerprint
                            cursor.execute("SELECT COALESCE(MAX(biometriclog_id), 150000) + 1 FROM biometriclogs")
                            new_log_id = cursor.fetchone()[0]

                            cursor.execute("""
                                INSERT INTO biometriclogs (biometriclog_id, biometric_id, taptime, status, remarks)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (new_log_id, None, current_time, None, f"Unknown fingerprint on {day_name}"))
                            conn.commit()
                            print(f"Unknown fingerprint logged (log_id: {new_log_id})")
                        else:
                            # Look up biometric
                            cursor.execute("""
                                SELECT b.biometric_id, b.personnel_id, p.firstname, p.lastname
                                FROM biometric b
                                LEFT JOIN personnel p ON b.personnel_id = p.personnel_id
                                WHERE b.biometric_uid = %s
                            """, (biometric_uid,))

                            result = cursor.fetchone()

                            if not result:
                                print(f"ERROR: Fingerprint ID '{biometric_uid}' not in database!")
                                print(f"Add it with: INSERT INTO biometric (biometric_id, biometric_uid, personnel_id)")
                                print(f"             VALUES (140001, '{biometric_uid}', YOUR_PERSONNEL_ID);")
                            else:
                                biometric_id, personnel_id, firstname, lastname = result
                                person_name = f"{firstname} {lastname}" if firstname else f"Personnel #{personnel_id}"

                                # Determine entry/exit with 15-minute buffer check
                                today_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
                                cursor.execute("""
                                    SELECT status, taptime FROM biometriclogs
                                    WHERE biometric_id = %s AND taptime >= %s
                                    ORDER BY taptime DESC LIMIT 1
                                """, (biometric_id, today_start))

                                last_scan = cursor.fetchone()
                                is_buffer = False

                                if not last_scan:
                                    status = "Entry"
                                    remarks = f"First scan of the day on {day_name}"
                                else:
                                    last_status, last_taptime = last_scan
                                    buffer_end = last_taptime + timedelta(minutes=15)

                                    if current_time <= buffer_end:
                                        # Within 15-minute buffer
                                        minutes_left = int((buffer_end - current_time).total_seconds() / 60)
                                        is_buffer = True
                                        print(f"BUFFER: Already scanned. Wait {minutes_left} more minutes.")
                                        ser.write(f"BUFFER:{person_name}\n".encode('utf-8'))
                                        cursor.close()
                                        conn.close()
                                        print()
                                        continue
                                    elif last_status == "Entry":
                                        status = "Exit"
                                        remarks = f"Exit scan on {day_name}"
                                    else:
                                        status = "Entry"
                                        remarks = f"Re-entry scan on {day_name}"

                                # Log the scan
                                cursor.execute("SELECT COALESCE(MAX(biometriclog_id), 150000) + 1 FROM biometriclogs")
                                new_log_id = cursor.fetchone()[0]

                                cursor.execute("""
                                    INSERT INTO biometriclogs (biometriclog_id, biometric_id, taptime, status, remarks)
                                    VALUES (%s, %s, %s, %s, %s)
                                """, (new_log_id, biometric_id, current_time, status, remarks))

                                # Update lastused
                                cursor.execute("UPDATE biometric SET lastused = %s WHERE biometric_id = %s",
                                             (current_time, biometric_id))

                                conn.commit()

                                print(f"SUCCESS! {person_name}: {status}")
                                print(f"   Log ID: {new_log_id}")
                                print(f"   Remarks: {remarks}")

                                # Send confirmation to Arduino
                                ser.write(f"LOGGED:{status}:{person_name}\n".encode('utf-8'))

                        cursor.close()
                        conn.close()
                        print()

                    except Exception as e:
                        print(f"Database error: {e}")

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n\nStopped by user")
    finally:
        ser.close()
        print("Serial port closed")

if __name__ == "__main__":
    main()
