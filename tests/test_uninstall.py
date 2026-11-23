import pytest

from better_lavka_mcp import uninstall


@pytest.fixture
def data(tmp_path, monkeypatch):
    state = tmp_path / 'share' / 'yandex-lavka-mcp'
    state.mkdir(parents=True)
    for name in ('session.json', 'preferences.json', 'aliases.sqlite', 'order-previews.sqlite'):
        (state / name).write_text('x')
    outside = tmp_path / 'elsewhere' / 'cookies.json'
    outside.parent.mkdir()
    outside.write_text('x')
    monkeypatch.setattr(uninstall, 'STATE_DIR', state)
    monkeypatch.setenv('YANDEX_SESSION_FILE', str(outside))
    return state, outside


def test_uninstall_deletes_all_local_data_including_a_custom_session_file(data, capsys):
    state, outside = data
    assert uninstall.run(['--yes']) == 0
    assert not state.exists() and not outside.exists()
    out = capsys.readouterr().out
    assert 'Deleted.' in out and 'uv cache clean better-lavka-mcp' in out and 'claude mcp remove lavka' in out


def test_uninstall_without_yes_deletes_nothing_when_it_cannot_ask(data, monkeypatch, capsys):
    state, outside = data
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    assert uninstall.run([]) == 1
    assert state.exists() and outside.exists()
    assert '--yes' in capsys.readouterr().out


def test_uninstall_with_nothing_stored(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(uninstall, 'STATE_DIR', tmp_path / 'missing')
    monkeypatch.delenv('YANDEX_SESSION_FILE', raising=False)
    assert uninstall.run(['--yes']) == 0
    assert 'Nothing to delete' in capsys.readouterr().out


def test_a_directory_in_yandex_session_file_is_never_deleted(data, tmp_path, monkeypatch, capsys):
    state, _ = data
    home = tmp_path / 'home'
    (home / 'photos').mkdir(parents=True)
    (home / 'photos' / 'cat.jpg').write_text('keep me')
    monkeypatch.setenv('YANDEX_SESSION_FILE', str(home))
    assert uninstall.run(['--yes']) == 0
    assert (home / 'photos' / 'cat.jpg').read_text() == 'keep me'
    assert not state.exists()
    assert 'not a file' in capsys.readouterr().out


def test_a_symlinked_session_file_removes_only_the_link(data, tmp_path, monkeypatch):
    target = tmp_path / 'real-cookies.json'
    target.write_text('keep me')
    link = tmp_path / 'link.json'
    link.symlink_to(target)
    monkeypatch.setenv('YANDEX_SESSION_FILE', str(link))
    assert uninstall.run(['--yes']) == 0
    assert not link.is_symlink() and target.read_text() == 'keep me'


def test_a_symlinked_data_folder_removes_only_the_link(tmp_path, monkeypatch):
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'important.txt').write_text('keep me')
    link = tmp_path / 'yandex-lavka-mcp'
    link.symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setattr(uninstall, 'STATE_DIR', link)
    monkeypatch.delenv('YANDEX_SESSION_FILE', raising=False)
    assert uninstall.run(['--yes']) == 0
    assert not link.is_symlink() and (elsewhere / 'important.txt').read_text() == 'keep me'
