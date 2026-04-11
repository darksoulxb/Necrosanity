#!/usr/bin/env python3
"""
System stability monitor - tracks hardware health over time.
Measures stability, detects anomalies, provides human-friendly insights.
"""

from __future__ import annotations

import sqlite3
import csv
import subprocess
import re
import statistics
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Callable
from dataclasses import dataclass, asdict
from collections import deque
from abc import ABC, abstractmethod


# ============ Data Models ============

@dataclass(frozen=True)
class SensorReading:
    """Immutable sensor measurement."""
    timestamp: str
    sensor_type: str  # cpu, gpu, fan, ssd, memory, network
    device_name: str
    metric_name: str
    value: float
    unit: str

    @property
    def full_name(self) -> str:
        return f"{self.device_name}/{self.metric_name}"


@dataclass
class StabilitySnapshot:
    """Stability assessment for a sensor at a point in time."""
    reading: SensorReading
    trend: str  # rising, falling, stable, spiking
    volatility: float  # 0-1, higher = less stable
    severity: str  # normal, warning, critical
    time_in_state: int  # seconds since last trend change


@dataclass
class HealthReport:
    """Human-readable system health summary."""
    timestamp: str
    overall_stability: float  # 0-100%
    grade: str  # A+ to F
    concerns: List[str]
    highlights: List[str]
    sensor_status: Dict[str, str]


# ============ Sensor Collectors (Modular) ============

class SensorCollector(ABC):
    """Base class for hardware sensor collectors."""

    @abstractmethod
    def collect(self, timestamp: str) -> List[SensorReading]:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass


class LMCollector(SensorCollector):
    """lm-sensors for CPU temperature and fan speeds."""

    @property
    def name(self) -> str:
        return "lm-sensors"

    def collect(self, timestamp: str) -> List[SensorReading]:
        readings = []
        try:
            result = subprocess.run(
                ["sensors", "-u"],
                capture_output=True, text=True, check=True, timeout=5
            )
            current_chip = None

            for line in result.stdout.splitlines():
                if not line.startswith(" ") and line.strip().endswith(":"):
                    current_chip = line.strip().rstrip(":")

                match = re.match(r"^\s+(\w+)_input:\s+([-\d.]+)", line)
                if match and current_chip:
                    sensor, val = match.group(1), float(match.group(2))

                    if "temp" in sensor:
                        readings.append(SensorReading(
                            timestamp, "cpu", current_chip, sensor, val, "°C"
                        ))
                    elif "fan" in sensor:
                        readings.append(SensorReading(
                            timestamp, "fan", current_chip, sensor, val, "RPM"
                        ))
        except Exception:
            pass
        return readings


class NvidiaCollector(SensorCollector):
    """nvidia-smi for NVIDIA GPU metrics."""

    @property
    def name(self) -> str:
        return "nvidia-smi"

    def collect(self, timestamp: str) -> List[SensorReading]:
        readings = []
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,name,temperature.gpu,utilization.gpu,power.draw",
                 "--format=csv,noheader"],
                capture_output=True, text=True, check=True, timeout=5
            )
            for line in result.stdout.strip().splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 3:
                    idx, name = parts[0], parts[1]
                    temp = float(parts[2])
                    readings.append(SensorReading(
                        timestamp, "gpu", f"nvidia_{idx}", "temperature", temp, "°C"
                    ))
        except Exception:
            pass
        return readings


class AMDGPUCollector(SensorCollector):
    """AMD GPU via sysfs."""

    @property
    def name(self) -> str:
        return "amdgpu-sysfs"

    def collect(self, timestamp: str) -> List[SensorReading]:
        readings = []
        for card_dir in Path("/sys/class/drm").glob("card*/device/hwmon/hwmon*"):
            temp_file = card_dir / "temp1_input"
            if temp_file.exists():
                try:
                    val = int(temp_file.read_text().strip()) / 1000.0
                    readings.append(SensorReading(
                        timestamp, "gpu", card_dir.parent.parent.name, "temperature", val, "°C"
                    ))
                except (ValueError, IOError):
                    pass
        return readings


class NVMeCollector(SensorCollector):
    """NVMe SSD temperature via smartctl."""

    @property
    def name(self) -> str:
        return "nvme-smart"

    def collect(self, timestamp: str) -> List[SensorReading]:
        readings = []
        try:
            result = subprocess.run(
                ["nvme", "list"],
                capture_output=True, text=True, timeout=5
            )
            for dev in re.findall(r"(/dev/nvme\d+)", result.stdout):
                try:
                    smart = subprocess.run(
                        ["smartctl", "-A", dev],
                        capture_output=True, text=True, check=True, timeout=5
                    )
                    for line in smart.stdout.splitlines():
                        if "Temperature" in line and "Celsius" in line:
                            match = re.search(r"(\d+)\s*\(Min/Max", line)
                            if match:
                                val = float(match.group(1))
                                readings.append(SensorReading(
                                    timestamp, "ssd", dev.replace("/", "_"), "temperature", val, "°C"
                                ))
                except subprocess.CalledProcessError:
                    pass
        except FileNotFoundError:
            pass
        return readings


# ============ Stability Engine ============

class StabilityEngine:
    """
    Analyzes sensor readings for stability patterns.
    Detects spikes, trends, and measures volatility.
    """

    def __init__(self, history_size: int = 60):
        self.history_size = history_size
        self._history: Dict[str, deque] = {}  # sensor -> recent values
        self._trends: Dict[str, str] = {}
        self._trend_since: Dict[str, datetime] = {}

    def analyze(self, reading: SensorReading) -> StabilitySnapshot:
        """Analyze a reading and return stability snapshot."""
        key = f"{reading.sensor_type}:{reading.full_name}"

        if key not in self._history:
            self._history[key] = deque(maxlen=self.history_size)
            self._trends[key] = "unknown"
            self._trend_since[key] = datetime.now()

        history = self._history[key]
        history.append(reading.value)

        # Calculate metrics
        volatility = self._calculate_volatility(history)
        trend = self._detect_trend(history)
        severity = self._assess_severity(reading, history)

        # Track trend changes
        if trend != self._trends[key]:
            self._trends[key] = trend
            self._trend_since[key] = datetime.now()

        time_in_state = int((datetime.now() - self._trend_since[key]).total_seconds())

        return StabilitySnapshot(
            reading=reading,
            trend=trend,
            volatility=round(volatility, 3),
            severity=severity,
            time_in_state=time_in_state
        )

    def _calculate_volatility(self, history: deque) -> float:
        """0 = perfectly stable, 1 = completely unstable."""
        if len(history) < 3:
            return 0.0

        values = list(history)
        mean_val = statistics.mean(values)
        if mean_val == 0:
            return 0.0

        # Coefficient of variation normalized
        try:
            std_dev = statistics.stdev(values)
            cv = std_dev / abs(mean_val)
            return min(cv / 0.5, 1.0)  # Cap at 1.0
        except statistics.StatisticsError:
            return 0.0

    def _detect_trend(self, history: deque) -> str:
        """Detect trend direction from recent values."""
        if len(history) < 5:
            return "insufficient_data"

        values = list(history)
        recent = values[-3:]
        older = values[:len(values)//2]

        recent_avg = sum(recent) / len(recent)
        older_avg = sum(older) / len(older)

        diff = recent_avg - older_avg
        diff_pct = abs(diff) / max(abs(older_avg), 0.001) * 100

        # Check for spike (sudden change)
        if len(values) >= 3:
            last_change = abs(values[-1] - values[-2]) / max(abs(values[-2]), 0.001) * 100
            if last_change > 20:
                return "spiking"

        if diff_pct < 5:
            return "stable"
        elif diff > 0:
            return "rising"
        else:
            return "falling"

    def _assess_severity(self, reading: SensorReading, history: deque) -> str:
        """Assess if reading indicates a problem."""
        value = reading.value

        # Temperature-based severity
        if reading.unit == "°C":
            if "cpu" in reading.sensor_type and value > 85:
                return "critical"
            if "cpu" in reading.sensor_type and value > 75:
                return "warning"
            if "gpu" in reading.sensor_type and value > 80:
                return "critical"
            if "gpu" in reading.sensor_type and value > 70:
                return "warning"
            if "ssd" in reading.sensor_type and value > 70:
                return "warning"

        return "normal"

    def overall_stability(self, snapshots: List[StabilitySnapshot]) -> float:
        """Calculate overall stability score 0-100."""
        if not snapshots:
            return 100.0

        scores = []
        for snap in snapshots:
            base = 100.0

            # Deduct for volatility
            base -= snap.volatility * 30

            # Deduct for unstable trends
            if snap.trend in ["spiking", "rising"]:
                base -= 15

            # Deduct for severity
            if snap.severity == "warning":
                base -= 10
            elif snap.severity == "critical":
                base -= 30

            scores.append(max(base, 0))

        return round(sum(scores) / len(scores), 1)


# ============ Human Formatter ============

class HumanFormatter:
    """Converts technical data into human-friendly descriptions."""

    TREND_EMOJI = {
        "stable": "➖",
        "rising": "📈",
        "falling": "📉",
        "spiking": "⚡",
        "insufficient_data": "❓"
    }

    SEVERITY_EMOJI = {
        "normal": "🟢",
        "warning": "🟡",
        "critical": "🔴"
    }

    def format_reading(self, snap: StabilitySnapshot) -> str:
        """Single line human description of a sensor."""
        r = snap.reading
        trend = self.TREND_EMOJI.get(snap.trend, "❓")
        severity = self.SEVERITY_EMOJI.get(snap.severity, "⚪")

        volatility_bar = self._volatility_bar(snap.volatility)

        return f"{severity} {trend} {r.device_name}: {r.value:.1f}{r.unit} {volatility_bar}"

    def _volatility_bar(self, volatility: float) -> str:
        """Visual volatility indicator."""
        filled = int((1 - volatility) * 5)
        return "▓" * filled + "░" * (5 - filled)

    def format_report(self, report: HealthReport) -> str:
        """Full human-readable health report."""
        lines = [
            "",
            "╔" + "═" * 48 + "╗",
            f"║  SYSTEM HEALTH REPORT - Grade {report.grade:3}        ║",
            f"║  Stability: {report.overall_stability}%                           ║",
            "╠" + "═" * 48 + "╣",
        ]

        if report.concerns:
            lines.append("║  ⚠️  Concerns:                                  ║")
            for concern in report.concerns[:3]:
                truncated = concern[:42] + "..." if len(concern) > 45 else concern
                lines.append(f"║     • {truncated:<42} ║")

        if report.highlights:
            lines.append("║  ✓  Highlights:                                ║")
            for highlight in report.highlights[:2]:
                truncated = highlight[:42] + "..." if len(highlight) > 45 else highlight
                lines.append(f"║     • {truncated:<42} ║")

        lines.append("╚" + "═" * 48 + "╝")

        return "\n".join(lines)

    def trend_description(self, trend: str) -> str:
        """Human-friendly trend description."""
        descriptions = {
            "stable": "holding steady",
            "rising": "climbing",
            "falling": "cooling down",
            "spiking": "fluctuating wildly",
            "insufficient_data": "collecting baseline data"
        }
        return descriptions.get(trend, trend)


# ============ Storage (modular backends) ============

class StorageBackend(ABC):
    """Abstract storage backend."""

    @abstractmethod
    def save(self, snapshots: List[StabilitySnapshot]) -> None:
        pass


class SQLiteBackend(StorageBackend):
    """SQLite storage with stability metrics."""

    def __init__(self, db_path: str = "stability.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
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
                unit TEXT NOT NULL,
                trend TEXT NOT NULL,
                volatility REAL NOT NULL,
                severity TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_time ON sensor_readings(timestamp)
        """)

        conn.commit()
        conn.close()

    def save(self, snapshots: List[StabilitySnapshot]) -> None:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        data = [
            (s.reading.timestamp, s.reading.sensor_type, s.reading.device_name,
             s.reading.metric_name, s.reading.value, s.reading.unit,
             s.trend, s.volatility, s.severity)
            for s in snapshots
        ]

        cursor.executemany("""
            INSERT INTO sensor_readings
            (timestamp, sensor_type, device_name, metric_name, value, unit, trend, volatility, severity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, data)

        conn.commit()
        conn.close()


class CSVBackend(StorageBackend):
    """Simple CSV logging."""

    def __init__(self, csv_path: str = "sensor_logs.csv"):
        self.csv_path = csv_path

    def save(self, snapshots: List[StabilitySnapshot]) -> None:
        file_exists = Path(self.csv_path).exists()

        with open(self.csv_path, "a", newline="") as f:
            writer = csv.writer(f)

            if not file_exists:
                writer.writerow([
                    "timestamp", "sensor_type", "device_name", "metric_name",
                    "value", "unit", "trend", "volatility", "severity"
                ])

            for s in snapshots:
                writer.writerow([
                    s.reading.timestamp, s.reading.sensor_type, s.reading.device_name,
                    s.reading.metric_name, s.reading.value, s.reading.unit,
                    s.trend, s.volatility, s.severity
                ])


# ============ Core System ============

class SystemMonitor:
    """Main orchestrator - collects sensors, analyzes stability, generates reports."""

    def __init__(
        self,
        collectors: Optional[List[SensorCollector]] = None,
        storage: Optional[List[StorageBackend]] = None,
        history_size: int = 60
    ):
        self.collectors = collectors or self._default_collectors()
        self.storage = storage or []
        self.stability = StabilityEngine(history_size=history_size)
        self.formatter = HumanFormatter()

    def _default_collectors(self) -> List[SensorCollector]:
        return [
            LMCollector(),
            NvidiaCollector(),
            AMDGPUCollector(),
            NVMeCollector()
        ]

    def scan(self) -> List[StabilitySnapshot]:
        """Collect and analyze all sensors."""
        timestamp = datetime.now().isoformat()
        readings: List[SensorReading] = []

        for collector in self.collectors:
            readings.extend(collector.collect(timestamp))

        snapshots = [self.stability.analyze(r) for r in readings]
        return snapshots

    def report(self, snapshots: Optional[List[StabilitySnapshot]] = None) -> HealthReport:
        """Generate human-readable health report."""
        if snapshots is None:
            snapshots = self.scan()

        stability_score = self.stability.overall_stability(snapshots)

        # Grade calculation
        if stability_score >= 95:
            grade = "A+"
        elif stability_score >= 90:
            grade = "A"
        elif stability_score >= 85:
            grade = "B+"
        elif stability_score >= 80:
            grade = "B"
        elif stability_score >= 70:
            grade = "C"
        elif stability_score >= 60:
            grade = "D"
        else:
            grade = "F"

        concerns = []
        highlights = []
        sensor_status = {}

        for snap in snapshots:
            name = snap.reading.full_name

            if snap.severity == "critical":
                concerns.append(
                    f"{name} is CRITICAL at {snap.reading.value:.1f}{snap.reading.unit}"
                )
                sensor_status[name] = "🔴 critical"
            elif snap.severity == "warning":
                concerns.append(
                    f"{name} is elevated ({snap.reading.value:.1f}{snap.reading.unit})"
                )
                sensor_status[name] = "🟡 warning"
            elif snap.trend == "spiking":
                concerns.append(
                    f"{name} is fluctuating wildly (volatility: {snap.volatility:.1%})"
                )
                sensor_status[name] = "⚡ unstable"
            elif snap.volatility < 0.1 and snap.severity == "normal":
                highlights.append(f"{name} is rock stable")
                sensor_status[name] = "🟢 stable"
            else:
                sensor_status[name] = "⚪ ok"

        return HealthReport(
            timestamp=datetime.now().isoformat(),
            overall_stability=stability_score,
            grade=grade,
            concerns=concerns,
            highlights=highlights,
            sensor_status=sensor_status
        )

    def run(self, verbose: bool = True) -> HealthReport:
        """Full cycle: collect, analyze, store, report."""
        snapshots = self.scan()
        report = self.report(snapshots)

        # Store data
        for backend in self.storage:
            backend.save(snapshots)

        if verbose:
            print(self.formatter.format_report(report))
            print()
            for snap in snapshots[:8]:  # Show first 8 sensors
                print("  " + self.formatter.format_reading(snap))
            if len(snapshots) > 8:
                print(f"  ... and {len(snapshots) - 8} more sensors")

        return report


# ============ Predictive Failure Analysis ============

@dataclass
class FailurePrediction:
    """Predicted failure for a sensor."""
    sensor_name: str
    current_value: float
    unit: str
    trend_rate: float  # units per day
    days_to_failure: Optional[float]  # None if not predictable
    failure_threshold: float
    confidence: float  # 0-1 based on data quality

    @property
    def doom_countdown(self) -> str:
        """Human readable time to failure."""
        if self.days_to_failure is None:
            return "unknown"
        if self.days_to_failure <= 0:
            return "IMMINENT"
        if self.days_to_failure > 365:
            return f"{self.days_to_failure/365:.1f} years"
        if self.days_to_failure > 30:
            return f"{self.days_to_failure/30:.0f} months"
        if self.days_to_failure > 7:
            return f"{self.days_to_failure/7:.0f} weeks"
        if self.days_to_failure > 1:
            return f"{self.days_to_failure:.0f} days"
        hours = self.days_to_failure * 24
        return f"{hours:.0f} hours"


class FailureOracle:
    """
    Analyzes historical trends to predict hardware failures.
    Uses linear regression on time-series data.
    """

    # Thresholds where components are considered "failed" or critical
    FAILURE_THRESHOLDS = {
        "cpu": 95,      # thermal shutdown
        "gpu": 100,     # thermal shutdown
        "ssd": 80,      # critical temp
        "fan": 100,     # RPM too low for cooling
    }

    def __init__(self, db_path: str = "stability.db"):
        self.db_path = db_path

    def get_historical_data(self, sensor_type: str, days: int = 7) -> List[Tuple[datetime, float]]:
        """Retrieve historical readings for a sensor type."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        since = (datetime.now() - timedelta(days=days)).isoformat()

        cursor.execute("""
            SELECT timestamp, value, device_name, metric_name
            FROM sensor_readings
            WHERE sensor_type = ? AND timestamp > ?
            ORDER BY timestamp
        """, (sensor_type, since))

        results = cursor.fetchall()
        conn.close()

        # Group by specific sensor (device/metric combo)
        by_sensor: Dict[str, List[Tuple[datetime, float]]] = {}
        for ts_str, value, device, metric in results:
            key = f"{device}/{metric}"
            if key not in by_sensor:
                by_sensor[key] = []
            try:
                dt = datetime.fromisoformat(ts_str)
                by_sensor[key].append((dt, value))
            except ValueError:
                pass

        # Return the sensor with most data points (most active)
        if not by_sensor:
            return []
        return max(by_sensor.values(), key=len)

    def calculate_trend(self, data: List[Tuple[datetime, float]]) -> Optional[Tuple[float, float]]:
        """
        Calculate trend slope using simple linear regression.
        Returns (rate_per_day, confidence) or None.
        """
        if len(data) < 10:
            return None

        # Convert to days since first reading
        base_time = data[0][0]
        x = [(dt - base_time).total_seconds() / 86400 for dt, _ in data]  # days
        y = [val for _, val in data]

        n = len(x)
        mean_x = sum(x) / n
        mean_y = sum(y) / n

        # Calculate slope
        numerator = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
        denominator = sum((xi - mean_x) ** 2 for xi in x)

        if denominator == 0:
            return (0, 0)

        slope = numerator / denominator

        # Calculate R² for confidence
        ss_tot = sum((yi - mean_y) ** 2 for yi in y)
        predictions = [mean_y + slope * (xi - mean_x) for xi in x]
        ss_res = sum((yi - pred) ** 2 for yi, pred in zip(y, predictions))
        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

        return (slope, max(0, r_squared))

    def predict_failures(self, sensor_type: str) -> List[FailurePrediction]:
        """Generate failure predictions for all sensors of a type."""
        predictions = []
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        since = (datetime.now() - timedelta(days=7)).isoformat()
        cursor.execute("""
            SELECT DISTINCT device_name, metric_name, unit
            FROM sensor_readings
            WHERE sensor_type = ? AND timestamp > ?
        """, (sensor_type, since))

        sensors = cursor.fetchall()
        conn.close()

        threshold = self.FAILURE_THRESHOLDS.get(sensor_type, 100)

        for device, metric, unit in sensors:
            data = self.get_historical_data(sensor_type, days=7)
            # Filter to this specific sensor
            sensor_data = [(dt, val) for dt, val in data
                          if any(d[0] == device and d[1] == metric
                                for d in [(device, metric)])]

            # Actually need to re-query per sensor
            full_data = self._get_sensor_history(sensor_type, device, metric, 7)

            if len(full_data) < 5:
                continue

            trend_result = self.calculate_trend(full_data)
            if trend_result is None:
                continue

            rate, confidence = trend_result
            current = full_data[-1][1]

            # Only predict if trending toward failure
            if rate <= 0:
                continue

            # Days until threshold reached
            remaining = threshold - current
            if remaining <= 0:
                days_to_failure = 0
            else:
                days_to_failure = remaining / rate if rate > 0 else None

            pred = FailurePrediction(
                sensor_name=f"{device}/{metric}",
                current_value=current,
                unit=unit,
                trend_rate=rate,
                days_to_failure=days_to_failure,
                failure_threshold=threshold,
                confidence=confidence
            )
            predictions.append(pred)

        return sorted(predictions, key=lambda p: p.days_to_failure or 9999)

    def _get_sensor_history(self, sensor_type: str, device: str, metric: str,
                            days: int) -> List[Tuple[datetime, float]]:
        """Get history for a specific sensor."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        since = (datetime.now() - timedelta(days=days)).isoformat()
        cursor.execute("""
            SELECT timestamp, value
            FROM sensor_readings
            WHERE sensor_type = ? AND device_name = ?
            AND metric_name = ? AND timestamp > ?
            ORDER BY timestamp
        """, (sensor_type, device, metric, since))

        results = []
        for ts_str, value in cursor.fetchall():
            try:
                dt = datetime.fromisoformat(ts_str)
                results.append((dt, value))
            except ValueError:
                pass

        conn.close()
        return results


class DoomClockFormatter:
    """Human-friendly formatter for failure predictions."""

    def format_predictions(self, predictions: List[FailurePrediction]) -> str:
        """Format list of predictions as doom clock."""
        if not predictions:
            return "  🟢 No failure trends detected. System healthy."

        lines = ["", "  ⚠️  FAILURE PREDICTIONS (The Doom Clock)", "  " + "─" * 45]

        for pred in predictions[:5]:  # Top 5 most urgent
            urgency = self._urgency_emoji(pred.days_to_failure)
            countdown = pred.doom_countdown

            lines.append(f"  {urgency} {pred.sensor_name}")
            lines.append(f"     Current: {pred.current_value:.1f}{pred.unit} → {pred.failure_threshold}{pred.unit}")
            lines.append(f"     Rate: +{pred.trend_rate:.2f}{pred.unit}/day | Confidence: {pred.confidence:.0%}")
            lines.append(f"     ☠️  Doom in: {countdown}")
            lines.append("")

        return "\n".join(lines)

    def _urgency_emoji(self, days: Optional[float]) -> str:
        if days is None:
            return "⚪"
        if days <= 1:
            return "🔴"
        if days <= 7:
            return "🟠"
        if days <= 30:
            return "🟡"
        return "🟢"


class PeriodicLogger:
    """Background logging daemon."""

    def __init__(self, monitor: SystemMonitor, interval_minutes: int = 5):
        self.monitor = monitor
        self.interval = interval_minutes * 60
        self.oracle = FailureOracle()
        self.doom_formatter = DoomClockFormatter()
        self.running = False

    def run_once(self) -> HealthReport:
        """Single collection cycle."""
        return self.monitor.run(verbose=False)

    def analyze_and_report(self):
        """Run collection and show doom predictions."""
        report = self.run_once()

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Collection complete")
        print(f"  Stability: {report.overall_stability}% | Grade: {report.grade}")

        # Check for doom
        for sensor_type in ["cpu", "gpu", "ssd", "fan"]:
            predictions = self.oracle.predict_failures(sensor_type)
            if predictions:
                print(self.doom_formatter.format_predictions(predictions))

        return report

    def start_daemon(self):
        """Run continuously."""
        import time
        import signal

        self.running = True

        def stop(signum, frame):
            self.running = False
            print("\n👋 Stopping daemon...")

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

        print(f"🕐 Starting periodic logger (every {self.interval//60} min)")
        print("Press Ctrl+C to stop\n")

        while self.running:
            self.analyze_and_report()

            # Sleep with interrupt handling
            for _ in range(self.interval):
                if not self.running:
                    break
                time.sleep(1)


# ============ Main Entry ============

def main():
    """Run the stability monitor."""
    import sys

    monitor = SystemMonitor(
        storage=[
            SQLiteBackend("stability.db"),
            CSVBackend("stability.csv")
        ],
        history_size=60
    )

    # Check for daemon mode
    if "--daemon" in sys.argv or "-d" in sys.argv:
        interval = 5  # default 5 minutes
        for arg in sys.argv:
            if arg.startswith("--interval="):
                try:
                    interval = int(arg.split("=")[1])
                except ValueError:
                    pass
        logger = PeriodicLogger(monitor, interval_minutes=interval)
        logger.start_daemon()
    elif "--doom" in sys.argv:
        # Just show predictions
        oracle = FailureOracle()
        formatter = DoomClockFormatter()
        print("\n🔮 Analyzing historical data for failure predictions...\n")
        for sensor_type in ["cpu", "gpu", "ssd", "fan"]:
            preds = oracle.predict_failures(sensor_type)
            if preds:
                print(formatter.format_predictions(preds))
        print("\nTip: Run with --daemon to collect data over time for better predictions.")
    else:
        # Single run
        monitor.run()
        print("\n💡 Run with --daemon for continuous monitoring")
        print("   or --doom to see failure predictions")


if __name__ == "__main__":
    main()
