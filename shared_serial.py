import os
import serial
import serial.tools.list_ports
import threading
import time


class SharedSerialPort:
    """
    Manages a single serial connection shared between multiple readers.
    Routes incoming messages to registered handlers based on message prefix.
    Opens the port on the first start() call; closes when all handlers unregister.
    """

    def __init__(self):
        self.serial_port = None
        self.is_open = False
        self.port_name = None
        self._running = False
        self._reader_thread = None
        self._handlers = {}   # prefix (str) -> callback(line: str)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Port lifecycle
    # ------------------------------------------------------------------

    def find_arduino_port(self):
        """Automatically detect Arduino/USB serial port."""
        ports = serial.tools.list_ports.comports()
        for port in ports:
            if any(kw in port.description for kw in ('Arduino', 'CH340', 'USB Serial')):
                return port.device
            if 'usbserial' in port.device.lower() or 'usbmodem' in port.device.lower():
                return port.device
            if 'USB' in port.hwid and any(x in port.hwid for x in ('VID:PID', 'FTDI', 'CP210', 'CH340')):
                return port.device
        return None

    def start(self, port=None):
        """Open the serial port if not already open."""
        if self.is_open:
            return {"success": True, "message": f"Port already open on {self.port_name}", "port": self.port_name}

        if port is None:
            # First try environment variable for explicit port configuration
            env_port = os.getenv('SERIAL_PORT')
            if env_port:
                port = env_port
                print(f"[INFO] Using SERIAL_PORT from environment: {port}")
            else:
                # Fall back to automatic detection
                port = self.find_arduino_port()

        if port is None:
            available = [p.device for p in serial.tools.list_ports.comports()]
            return {
                "success": False,
                "error": f"Arduino not found. Available ports: {', '.join(available) if available else 'None'}"
            }

        try:
            self.serial_port = serial.Serial(port, 9600, timeout=1, exclusive=True)
            time.sleep(2)
            self.serial_port.reset_input_buffer()
            self.serial_port.reset_output_buffer()

            self.port_name = port
            self.is_open = True
            self._running = True
            self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._reader_thread.start()

            print(f"SharedSerialPort: opened {port}")
            return {"success": True, "message": f"Serial port opened on {port}", "port": port}

        except serial.SerialException as e:
            error_msg = str(e)
            if "Resource busy" in error_msg or "exclusively lock" in error_msg:
                return {"success": False, "error": f"Port {port} is already in use by another process."}
            elif "Permission denied" in error_msg:
                return {"success": False, "error": f"Permission denied. Try: sudo chmod 666 {port}"}
            else:
                return {"success": False, "error": f"Failed to open serial port: {error_msg}"}
        except Exception as e:
            return {"success": False, "error": f"Unexpected error: {str(e)}"}

    def _close(self):
        """Internal: stop the read loop and close the port."""
        self._running = False
        if self._reader_thread:
            self._reader_thread.join(timeout=3)
            self._reader_thread = None
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
        self.serial_port = None
        self.is_open = False
        self.port_name = None
        print("SharedSerialPort: closed")

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def register_handler(self, prefix, callback):
        """Register a callback for lines starting with *prefix*."""
        with self._lock:
            self._handlers[prefix] = callback
        print(f"SharedSerialPort: registered handler for '{prefix}'")

    def unregister_handler(self, prefix):
        """Unregister a handler. Closes the port if no handlers remain."""
        with self._lock:
            self._handlers.pop(prefix, None)
            remaining = len(self._handlers)
        print(f"SharedSerialPort: unregistered handler for '{prefix}'")
        if remaining == 0:
            self._close()

    # ------------------------------------------------------------------
    # Writing back to Arduino
    # ------------------------------------------------------------------

    def write(self, data: bytes):
        """Send raw bytes to the Arduino."""
        try:
            if self.serial_port and self.serial_port.is_open:
                self.serial_port.write(data)
        except Exception as e:
            print(f"SharedSerialPort: write error: {e}")

    # ------------------------------------------------------------------
    # Read loop
    # ------------------------------------------------------------------

    def _read_loop(self):
        print(f"SharedSerialPort: read loop started on {self.port_name}")
        while self._running:
            try:
                if self.serial_port and self.serial_port.in_waiting:
                    line = self.serial_port.readline().decode('utf-8', errors='replace').strip()
                    if not line:
                        continue
                    with self._lock:
                        handlers = dict(self._handlers)
                    for prefix, callback in handlers.items():
                        if line.startswith(prefix):
                            # Run callback in a thread so a slow handler doesn't block reads
                            threading.Thread(target=callback, args=(line,), daemon=True).start()
                            break
                time.sleep(0.05)
            except serial.SerialException as e:
                print(f"SharedSerialPort: serial error in read loop: {e}")
                self._running = False
                break
            except Exception as e:
                print(f"SharedSerialPort: error in read loop: {e}")
                time.sleep(0.1)
        print("SharedSerialPort: read loop stopped")
