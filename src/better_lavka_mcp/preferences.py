"""Remembered choices between sessions: delivery address and card suffix. Owner-only JSON."""
import json
import os
import tempfile
from pathlib import Path

from better_lavka_mcp import paths

PATH = paths.data_dir() / 'preferences.json'


def load():
    try:
        data = json.loads(Path(PATH).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update(**values):
    """Merge values into the preferences file atomically; None removes a key."""
    target = Path(PATH)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = load()
    for key, value in values.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    fd, temporary = tempfile.mkstemp(prefix='.lavka-preferences-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return data
