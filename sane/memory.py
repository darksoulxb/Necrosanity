"""Session state persistence for Necrosanity."""
import json
from .config import DATA_DIR

_state_file = DATA_DIR / "session.json"


def load_state() -> dict:
    """Load persisted session state (e.g. user prefs, last run info)."""
    if _state_file.exists():
        try:
            with open(_state_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_state(state: dict) -> None:
    """Atomically save session state."""
    tmp = _state_file.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(_state_file)


def clear_state() -> None:
    """Wipe persisted state."""
    if _state_file.exists():
        _state_file.unlink()
