# SANE - System Analytics & Notification Engine

A hardware monitoring utility with btop-inspired TUI, SQLite/CSV/TXT logging, and sanity scoring.

## Features

- **Real-time TUI** with gradient bars, color-coded panels, and Braille spinners
- **Multi-format logging**: SQLite (structured), CSV, TXT (human-readable)
- **Sanity scoring**: 0-100% health score based on CPU, GPU, SSD, fans, power, clocks
- **Keyboard controls**: `q` quit, `l` toggle logging, `c` clear logs
- **Sensors**: CPU/GPU temps, fan RPM, power draw (Intel RAPL, NVIDIA), clock frequencies, throttling flags, ECC memory errors, storage health

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### TUI Mode (real-time dashboard)

```bash
cd sane
python3 main1.py
```

### CLI Mode (one-shot logging)

```bash
cd sane
python3 main.py
```

## Structure


```
Necrosanity/
├── requirements.txt
└── sane/
    ├── __init__.py      # package exports
    ├── config.py        # data paths (~/.local/share/necrosanity/)
    ├── main.py          # core: collectors, stability engine, storage, CLI
    ├── main1.py         # rich TUI — real-time dashboard
    ├── memory.py        # session state persistence
    └── method.md        # design notes
```

---

## Installation

```bash
pip install -r requirements.txt
```

System dependencies (optional, enables more sensors):

```bash
# Arch / CachyOS
sudo pacman -S lm_sensors smartmontools nvme-cli

# Debian / Ubuntu
sudo apt install lm-sensors smartmontools nvme-cli
```

---

## Usage

### TUI — real-time dashboard

```bash
python3 -m sane.main1
```

Keybinds: `q` quit · `l` toggle logging · `c` clear logs

### CLI — one-shot report

```bash
python3 -m sane.main
```

Flags: `--daemon` continuous logging · `--interval=N` minutes · `--doom` failure predictions

---

## Sensors

| Source | Data |
|---|---|
| `lm-sensors` | CPU temps, fan RPM |
| `nvidia-smi` | GPU temp, utilization, power draw |
| sysfs `/sys/class/drm` | AMD GPU temp |
| `smartctl` + `nvme` | NVMe SSD temp |

---

## Sanity Score

0–100 score calculated per sensor, averaged across all active sensors.

| Condition | Penalty |
|---|---|
| Volatility (noisy signal) | up to −30 |
| Spiking or rising trend | −15 |
| Warning threshold crossed | −10 |
| Critical threshold crossed | −30 |

Grades: `A+` ≥95 · `A` ≥90 · `B+` ≥85 · `B` ≥80 · `C` ≥70 · `D` ≥60 · `F` below

---

## Data Storage

All data is written to `~/.local/share/necrosanity/`:

| File | Contents |
|---|---|
| `stability.db` | SQLite — full sensor history with trend + volatility |
| `stability.csv` | CSV — same data, human-readable |
| `session.json` | Session state (prefs, last run) |

---

## License

MIT
```

## Requirements

- `lm-sensors` - CPU temps and fans
- `smartmontools` - SSD/HDD health
- `nvme-cli` - NVMe SSD data
- `nvidia-smi` - NVIDIA GPU (optional)

## License

MIT
