import contextlib
import json
import stat
from pathlib import Path

import httpx
import pytest


def _write_session(target, **cookies):
    """Simulate another process saving a session: private, valid JSON."""
    target.write_text(json.dumps({'cookies': [
        {'name': name, 'value': value, 'domain': '.yandex.uz', 'path': '/'} for name, value in cookies.items()
    ]}))
    target.chmod(0o600)
    return target


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
        _write_session(target, Session_id='other')  # a QR login finished meanwhile
    assert _values(target) == {'Session_id': 'other'}
    client, target = _saved_session(tmp_path, monkeypatch)
    async with client.create_http() as http:
        http.cookies.delete('Session_id', domain='.yandex.uz', path='/')
        http.cookies.set('lavka__session', 'new', domain='lavka.yandex.uz', path='/')
    assert _values(target) == {'Session_id': 'login', 'lavka__session': 'old'}


@pytest.mark.asyncio
async def test_a_stale_rotation_keeps_another_account_that_logged_in_meanwhile(tmp_path, monkeypatch):
    """A service-cookie rotation in this process must not resurrect the previous account's login."""
    client, target = _saved_session(tmp_path, monkeypatch)
    async with client.create_http() as http:
        http.cookies.set('lavka__session', 'stale', domain='lavka.yandex.uz', path='/')
        _write_session(target, Session_id='other', lavka__session='fresh')
    assert _values(target) == {'Session_id': 'other', 'lavka__session': 'fresh'}


@pytest.mark.asyncio
async def test_rotation_saves_back_under_the_session_lock(tmp_path, monkeypatch):
    from better_lavka_mcp import paths
    entered = []
    real = paths.session_lock

    @contextlib.contextmanager
    def recording(path, *args, **kwargs):
        entered.append(Path(path))
        with real(path, *args, **kwargs):
            yield

    monkeypatch.setattr(paths, 'session_lock', recording)
    client, target = _saved_session(tmp_path, monkeypatch)
    async with client.create_http() as http:
        http.cookies.set('lavka__session', 'new', domain='lavka.yandex.uz', path='/')
    assert _values(target)['lavka__session'] == 'new'
    assert entered == [target]


class _Manager:
    """Stages a verified session the way QRManager does, without any network."""

    def __init__(self, staged, cookies):
        self.auth = None
        self.session_path = staged
        self.cookies = cookies

    async def start(self):
        self.auth = object()
        return {'qr_image': 'local.png', 'qr_url': 'SECRET'}

    async def poll(self):
        _write_session(self.session_path, **self.cookies)
        self.auth = None
        return {'status': 'session_saved_not_verified_by_lavka'}

    async def close(self):
        self.auth = None


@pytest.mark.asyncio
async def test_a_rotation_during_a_pending_login_does_not_fail_it(tmp_path, monkeypatch):
    """Finding: a tool call saving a rotated service cookie used to turn a good re-login into session_changed."""
    from better_lavka_mcp.auth.tools import QRLogin

    async def verified(path):
        return True

    client, target = _saved_session(tmp_path, monkeypatch)  # Session_id=login, lavka__session=old
    login = QRLogin(target, _Manager(tmp_path / 'staged', {'Session_id': 'login'}), verified)
    await login.start(replace_existing=True)
    async with client.create_http() as http:
        http.cookies.set('lavka__session', 'rotated', domain='lavka.yandex.uz', path='/')
    assert _values(target)['lavka__session'] == 'rotated'
    assert (await login.status())['status'] == 'authenticated'
    assert _values(target) == {'Session_id': 'login'}
