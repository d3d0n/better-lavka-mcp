import pytest


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    """Keep every test away from the real session file and remembered choices."""
    from better_lavka_mcp import aliases, preferences, server
    monkeypatch.setattr(server, 'STATE', str(tmp_path / 'session.json'))
    monkeypatch.setattr(preferences, 'PATH', tmp_path / 'preferences.json')
    monkeypatch.setattr(aliases, 'PATH', tmp_path / 'aliases.json')
    aliases.reset()
    monkeypatch.setattr(server, '_delivery', None)
    yield
    aliases.reset()
