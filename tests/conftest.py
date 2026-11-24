import pytest


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    """Keep every test away from the real session file and remembered choices."""
    from better_lavka_mcp import aliases, app, client, preferences
    from better_lavka_mcp.orders import diagnostics
    monkeypatch.setattr(client, 'STATE', str(tmp_path / 'session.json'))
    monkeypatch.setattr(preferences, 'PATH', tmp_path / 'preferences.json')
    monkeypatch.setattr(aliases, 'PATH', tmp_path / 'aliases.sqlite')
    monkeypatch.setattr(diagnostics, 'PATH', tmp_path / 'checkout-diagnostics.jsonl')
    monkeypatch.setattr(app, '_delivery', None)
