"""Configuration for Necrosanity."""
from pathlib import Path

# Base directory for all Necrosanity data
DATA_DIR = Path.home() / ".local" / "share" / "necrosanity"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Storage paths
DB_PATH  = DATA_DIR / "stability.db"
CSV_PATH = DATA_DIR / "stability.csv"
LOG_PATH = DATA_DIR / "necrosanity.log"
