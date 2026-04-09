"""Configuration for sane package."""
from pathlib import Path

# Base directory for data storage
DATA_DIR = Path.home() / ".config" / "sane"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Commands storage file
commands_file = DATA_DIR / "commands.json"
