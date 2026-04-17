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
sane/
├── __init__.py      # Package exports
├── config.py        # Data directory paths
├── main.py          # CLI logging tool
├── main1.py         # TUI with rich/btop styling
├── memory.py        # JSON persistence helpers
└── method.md        # Design notes
```

## Requirements

- `lm-sensors` - CPU temps and fans
- `smartmontools` - SSD/HDD health
- `nvme-cli` - NVMe SSD data
- `nvidia-smi` - NVIDIA GPU (optional)

## License

MIT
