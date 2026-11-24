"""Remembered choices between sessions: delivery address and card suffix. Owner-only JSON."""
import json
import os
import tempfile
from pathlib import Path

from better_lavka_mcp import paths

PATH = paths.data_dir() / 'preferences.json'


def load():
    """The remembered choices; an absent or malformed file means none were saved.

    A symlinked or group/world-readable file is refused rather than read: it holds the address
    where the user gets deliveries."""
    target = Path(PATH)
    paths.prepare_parent(target, create=False)
    try:
        raw = paths.read_private_bytes(target, 'Preferences file')
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def update(**values):
    """Merge values into the preferences file atomically; None removes a key."""
    target = Path(PATH)
    paths.prepare_parent(target)
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
