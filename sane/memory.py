import json
from .config import commands_file

def load_data():
    if commands_file.exists():
        with open(commands_file) as f:
            return json.load(f)
    return {}

def save_data(commands):
    temp = commands_file.with_suffix(".tmp")
    with open(temp,"w") as f:
        json.dump(commands,f,indent=2)
    temp.replace(commands_file)