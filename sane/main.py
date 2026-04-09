# import sensors
#
# def cpu_temps():
#     sensors.init()
#     temps = {}
#
#     for chip in sensors.iter_detected_chips():
#         chip_name = f"{i ju}"
# !/usr/bin/env python3
"""
Hardware temperature monitor with logging.
Stores data in SQLite (structured) and CSV/TXT (user-readable).
"""

import sqlite3
import csv
import json
import subprocess
import re
import os
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional
from dataclasses import dataclass, asdict


@dataclass
class SensorReading:
    timestamp: str
    sensor_type: str  # cpu, gpu, fan, ssd
    device_name: str
    metric_name: str  # e.g., "Core_0", "temp1"
    value: float
    unit: str  # C, RPM, etc.


class TemperatureLogger:
    def __init__(self, db_path: str = "sensor_logs.db", csv_path: str = "sensor_logs.csv",
                 txt_path: str = "sensor_logs.txt"):
        self.db_path = db_path
        self.csv_path = csv_path
        self.txt_path = txt_path
        self._init_database()

    # ========== Database (SQLite) ==========

    def _init_database(self):
        """Create SQLite tables for sensor data."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sensor_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                sensor_type TEXT NOT NULL,
                device_name TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL
            )
        """)

        # Index for faster queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp ON sensor_readings(timestamp)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sensor_type ON sensor_readings(sensor_type)
        """)

        conn.commit()
        conn.close()

    def log_to_database(self, readings: list[SensorReading]):
        """Insert sensor readings into SQLite database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        data = [
            (r.timestamp, r.sensor_type, r.device_name, r.metric_name, r.value, r.unit)
            for r in readings
        ]

        cursor.executemany("""
            INSERT INTO sensor_readings (timestamp, sensor_type, device_name, metric_name, value, unit)
            VALUES (?, ?, ?, ?, ?, ?)
        """, data)

        conn.commit()
        conn.close()

    # ========== CSV Logging ==========

    def log_to_csv(self, readings: list[SensorReading]):
        """Append readings to CSV file (creates if doesn't exist)."""
        file_exists = Path(self.csv_path).exists()

        with open(self.csv_path, "a", newline="") as f:
            writer = csv.writer(f)

            # Write header if new file
            if not file_exists:
                writer.writerow(["timestamp", "sensor_type", "device_name", "metric_name", "value", "unit"])

            for r in readings:
                writer.writerow([r.timestamp, r.sensor_type, r.device_name, r.metric_name, r.value, r.unit])

    # ========== TXT Logging ==========

    def log_to_txt(self, readings: list[SensorReading], summary: Optional[Dict] = None):
        """Append human-readable log to TXT file."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with open(self.txt_path, "a") as f:
            f.write(f"\n{'=' * 50}\n")
            f.write(f"Log Entry: {timestamp}\n")
            f.write(f"{'=' * 50}\n")

            # Group by sensor type for readability
            by_type: Dict[str, list] = {}
            for r in readings:
                by_type.setdefault(r.sensor_type, []).append(r)

            for sensor_type, items in sorted(by_type.items()):
                f.write(f"\n[{sensor_type.upper()}]\n")
                for r in items:
                    f.write(f"  {r.device_name}/{r.metric_name}: {r.value:.1f} {r.unit}\n")

            if summary:
                f.write(f"\n[SUMMARY]\n")
                for key, val in summary.items():
                    f.write(f"  {key}: {val}\n")


# ========== Sensor Collection (simplified) ==========

def collect_sensors() -> tuple[list[SensorReading], Dict]:
    """Collect all sensor data and return readings + summary stats."""
    readings = []
    timestamp = datetime.now().isoformat()
    summary = {"total_sensors": 0, "cpu_max": None, "gpu_max": None}

    # --- CPU & Fans via lm-sensors ---
    try:
        result = subprocess.run(["sensors", "-u"], capture_output=True, text=True, check=True)
        current_chip = None

        for line in result.stdout.splitlines():
            if not line.startswith(" ") and line.strip().endswith(":"):
                current_chip = line.strip().rstrip(":")

            match = re.match(r"^\s+(\w+)_input:\s+([-\d.]+)", line)
            if match and current_chip:
                sensor, val = match.group(1), float(match.group(2))

                if "temp" in sensor:
                    readings.append(SensorReading(timestamp, "cpu", current_chip, sensor, val, "C"))
                    summary["cpu_max"] = max(summary["cpu_max"] or 0, val)
                elif "fan" in sensor:
                    readings.append(SensorReading(timestamp, "fan", current_chip, sensor, val, "RPM"))

    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # --- NVIDIA GPU ---
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,temperature.gpu", "--format=csv,noheader"],
            capture_output=True, text=True, check=True
        )
        for line in result.stdout.strip().splitlines():
            idx, name, temp = [x.strip() for x in line.split(",")]
            val = float(temp)
            readings.append(SensorReading(timestamp, "gpu", f"nvidia_{idx}", name.replace(" ", "_"), val, "C"))
            summary["gpu_max"] = max(summary["gpu_max"] or 0, val)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # --- AMD GPU via sysfs ---
    for card_dir in Path("/sys/class/drm").glob("card*/device/hwmon/hwmon*"):
        temp_file = card_dir / "temp1_input"
        if temp_file.exists():
            try:
                val = int(temp_file.read_text().strip()) / 1000.0
                readings.append(SensorReading(timestamp, "gpu", str(card_dir.parent.parent), "temp1", val, "C"))
                summary["gpu_max"] = max(summary["gpu_max"] or 0, val)
            except (ValueError, IOError):
                pass

    # --- NVMe SSD ---
    try:
        result = subprocess.run(["nvme", "list"], capture_output=True, text=True)
        for dev in re.findall(r"(/dev/nvme\d+)", result.stdout):
            try:
                smart = subprocess.run(["smartctl", "-A", dev], capture_output=True, text=True, check=True)
                for line in smart.stdout.splitlines():
                    if "Temperature" in line and "Celsius" in line:
                        match = re.search(r"(\d+)\s*\(Min/Max", line)
                        if match:
                            val = float(match.group(1))
                            readings.append(SensorReading(timestamp, "ssd", dev.replace("/", "_"), "temp", val, "C"))
            except subprocess.CalledProcessError:
                pass
    except FileNotFoundError:
        pass

    summary["total_sensors"] = len(readings)
    return readings, summary


# ========== Main ==========

def main():
    """Collect and log sensor data."""
    logger = TemperatureLogger(
        db_path="sensor_logs.db",
        csv_path="../sensor_logs.csv",
        txt_path="../sensor_logs.txt"
    )

    # Collect data
    readings, summary = collect_sensors()

    if not readings:
        print("No sensors detected. Check that lm-sensors, smartmontools, and nvme-cli are installed.")
        return

    # Log to all formats
    logger.log_to_database(readings)
    logger.log_to_csv(readings)
    logger.log_to_txt(readings, summary)

    # Print summary
    print(f"Logged {len(readings)} sensor readings:")
    print(f"  - Database: {logger.db_path}")
    print(f"  - CSV: {logger.csv_path}")
    print(f"  - TXT: {logger.txt_path}")
    print(f"\nCPU max: {summary.get('cpu_max', 'N/A')}, GPU max: {summary.get('gpu_max', 'N/A')}")


if __name__ == "__main__":
    main()