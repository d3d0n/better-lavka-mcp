import json
import stat

import httpx
import pytest


@pytest.mark.asyncio
async def test_save_uz_session_round_trips_cookies_without_domain_widening(tmp_path, monkeypatch):
    from better_lavka_mcp import client
    from better_lavka_mcp.auth.qr import save_session
    async with httpx.AsyncClient() as http:
        http.cookies.set('Session_id', 'sensitive', domain='.yandex.uz', path='/')
        http.cookies.set('ru_only', 'sensitive', domain='.yandex.ru', path='/')
        target = tmp_path / 'state.json'
        save_session(http, target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    monkeypatch.setattr(client, 'STATE', str(target))
    restored = client.create_http()
    assert 'Session_id=sensitive' in restored.build_request('GET', 'https://lavka.yandex.uz/').headers['cookie']
    assert 'ru_only' not in restored.build_request('GET', 'https://lavka.yandex.uz/').headers['cookie']
    assert 'Session_id' not in restored.build_request('GET', 'https://yandex.ru/').headers['cookie']
    await restored.aclose()


def _saved_session(tmp_path, monkeypatch):
    from better_lavka_mcp import client
    from better_lavka_mcp.auth.qr import save_session
    http = httpx.AsyncClient()
    http.cookies.set('Session_id', 'login', domain='.yandex.uz', path='/')
    http.cookies.set('lavka__session', 'old', domain='lavka.yandex.uz', path='/')
    target = tmp_path / 'state.json'
    save_session(http, target)
    monkeypatch.setattr(client, 'STATE', str(target))
    return client, target


def _values(target):
    return {c['name']: c['value'] for c in json.loads(target.read_text())['cookies']}


@pytest.mark.asyncio
async def test_cookies_rotated_by_lavka_are_saved_when_the_call_ends(tmp_path, monkeypatch):
    client, target = _saved_session(tmp_path, monkeypatch)
    async with client.create_http() as http:
        http.cookies.set('lavka__session', 'new', domain='lavka.yandex.uz', path='/')
    assert _values(target) == {'Session_id': 'login', 'lavka__session': 'new'}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_rotation_never_overwrites_a_new_login_or_drops_the_login_cookie(tmp_path, monkeypatch):
    client, target = _saved_session(tmp_path, monkeypatch)
    async with client.create_http() as http:
        http.cookies.set('lavka__session', 'stale', domain='lavka.yandex.uz', path='/')
        target.write_text(json.dumps({'cookies': []}))  # a QR login finished meanwhile
    assert json.loads(target.read_text()) == {'cookies': []}
    client, target = _saved_session(tmp_path, monkeypatch)
    async with client.create_http() as http:
        http.cookies.delete('Session_id', domain='.yandex.uz', path='/')
        http.cookies.set('lavka__session', 'new', domain='lavka.yandex.uz', path='/')
    assert _values(target) == {'Session_id': 'login', 'lavka__session': 'old'}
