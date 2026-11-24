from pathlib import Path

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
