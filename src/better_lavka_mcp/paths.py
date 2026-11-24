"""Where the server keeps its private data on this computer.

macOS and Linux keep the historical ``~/.local/share/yandex-lavka-mcp`` folder so
existing installs find their session and guards. Windows has no such convention,
so it uses ``%LOCALAPPDATA%\\better-lavka-mcp``.
"""
import os
import stat
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


def private_dir(info, system=None):
    """A directory only this user can use. Windows has no POSIX modes; %LOCALAPPDATA% is per-user already."""
    if (os.name if system is None else system) == 'nt':
        return stat.S_ISDIR(info.st_mode)
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700


def private_file(info, system=None):
    """A regular file only this user can read or write (on Windows: just a regular file, see private_dir)."""
    if (os.name if system is None else system) == 'nt':
        return stat.S_ISREG(info.st_mode)
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
