#!/usr/bin/env python3
"""
Hardware temperature monitor — real-time TUI with sanity scoring.
Uses rich for the btop-style live display.
Keeps SQLite/CSV/TXT logging from original.

Deps: pip install rich
"""

import sqlite3
import csv
import subprocess
import re
import time
import signal
import sys
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns
from rich.bar import Bar
from rich import box
from rich.align import Align
from rich.style import Style


# ─────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────

@dataclass
class SensorReading:
    timestamp: str
    sensor_type: str   # cpu, gpu, fan, ssd, power, clock, throttle, memory, storage_health
    device_name: str
    metric_name: str
    value: float
    unit: str          # C, RPM, MHz, W, %, errors, percent


# ─────────────────────────────────────────────
# Thresholds for sanity scoring
# ─────────────────────────────────────────────

THRESHOLDS = {
    "cpu": {"ok": 70, "warn": 85, "crit": 95},
    "gpu": {"ok": 75, "warn": 88, "crit": 100},
    "ssd": {"ok": 45, "warn": 60, "crit": 70},
    "fan": {"ok": 500, "warn": 200, "crit": 0},   # fan: low RPM is bad
    "power": {"ok": 100, "warn": 200, "crit": 300},  # watts
    "clock": {"ok": 2000, "warn": 1000, "crit": 500},  # MHz (throttling)
    "throttle": {"ok": 0, "warn": 1, "crit": 5},  # throttle events
    "memory": {"ok": 0, "warn": 1, "crit": 10},  # ECC errors
    "storage_health": {"ok": 95, "warn": 80, "crit": 60},  # % health
}

WEIGHTS = {"cpu": 0.30, "gpu": 0.25, "ssd": 0.10, "fan": 0.10, "power": 0.10, "clock": 0.05, "throttle": 0.05, "memory": 0.03, "storage_health": 0.02}


def _temp_score(val: float, thresholds: dict) -> float:
    """0.0 (dead) → 1.0 (healthy) for a temperature reading."""
    ok, warn, crit = thresholds["ok"], thresholds["warn"], thresholds["crit"]
    if val <= ok:
        return 1.0
    if val >= crit:
        return 0.0
    if val <= warn:
        return 1.0 - 0.5 * (val - ok) / (warn - ok)
    return 0.5 - 0.5 * (val - warn) / (crit - warn)


def _fan_score(val: float, thresholds: dict) -> float:
    """Higher RPM = healthier (opposite of temps)."""
    ok, warn, crit = thresholds["ok"], thresholds["warn"], thresholds["crit"]
    if val >= ok:
        return 1.0
    if val <= crit:
        return 0.0
    return (val - crit) / (ok - crit)


def compute_sanity(readings: List[SensorReading]) -> Tuple[float, dict]:
    """
    Returns overall sanity 0–100 and per-type averages.
    """
    per_type: Dict[str, List[float]] = {k: [] for k in THRESHOLDS}

    for r in readings:
        t = r.sensor_type
        if t not in THRESHOLDS:
            continue
        thresh = THRESHOLDS[t]
        if t == "fan":
            score = _fan_score(r.value, thresh)
        else:
            score = _temp_score(r.value, thresh)
        per_type[t].append(score)

    type_scores: Dict[str, Optional[float]] = {}
    total, weight_sum = 0.0, 0.0

    for t, scores in per_type.items():
        if scores:
            avg = sum(scores) / len(scores)
            type_scores[t] = avg
            w = WEIGHTS.get(t, 0.1)
            total += avg * w
            weight_sum += w
        else:
            type_scores[t] = None

    sanity = (total / weight_sum * 100) if weight_sum else 100.0
    return sanity, type_scores


# ─────────────────────────────────────────────
# Sensor collection (your original logic)
# ─────────────────────────────────────────────

def collect_sensors() -> Tuple[List[SensorReading], Dict]:
    readings: List[SensorReading] = []
    timestamp = datetime.now().isoformat()
    summary: Dict = {
        "total_sensors": 0, "cpu_max": None, "gpu_max": None,
        "cpu_power": None, "gpu_power": None,
        "cpu_clock": None, "gpu_clock": None,
        "throttle_events": 0, "ecc_errors": 0,
        "storage_health_min": None,
    }

    # lm-sensors (CPU temps + fans)
    try:
        result = subprocess.run(
            ["sensors", "-u"], capture_output=True, text=True, timeout=3, check=True
        )
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
    except Exception:
        pass

    # NVIDIA GPU
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,temperature.gpu", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3, check=True
        )
        for line in result.stdout.strip().splitlines():
            idx, name, temp = [x.strip() for x in line.split(",")]
            val = float(temp)
            readings.append(SensorReading(timestamp, "gpu", f"nvidia_{idx}", name.replace(" ", "_"), val, "C"))
            summary["gpu_max"] = max(summary["gpu_max"] or 0, val)
    except Exception:
        pass

    # AMD GPU via sysfs
    for card_dir in Path("/sys/class/drm").glob("card*/device/hwmon/hwmon*"):
        temp_file = card_dir / "temp1_input"
        if temp_file.exists():
            try:
                val = int(temp_file.read_text().strip()) / 1000.0
                readings.append(SensorReading(timestamp, "gpu", str(card_dir.parent.parent), "temp1", val, "C"))
                summary["gpu_max"] = max(summary["gpu_max"] or 0, val)
            except Exception:
                pass

    # NVMe SSD - Temperature and Health
    try:
        result = subprocess.run(["nvme", "list"], capture_output=True, text=True, timeout=3)
        for dev in re.findall(r"(/dev/nvme\d+)", result.stdout):
            try:
                # Temperature from smartctl
                smart = subprocess.run(
                    ["smartctl", "-A", dev], capture_output=True, text=True, timeout=3, check=True
                )
                for line in smart.stdout.splitlines():
                    if "Temperature" in line and "Celsius" in line:
                        m = re.search(r"(\d+)\s*\(Min/Max", line)
                        if m:
                            val = float(m.group(1))
                            readings.append(SensorReading(timestamp, "ssd", dev.replace("/", "_"), "temp", val, "C"))
                # Storage health percentage
                health_result = subprocess.run(
                    ["smartctl", "-H", dev], capture_output=True, text=True, timeout=3
                )
                # NVMe overall health percentage from smartctl -a
                full_smart = subprocess.run(
                    ["smartctl", "-a", dev], capture_output=True, text=True, timeout=3
                )
                # Look for percentage used or available spare
                for line in full_smart.stdout.splitlines():
                    if "Percentage Used" in line:
                        m = re.search(r"(\d+)%", line)
                        if m:
                            used = int(m.group(1))
                            health = max(0, 100 - used)
                            readings.append(SensorReading(timestamp, "storage_health", dev.replace("/", "_"), "health_percent", health, "%"))
                            summary["storage_health_min"] = min(summary["storage_health_min"] or 100, health)
                    elif "Available Spare" in line and "%" in line:
                        m = re.search(r"(\d+)%", line)
                        if m:
                            spare = int(m.group(1))
                            readings.append(SensorReading(timestamp, "storage_health", dev.replace("/", "_"), "available_spare", spare, "%"))
                    elif "Data Units Read" in line:
                        m = re.search(r"([\d,]+)\s*\[", line)
                        if m:
                            units = int(m.group(1).replace(",", ""))
                            readings.append(SensorReading(timestamp, "storage_health", dev.replace("/", "_"), "data_units_read", units, "units"))
                    elif "Data Units Written" in line:
                        m = re.search(r"([\d,]+)\s*\[", line)
                        if m:
                            units = int(m.group(1).replace(",", ""))
                            readings.append(SensorReading(timestamp, "storage_health", dev.replace("/", "_"), "data_units_written", units, "units"))
            except Exception:
                pass
    except Exception:
        pass

    # SATA SSD/HDD via smartctl
    try:
        result = subprocess.run(["lsblk", "-d", "-n", "-o", "NAME,TYPE"], capture_output=True, text=True, timeout=3)
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ("disk",):
                dev = f"/dev/{parts[0]}"
                try:
                    smart = subprocess.run(
                        ["smartctl", "-a", dev], capture_output=True, text=True, timeout=3
                    )
                    # Temperature
                    for line2 in smart.stdout.splitlines():
                        if "Temperature_Celsius" in line2 or "Airflow_Temperature_Cel" in line2:
                            vals = line2.split()
                            for v in vals:
                                try:
                                    temp = float(v)
                                    if 0 <= temp <= 100:
                                        readings.append(SensorReading(timestamp, "ssd", dev.replace("/", "_"), "temp", temp, "C"))
                                        break
                                except:
                                    continue
                        # Wear leveling count or SSD life
                        if "Wear_Leveling_Count" in line2 or "Media_Wearout_Indicator" in line2:
                            vals = line2.split()
                            for v in vals:
                                try:
                                    health = float(v)
                                    if 0 <= health <= 100:
                                        readings.append(SensorReading(timestamp, "storage_health", dev.replace("/", "_"), "wear_indicator", health, "%"))
                                        summary["storage_health_min"] = min(summary["storage_health_min"] or 100, health)
                                        break
                                except:
                                    continue
                        # Reallocated sectors (bad sign)
                        if "Reallocated_Sector_Ct" in line2:
                            vals = line2.split()
                            for v in vals:
                                try:
                                    count = int(v)
                                    readings.append(SensorReading(timestamp, "storage_health", dev.replace("/", "_"), "reallocated_sectors", count, "count"))
                                    break
                                except:
                                    continue
                except Exception:
                    pass
    except Exception:
        pass

    # CPU Clock Frequencies
    try:
        for cpu_dir in Path("/sys/devices/system/cpu").glob("cpu[0-9]*"):
            freq_file = cpu_dir / "cpufreq" / "scaling_cur_freq"
            if freq_file.exists():
                try:
                    freq_khz = int(freq_file.read_text().strip())
                    freq_mhz = freq_khz / 1000.0
                    cpu_name = cpu_dir.name
                    readings.append(SensorReading(timestamp, "clock", cpu_name, "current_freq", freq_mhz, "MHz"))
                    if cpu_name == "cpu0":
                        summary["cpu_clock"] = freq_mhz
                except Exception:
                    pass
    except Exception:
        pass

    # CPU Power (Intel RAPL)
    try:
        rapl_dirs = list(Path("/sys/class/powercap").glob("intel-rapl:*"))
        for rapl_dir in rapl_dirs:
            name_file = rapl_dir / "name"
            energy_file = rapl_dir / "energy_uj"
            if name_file.exists() and energy_file.exists():
                try:
                    name = name_file.read_text().strip()
                    energy_uj = int(energy_file.read_text().strip())
                    # Power in watts (approximate from energy readings)
                    power_w = energy_uj / 1e6  # rough conversion
                    if "package" in name.lower() or "core" in name.lower():
                        readings.append(SensorReading(timestamp, "power", str(rapl_dir.name), name, power_w, "W"))
                        if "package" in name.lower():
                            summary["cpu_power"] = power_w
                except Exception:
                    pass
    except Exception:
        pass

    # CPU Throttling (thermal throttling events)
    try:
        thermal_throttle_file = Path("/sys/devices/system/cpu/cpu0/thermal_throttle/core_throttle_count")
        if thermal_throttle_file.exists():
            count = int(thermal_throttle_file.read_text().strip())
            readings.append(SensorReading(timestamp, "throttle", "cpu0", "thermal_throttle_count", count, "count"))
            summary["throttle_events"] += count
    except Exception:
        pass

    try:
        for cpu_dir in Path("/sys/devices/system/cpu").glob("cpu[0-9]*"):
            throttle_file = cpu_dir / "thermal_throttle" / "core_throttle_count"
            if throttle_file.exists():
                try:
                    count = int(throttle_file.read_text().strip())
                    if count > 0:
                        readings.append(SensorReading(timestamp, "throttle", cpu_dir.name, "thermal_throttle_count", count, "count"))
                        summary["throttle_events"] += count
                except Exception:
                    pass
    except Exception:
        pass

    # Memory Health (ECC errors)
    try:
        edac_dir = Path("/sys/devices/system/edac/mc")
        if edac_dir.exists():
            for mc_dir in edac_dir.glob("mc*"):
                ce_file = mc_dir / "ce_count"  # Correctable errors
                ue_file = mc_dir / "ue_count"  # Uncorrectable errors
                try:
                    if ce_file.exists():
                        ce_count = int(ce_file.read_text().strip())
                        readings.append(SensorReading(timestamp, "memory", str(mc_dir.name), "ecc_correctable_errors", ce_count, "errors"))
                        summary["ecc_errors"] += ce_count
                    if ue_file.exists():
                        ue_count = int(ue_file.read_text().strip())
                        readings.append(SensorReading(timestamp, "memory", str(mc_dir.name), "ecc_uncorrectable_errors", ue_count, "errors"))
                        summary["ecc_errors"] += ue_count
                except Exception:
                    pass
    except Exception:
        pass

    # Try ras-mc-ctl for memory errors if available
    try:
        result = subprocess.run(
            ["ras-mc-ctl", "--error-count"], capture_output=True, text=True, timeout=3
        )
        for line in result.stdout.splitlines():
            if "error" in line.lower():
                m = re.search(r"(\d+)", line)
                if m:
                    count = int(m.group(1))
                    if count > 0:
                        readings.append(SensorReading(timestamp, "memory", "ras-mc", "memory_errors", count, "errors"))
                        summary["ecc_errors"] += count
    except Exception:
        pass

    # GPU Clock and Power for NVIDIA
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,clocks.gr,clocks.mem,power.draw", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3, check=True
        )
        for line in result.stdout.strip().splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 5:
                idx, name, gr_clock, mem_clock, power = parts[0], parts[1], parts[2], parts[3], parts[4]
                # Parse clock values (remove MHz suffix)
                gr_mhz = float(gr_clock.replace("MHz", "").strip()) if "MHz" in gr_clock else 0
                mem_mhz = float(mem_clock.replace("MHz", "").strip()) if "MHz" in mem_clock else 0
                # Parse power (remove W suffix)
                power_w = float(power.replace("W", "").strip()) if "W" in power else 0
                
                if gr_mhz > 0:
                    readings.append(SensorReading(timestamp, "clock", f"nvidia_{idx}", "graphics_clock", gr_mhz, "MHz"))
                    summary["gpu_clock"] = gr_mhz
                if mem_mhz > 0:
                    readings.append(SensorReading(timestamp, "clock", f"nvidia_{idx}", "memory_clock", mem_mhz, "MHz"))
                if power_w > 0:
                    readings.append(SensorReading(timestamp, "power", f"nvidia_{idx}", "power_draw", power_w, "W"))
                    summary["gpu_power"] = power_w
    except Exception:
        pass

    # GPU Throttling for NVIDIA
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,clocks_event_reasons.gpu_idle,clocks_event_reasons.sw_thermal_slowdown,clocks_event_reasons.hw_thermal_slowdown,clocks_event_reasons.hw_power_brake_slowdown", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3, check=True
        )
        for line in result.stdout.strip().splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 5:
                idx = parts[0]
                idle = parts[1] == "Active"
                sw_throttle = parts[2] == "Active"
                hw_throttle = parts[3] == "Active"
                power_throttle = parts[4] == "Active"
                
                if sw_throttle:
                    readings.append(SensorReading(timestamp, "throttle", f"nvidia_{idx}", "sw_thermal_throttle", 1, "flag"))
                if hw_throttle:
                    readings.append(SensorReading(timestamp, "throttle", f"nvidia_{idx}", "hw_thermal_throttle", 1, "flag"))
                if power_throttle:
                    readings.append(SensorReading(timestamp, "throttle", f"nvidia_{idx}", "power_throttle", 1, "flag"))
                throttle_count = sum([sw_throttle, hw_throttle, power_throttle])
                if throttle_count > 0:
                    summary["throttle_events"] += throttle_count
    except Exception:
        pass

    summary["total_sensors"] = len(readings)
    return readings, summary


# ─────────────────────────────────────────────
# Logging (your original logic, unchanged)
# ─────────────────────────────────────────────

class TemperatureLogger:
    def __init__(self, db_path="sensor_logs.db", csv_path="sensor_logs.csv", txt_path="sensor_logs.txt"):
        self.db_path, self.csv_path, self.txt_path = db_path, csv_path, txt_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS sensor_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, sensor_type TEXT, device_name TEXT,
                metric_name TEXT, value REAL, unit TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ts  ON sensor_readings(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_typ ON sensor_readings(sensor_type)")
        conn.commit(); conn.close()

    def log(self, readings: List[SensorReading]):
        # SQLite
        conn = sqlite3.connect(self.db_path)
        conn.cursor().executemany(
            "INSERT INTO sensor_readings(timestamp,sensor_type,device_name,metric_name,value,unit) VALUES(?,?,?,?,?,?)",
            [(r.timestamp, r.sensor_type, r.device_name, r.metric_name, r.value, r.unit) for r in readings]
        )
        conn.commit(); conn.close()

        # CSV
        file_exists = Path(self.csv_path).exists()
        with open(self.csv_path, "a", newline="") as f:
            w = csv.writer(f)
            if not file_exists:
                w.writerow(["timestamp", "sensor_type", "device_name", "metric_name", "value", "unit"])
            for r in readings:
                w.writerow([r.timestamp, r.sensor_type, r.device_name, r.metric_name, r.value, r.unit])

        # TXT
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.txt_path, "a") as f:
            f.write(f"\n{'='*50}\nLog Entry: {ts}\n{'='*50}\n")
            by_type: Dict[str, list] = {}
            for r in readings:
                by_type.setdefault(r.sensor_type, []).append(r)
            for st, items in sorted(by_type.items()):
                f.write(f"\n[{st.upper()}]\n")
                for r in items:
                    f.write(f"  {r.device_name}/{r.metric_name}: {r.value:.1f} {r.unit}\n")

    def clear_logs(self) -> bool:
        """Clear all log files. Returns True if successful."""
        try:
            # Clear SQLite table
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute("DELETE FROM sensor_readings")
            c.execute("VACUUM")
            conn.commit()
            conn.close()

            # Clear CSV (keep header)
            if Path(self.csv_path).exists():
                with open(self.csv_path, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["timestamp", "sensor_type", "device_name", "metric_name", "value", "unit"])

            # Clear TXT (add cleared marker)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.txt_path, "w") as f:
                f.write(f"{'='*50}\nLOGS CLEARED: {ts}\n{'='*50}\n")

            return True
        except Exception:
            return False


# ─────────────────────────────────────────────
# TUI rendering helpers
# ─────────────────────────────────────────────

def _val_color(val: float, unit: str, sensor_type: str) -> str:
    if unit == "RPM":
        if val >= 500:  return "green"
        if val >= 200:  return "yellow"
        return "red"
    if unit == "MHz":
        # For clocks: higher is better, low indicates throttling
        if val >= 1500: return "green"
        if val >= 800:  return "yellow"
        return "red"
    if unit == "W" or unit == "watt":
        # Power: moderate is good, very high is concerning
        if val <= 100: return "green"
        if val <= 200: return "yellow"
        return "red"
    if unit == "%" and sensor_type == "storage_health":
        # Health percentage: higher is better
        if val >= 95: return "green"
        if val >= 80: return "yellow"
        return "red"
    if unit == "errors" or unit == "count":
        # Errors: any is bad
        if val == 0:  return "green"
        if val <= 5:  return "yellow"
        return "red"
    # temperature
    thresh = THRESHOLDS.get(sensor_type, THRESHOLDS["cpu"])
    if val <= thresh["ok"]:   return "green"
    if val <= thresh["warn"]: return "yellow"
    return "red"


def _gradient_bar(val: float, max_val: float, width: int = 16, 
                 colors: Tuple[str, str, str] = ("green", "yellow", "red")) -> str:
    """Create a gradient bar with btop-style coloring (start, mid, end)."""
    pct = min(val / max_val, 1.0) if max_val > 0 else 0
    filled = int(pct * width)
    start_color, mid_color, end_color = colors
    
    # Split bar into 3 sections for gradient effect
    third = width // 3
    bar = ""
    for i in range(filled):
        if i < third:
            bar += f"[{start_color}]█[/{start_color}]"
        elif i < 2 * third:
            bar += f"[{mid_color}]█[/{mid_color}]"
        else:
            bar += f"[{end_color}]█[/{end_color}]"
    
    empty = width - filled
    bar += f"[bright_black]{'░' * empty}[/bright_black]"
    return bar


def _braille_bar(val: float, max_val: float, width: int = 8) -> str:
    """btop-style braille graph for small spaces."""
    pct = min(val / max_val, 1.0) if max_val > 0 else 0
    # Braille patterns: ⣀⣄⣆⣇⣏⣟⣿
    patterns = [' ', '⣀', '⣄', '⣆', '⣇', '⣏', '⣟', '⣿']
    idx = int(pct * (len(patterns) - 1))
    return patterns[idx] * width


# btop-inspired color scheme
BTOP_COLORS = {
    "cpu": {"box": "cyan", "gradient": ("green", "yellow", "red")},
    "gpu": {"box": "magenta", "gradient": ("green", "yellow", "red")},
    "ssd": {"box": "blue", "gradient": ("blue", "cyan", "green")},
    "fan": {"box": "green", "gradient": ("red", "yellow", "green")},  # reverse: low is bad
    "power": {"box": "yellow", "gradient": ("green", "yellow", "red")},
    "clock": {"box": "bright_cyan", "gradient": ("red", "yellow", "green")},  # low = throttling
    "throttle": {"box": "red", "gradient": ("green", "yellow", "red")},
    "memory": {"box": "bright_magenta", "gradient": ("green", "yellow", "red")},
    "storage_health": {"box": "bright_blue", "gradient": ("red", "yellow", "green")},
}


def _sanity_gradient_bar(score: float, width: int = 25) -> Text:
    """btop-style gradient sanity bar: green→yellow→red."""
    filled = int(score / 100 * width)
    t = Text()
    
    # btop gradient: green for healthy, yellow mid, red for critical
    third = width // 3
    for i in range(filled):
        if i < third:
            t.append("█", style="green")
        elif i < 2 * third:
            t.append("█", style="yellow")
        else:
            t.append("█", style="red")
    
    t.append("░" * (width - filled), style="bright_black")
    return t


def build_header(sanity: float, tick: int) -> Panel:
    """btop-style header with gradient sanity bar."""
    spin = "⣾⣽⣻⢿⡿⣟⣯⣷"[tick % 8]
    
    # btop-style color for percentage
    if sanity >= 80:
        pct_color = "bold green"
    elif sanity >= 50:
        pct_color = "bold yellow"
    else:
        pct_color = "bold red"
    
    score_bar = _sanity_gradient_bar(sanity)

    t = Text()
    t.append(f" {spin} ", style="bright_cyan")
    t.append("SANE", style="bold bright_cyan")
    t.append("-o-meter ", style="dim bright_cyan")
    t.append(score_bar)
    t.append(f" {sanity:5.1f}%", style=pct_color)
    t.append(f" {datetime.now().strftime('%H:%M:%S')}", style="bright_black")

    return Panel(
        Align.center(t),
        border_style="bright_cyan",
        box=box.HEAVY_HEAD,
        padding=(0, 1),
    )


def build_sensor_table(
    readings: List[SensorReading],
    sensor_type: str,
    title: str,
    max_temp: float = 100,
    max_rows: int = 4,
) -> Panel:
    items = [r for r in readings if r.sensor_type == sensor_type]
    btop = BTOP_COLORS.get(sensor_type, BTOP_COLORS["cpu"])

    table = Table(box=box.SIMPLE, show_header=True, header_style=f"bold {btop['box']}", expand=True, padding=(0, 1))
    table.add_column("Device", style="dim", no_wrap=True, max_width=16)
    table.add_column("Metric", no_wrap=True, max_width=12)
    table.add_column("Value", justify="right", width=7)
    table.add_column("Bar", min_width=14)

    # Determine border color based on health
    has_critical = any(_val_color(r.value, r.unit, sensor_type) == "red" for r in items)
    has_warning = any(_val_color(r.value, r.unit, sensor_type) == "yellow" for r in items)
    
    if not items:
        border = btop["box"]  # Default box color from btop theme
    elif has_critical:
        border = "red"
    elif has_warning:
        border = "yellow"
    else:
        border = btop["box"]

    if not items:
        table.add_row("—", "—", "n/a", "no data")
    else:
        # Limit to max_rows, sorted by value
        sorted_items = sorted(items, key=lambda x: x.value, reverse=(sensor_type != "fan"))[:max_rows]
        for r in sorted_items:
            color = _val_color(r.value, r.unit, sensor_type)
            val_str = f"{r.value:.0f}{r.unit}"
            
            # Use btop gradient bars
            bar_max = max_temp if r.unit == "C" else 3000
            if sensor_type in ("cpu", "gpu", "ssd"):
                bar = _gradient_bar(r.value, bar_max, width=14, colors=btop["gradient"])
            else:
                bar = _gradient_bar(r.value, bar_max, width=14)
            
            table.add_row(
                r.device_name[-16:],
                r.metric_name[:12],
                Text(val_str, style=f"bold {color}"),
                Text.from_markup(bar),
            )

    # btop-style title with box color
    title_style = f"[bold {btop['box']}]{title}[/bold {btop['box']}]"
    return Panel(table, title=title_style, border_style=border, box=box.ROUNDED, height=max_rows + 3)


def build_summary(sanity: float, type_scores: dict, summary: dict, logging_on: bool) -> Panel:
    table = Table(box=box.SIMPLE, show_header=False, expand=True, padding=(0, 1))
    table.add_column("Key",   style="bright_black", width=14)
    table.add_column("Value", style="white")

    # btop-style colored section labels
    labels = {
        "cpu": ("CPU", BTOP_COLORS["cpu"]["box"]),
        "gpu": ("GPU", BTOP_COLORS["gpu"]["box"]),
        "ssd": ("SSD", BTOP_COLORS["ssd"]["box"]),
        "fan": ("Fans", BTOP_COLORS["fan"]["box"]),
        "power": ("Power", BTOP_COLORS["power"]["box"]),
        "clock": ("Clock", BTOP_COLORS["clock"]["box"]),
        "throttle": ("Throttle", BTOP_COLORS["throttle"]["box"]),
        "memory": ("Memory", BTOP_COLORS["memory"]["box"]),
        "storage_health": ("Storage", BTOP_COLORS["storage_health"]["box"]),
    }
    for k, (label, color) in labels.items():
        sc = type_scores.get(k)
        if sc is not None:
            pct = sc * 100
            val_color = "green" if pct >= 80 else ("yellow" if pct >= 50 else "red")
            table.add_row(f"[{color}]{label}[/{color}]", Text(f"{pct:.0f}%", style=f"bold {val_color}"))
        else:
            table.add_row(f"[{color}]{label}[/{color}]", Text("N/A", style="bright_black"))

    # Compact metrics with btop-style colors
    cpu_temp = summary.get('cpu_max') or '—'
    gpu_temp = summary.get('gpu_max') or '—'
    cpu_pwr = summary.get('cpu_power') or '—'
    gpu_pwr = summary.get('gpu_power') or '—'
    cpu_clk = summary.get('cpu_clock') or '—'
    gpu_clk = summary.get('gpu_clock') or '—'
    storage = summary.get('storage_health_min') or '—'
    
    table.add_row("", "")
    table.add_row("Sensors", str(summary.get("total_sensors", 0)))
    table.add_row(f"[cyan]CPU[cyan] {cpu_temp}°C {cpu_pwr}W", f"{cpu_clk}MHz")
    table.add_row(f"[magenta]GPU[magenta] {gpu_temp}°C {gpu_pwr}W", f"{gpu_clk}MHz")
    table.add_row(f"[red]T:{summary.get('throttle_events', 0)}[red] [bright_magenta]M:{summary.get('ecc_errors', 0)}[bright_magenta] [bright_blue]S:{storage}%[bright_blue]", "")
    log_color = "green" if logging_on else "bright_black"
    log_status = "ON" if logging_on else "OFF"
    table.add_row(f"Log [bold {log_color}]{log_status}[/{log_color}]", "")

    return Panel(table, title="[bold bright_cyan]Summary[/bold bright_cyan]", border_style="bright_cyan", box=box.ROUNDED)


def build_footer() -> Panel:
    """btop-style footer with keyboard shortcuts."""
    t = Text()
    t.append("[q]", style="bold bright_cyan")
    t.append("uit ", style="bright_black")
    t.append("[l]", style="bold bright_cyan")
    t.append("og ", style="bright_black")
    t.append("[c]", style="bold bright_cyan")
    t.append("lear ", style="bright_black")
    return Panel(Align.center(t), border_style="bright_black", box=box.SIMPLE, padding=(0, 1))


def build_layout(readings, summary, sanity, type_scores, tick, logging_on) -> Layout:
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )

    layout["body"].split_row(
        Layout(name="left", ratio=3),
        Layout(name="right", ratio=1, minimum_size=20),
    )

    layout["left"].split_column(
        Layout(name="top_row", size=9),
        Layout(name="bot_row", size=9),
    )

    layout["top_row"].split_row(
        Layout(name="cpu"),
        Layout(name="gpu"),
    )

    layout["bot_row"].split_row(
        Layout(name="ssd"),
        Layout(name="fan"),
    )

    layout["right"].split_column(
        Layout(name="summary"),
        Layout(name="power", size=8),
        Layout(name="clock", size=8),
    )

    layout["header"].update(build_header(sanity, tick))
    layout["footer"].update(build_footer())
    layout["cpu"].update(build_sensor_table(readings, "cpu", "󰻠 CPU"))
    layout["gpu"].update(build_sensor_table(readings, "gpu", "󰾲 GPU"))
    layout["ssd"].update(build_sensor_table(readings, "ssd", "󱛟 SSD"))
    layout["fan"].update(build_sensor_table(readings, "fan", "󰈀 Fans", max_temp=3000))
    layout["power"].update(build_sensor_table(readings, "power", "⚡ Power"))
    layout["clock"].update(build_sensor_table(readings, "clock", "󰓅 Clocks"))
    layout["summary"].update(build_summary(sanity, type_scores, summary, logging_on))

    return layout


# ─────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────

REFRESH_INTERVAL = 2.0   # seconds between polls
LOG_EVERY_N = 30         # log every N ticks (~60s default)


def _getch() -> str:
    """Read a single character from stdin without waiting for enter."""
    import termios
    import tty
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def main():
    console = Console()
    logger = TemperatureLogger()
    logging_on = True
    tick = 0
    running = True
    last_action = None

    def _quit(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _quit)
    signal.signal(signal.SIGTERM, _quit)

    def keyboard_listener():
        """Listen for keyboard input in a separate thread."""
        nonlocal logging_on, running, last_action
        while running:
            try:
                ch = _getch()
                if ch == 'q' or ch == '\x03':  # q or Ctrl+C
                    _quit()
                    break
                elif ch == 'L' or ch == 'l':
                    logging_on = not logging_on
                    last_action = "logging " + ("ON" if logging_on else "OFF")
                elif ch == 'C' or ch == 'c':
                    if logger.clear_logs():
                        last_action = "logs cleared"
                    else:
                        last_action = "clear failed"
            except Exception:
                pass

    # Initial read
    readings, summary = collect_sensors()
    if not readings:
        console.print(
            "[red]No sensors detected.[/red] "
            "Install [bold]lm-sensors[/bold], [bold]smartmontools[/bold], [bold]nvme-cli[/bold]."
        )
        sys.exit(1)

    sanity, type_scores = compute_sanity(readings)

    # Start keyboard listener thread
    kb_thread = threading.Thread(target=keyboard_listener, daemon=True)
    kb_thread.start()

    with Live(
        build_layout(readings, summary, sanity, type_scores, tick, logging_on),
        console=console,
        refresh_per_second=4,
        screen=True,
    ) as live:
        while running:
            time.sleep(REFRESH_INTERVAL)
            tick += 1

            readings, summary = collect_sensors()
            sanity, type_scores = compute_sanity(readings)

            if logging_on and tick % LOG_EVERY_N == 0:
                logger.log(readings)

            # Build layout with last action if any
            layout = build_layout(readings, summary, sanity, type_scores, tick, logging_on)
            live.update(layout)

            if last_action:
                # Briefly show action feedback
                time.sleep(0.3)
                last_action = None

    console.print("\n[dim]Goodbye. Logs saved.[/dim]")


if __name__ == "__main__":
    main()