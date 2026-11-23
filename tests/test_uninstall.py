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
