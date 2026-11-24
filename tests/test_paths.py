import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from better_lavka_mcp import paths


def test_windows_uses_local_appdata():
    environ = {'LOCALAPPDATA': r'C:\Users\alice\AppData\Local'}
    assert paths.data_dir(system='nt', environ=environ) == Path(environ['LOCALAPPDATA']) / 'better-lavka-mcp'


def test_windows_without_local_appdata_falls_back_to_the_user_profile():
    assert paths.data_dir(system='nt', environ={}) == Path.home() / 'AppData' / 'Local' / 'better-lavka-mcp'


def test_posix_keeps_the_existing_share_folder():
    assert paths.data_dir(system='posix', environ={}) == Path.home() / '.local/share/yandex-lavka-mcp'


def test_session_file_defaults_to_the_data_directory_and_honours_the_override(tmp_path, monkeypatch):
    monkeypatch.delenv('YANDEX_SESSION_FILE', raising=False)
    assert paths.session_file() == str(paths.data_dir() / 'session.json')
    monkeypatch.setenv('YANDEX_SESSION_FILE', str(tmp_path / 'custom.json'))
    assert paths.session_file() == str(tmp_path / 'custom.json')


def test_windows_accepts_files_that_posix_rejects_as_shared(tmp_path):
    shared = tmp_path / 'previews.sqlite'
    shared.write_text('x')
    shared.chmod(0o644)
    info = os.stat(shared)
    # On Windows only the file type matters; %LOCALAPPDATA% is per-user.
    assert paths.private_file(info, system='nt')
    assert not paths.private_dir(info, system='nt')
    if os.name == 'nt':
        pytest.skip('POSIX ownership needs os.getuid')
    assert not paths.private_file(info, system='posix')
    shared.chmod(0o600)
    assert paths.private_file(os.stat(shared), system='posix')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file modes')
def test_shared_session_file_bytes_are_refused(tmp_path):
    session = tmp_path / 'session.json'
    session.write_text('{}')
    session.chmod(0o644)
    with pytest.raises(ValueError, match='chmod 600'):
        paths.read_private_bytes(session, 'Saved session file')
    session.chmod(0o600)
    assert paths.read_private_bytes(session, 'Saved session file') == b'{}'


def test_symlinked_private_file_is_refused(tmp_path):
    target = tmp_path / 'real'
    target.write_text('secret')
    target.chmod(0o600)
    link = tmp_path / 'link'
    link.symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        paths.read_private_bytes(link, 'Saved session file')


def test_session_lock_is_exclusive_and_kept_for_the_next_writer(tmp_path):
    session = tmp_path / 'session.json'
    lock = tmp_path / 'session.json.lock'
    with paths.session_lock(session):
        assert not session.exists()  # the session inode is replaced; it is never the lock
        assert lock.is_file()
        if os.name != 'nt':
            assert stat.S_IMODE(lock.stat().st_mode) == 0o600
            import fcntl
            fd = os.open(lock, os.O_RDWR)
            try:
                with pytest.raises(OSError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
    assert lock.is_file()  # never unlinked: another process may already hold it open
    with paths.session_lock(session):
        pass


@pytest.mark.skipif(os.name == 'nt', reason='POSIX modes and symlinks')
def test_existing_shared_data_directory_is_refused_by_every_private_store(tmp_path):
    home = tmp_path / 'home'
    directory = home / '.local/share/yandex-lavka-mcp'
    directory.mkdir(parents=True)
    directory.chmod(0o755)
    script = (
        "from better_lavka_mcp import aliases, client, preferences\n"
        "from better_lavka_mcp.auth import qr\n"
        "class Jar:\n"
        "    jar = []\n"
        "class Fake:\n"
        "    cookies = Jar\n"
        f"folder = __import__('pathlib').Path({str(directory)!r})\n"
        "for call in (client.create_http, preferences.load, lambda: aliases.short('product'),\n"
        "             lambda: qr.write_qr_image('https://passport.yandex.uz/q', folder / 'qr.png'),\n"
        "             lambda: qr.save_session(Fake, folder / 'session.json')):\n"
        "    try:\n"
        "        call()\n"
        "    except ValueError as error:\n"
        "        assert 'chmod 700' in str(error), error\n"
        "    else:\n"
        "        raise AssertionError('shared data directory accepted')\n"
    )
    env = os.environ.copy()
    env.update(HOME=str(home))
    env.pop('YANDEX_SESSION_FILE', None)
    subprocess.run([sys.executable, '-c', script], env=env, check=True)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755  # refused, not repaired
    assert list(directory.iterdir()) == []


@pytest.mark.skipif(os.name == 'nt', reason='POSIX modes and symlinks')
def test_alias_database_is_never_chmodded_through_a_symlink(tmp_path, monkeypatch):
    from better_lavka_mcp import aliases
    victim = tmp_path / 'victim.sqlite'
    victim.write_text('keep')
    victim.chmod(0o644)
    (tmp_path / 'aliases.sqlite').symlink_to(victim)
    monkeypatch.setattr(aliases, 'PATH', tmp_path / 'aliases.sqlite')
    with pytest.raises(ValueError, match='symlink'):
        aliases.short('7acc5006e3dd4636bb613646099df8a5000400020001')
    assert victim.read_text() == 'keep'
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_alias_database_is_created_private(tmp_path, monkeypatch):
    from better_lavka_mcp import aliases
    target = tmp_path / 'aliases.sqlite'
    monkeypatch.setattr(aliases, 'PATH', target)
    assert len(aliases.short('7acc5006e3dd4636bb613646099df8a5000400020001').split()) == 3
    if os.name != 'nt':
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file modes')
def test_shared_preferences_are_refused(tmp_path, monkeypatch):
    from better_lavka_mcp import preferences
    target = tmp_path / 'preferences.json'
    target.write_text(json.dumps({'address': {'location': [69.2, 41.3]}}))
    target.chmod(0o644)
    monkeypatch.setattr(preferences, 'PATH', target)
    with pytest.raises(ValueError, match='chmod 600'):
        preferences.load()


def test_symlinked_preferences_are_refused(tmp_path, monkeypatch):
    from better_lavka_mcp import preferences
    target = tmp_path / 'elsewhere.json'
    target.write_text(json.dumps({'card_last4': '1234'}))
    target.chmod(0o600)
    link = tmp_path / 'preferences.json'
    link.symlink_to(target)
    monkeypatch.setattr(preferences, 'PATH', link)
    with pytest.raises(ValueError, match='symlink'):
        preferences.load()


def test_preferences_are_written_owner_only(tmp_path, monkeypatch):
    from better_lavka_mcp import preferences
    target = tmp_path / 'preferences.json'
    monkeypatch.setattr(preferences, 'PATH', target)
    preferences.update(card_last4='1234')
    assert preferences.load() == {'card_last4': '1234'}
    if os.name != 'nt':
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
