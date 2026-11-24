"""Where the server keeps its private data on this computer.

macOS and Linux keep the historical ``~/.local/share/yandex-lavka-mcp`` folder so
existing installs find their session and guards. Windows has no such convention,
so it uses ``%LOCALAPPDATA%\\better-lavka-mcp``.
"""
import os
from pathlib import Path

WINDOWS_DIR_NAME = 'better-lavka-mcp'


def data_dir(system=None, environ=None):
    """Platform-private data directory; tests pass an explicit system and environ."""
    system = os.name if system is None else system
    environ = os.environ if environ is None else environ
    if system == 'nt':
        base = environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
        return Path(base) / WINDOWS_DIR_NAME
    return Path.home() / '.local/share/yandex-lavka-mcp'


def session_file():
    """Saved Lavka cookies; YANDEX_SESSION_FILE overrides the default location."""
    return os.path.expanduser(os.environ.get('YANDEX_SESSION_FILE', str(data_dir() / 'session.json')))
