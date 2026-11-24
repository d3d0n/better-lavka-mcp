"""Where the server keeps its private data on this computer.

macOS and Linux keep the historical ``~/.local/share/yandex-lavka-mcp`` folder so
existing installs find their session and guards. Windows has no such convention,
so it uses ``%LOCALAPPDATA%\\better-lavka-mcp``.

Everything the server stores is a single account's: the session cookies, the remembered
address and the short ids. The helpers here create such state owner-only and refuse to
use an existing folder or file that other users can read, that a symlink hides, or that
belongs to somebody else.
"""
import contextlib
import errno
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


def _posix(system=None):
    return (os.name if system is None else system) != 'nt'


def _unsafe_file(what, path, system=None):
    hint = f'fix its permissions with chmod 600 {path} or delete it and sign in again' if _posix(system) else 'move it aside and sign in again'
    return f'{what} must be a regular file that is not a symlink and only you can read: {path}; {hint}'


def _unsafe_dir(path, system=None):
    hint = f'; fix its permissions with chmod 700 {path}' if _posix(system) else ''
    return f'{path} must be a directory that is not a symlink and only you can use{hint}'


def ensure_private_dir(directory, system=None, create=True):
    """An existing owner-only directory, or a fresh one; returns the Path.

    A symlink, another user's directory and a group/world-usable directory are refused
    instead of being repaired: chmod through a symlink would hand the target to a third party."""
    directory = Path(directory)
    try:
        info = directory.lstat()
    except FileNotFoundError:
        if not create:
            return directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
    if not private_dir(info, system):
        raise ValueError(_unsafe_dir(directory, system))
    return directory


def prepare_parent(path, system=None, create=True):
    """Make the file's parent usable for private state.

    The server's own data directory must already be private; a YANDEX_SESSION_FILE override
    may point into an ordinary folder (a home directory, a mounted volume) that is not ours
    to restrict, so only a missing parent is created there."""
    parent = Path(path).expanduser().parent
    default = data_dir()
    if parent == default or default in parent.parents:
        return ensure_private_dir(parent, system, create=create)
    if create:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return parent


def read_private_bytes(path, what='File', system=None):
    """Bytes of an owner-only regular file, never following a symlink (FileNotFoundError when absent).

    O_NOFOLLOW closes the window between the check and the read; the descriptor's own
    metadata is checked again so a swapped-in file is still refused."""
    target = Path(path)
    if target.is_symlink():
        raise ValueError(_unsafe_file(what, target, system))
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise ValueError(_unsafe_file(what, target, system)) from None
        raise
    try:
        if not private_file(os.fstat(fd), system):
            raise ValueError(_unsafe_file(what, target, system))
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b''.join(chunks)
    finally:
        os.close(fd)


@contextlib.contextmanager
def session_lock(session_path, system=None):
    """Cross-process exclusive lock for the read-compare-replace of the saved session.

    Every process (a cookie rotation saving back, a QR login promoting a fresh account) has to
    compare and then replace the session in one step; without a shared lock the slower one wins
    and a stale save-back can erase a fresh login.

    The lock lives in a sibling file that is created once and never replaced or removed: a lock
    on the session file itself would guard only the inode another process already swapped out.
    The sibling is opened with O_NOFOLLOW so a planted symlink cannot redirect the lock, and the
    resolved parent is used so every process agrees on one path."""
    target = Path(session_path).expanduser()
    prepare_parent(target)
    lock = target.parent.resolve() / (target.name + '.lock')
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if lock.is_symlink():
        raise ValueError(f'Session lock file must not be a symlink: {lock}')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise ValueError(f'Session lock file must not be a symlink: {lock}') from None
        raise
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'\0')
        os.lseek(fd, 0, os.SEEK_SET)
        if _posix(system):
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # ty: ignore[missing-attribute]
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # ty: ignore[missing-attribute]
    finally:
        os.close(fd)
