import contextlib
import json
import os
from pathlib import Path

import pytest


def _write_session(path, **cookies):
    """A private saved session with the given cookies, the way save_session writes it."""
    path.write_text(json.dumps({'cookies': [
        {'name': name, 'value': value, 'domain': '.yandex.uz', 'path': '/'} for name, value in cookies.items()
    ]}))
    path.chmod(0o600)
    return path


class _Manager:
    """Stages a new session file the way QRManager does, without any network."""

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


async def _verified(path):
    return True


@pytest.mark.asyncio
async def test_start_hides_qr_link_and_prevents_existing_session_replacement(tmp_path):
    from better_lavka_mcp.auth.tools import QRLogin
    session = _write_session(tmp_path / 'session.json', Session_id='existing')
    login = QRLogin(session, _Manager(tmp_path / 'staged', {'Session_id': 'new'}))
    assert (await login.start())['status'] == 'replacement_requires_approval'
    result = await login.start(replace_existing=True)
    assert result['status'] == 'pending'
    assert result['qr_url'] == 'SECRET'


@pytest.mark.asyncio
async def test_verified_session_is_saved_and_status_checked(tmp_path):
    from better_lavka_mcp.auth.tools import QRLogin
    session = tmp_path / 'session'
    login = QRLogin(session, _Manager(tmp_path / 'staged', {'Session_id': 'new'}), _verified)
    await login.start()
    assert (await login.status())['status'] == 'authenticated'
    assert json.loads(session.read_text())['cookies'][0]['value'] == 'new'
    assert (await login.status())['status'] == 'authenticated'


@pytest.mark.asyncio
async def test_concurrent_session_change_is_not_overwritten(tmp_path):
    from better_lavka_mcp.auth.tools import QRLogin
    session = tmp_path / 'session'
    login = QRLogin(session, _Manager(tmp_path / 'staged', {'Session_id': 'new'}), _verified)
    await login.start()
    _write_session(session, Session_id='other')
    assert (await login.status())['status'] == 'session_changed'
    assert json.loads(session.read_text())['cookies'][0]['value'] == 'other'


@pytest.mark.asyncio
async def test_rotated_service_cookie_does_not_fail_a_finished_login(tmp_path):
    """A normal tool call saving a rotated service cookie changes bytes, not the account."""
    from better_lavka_mcp.auth.tools import QRLogin
    session = _write_session(tmp_path / 'session', Session_id='a', lavka__session='one')
    login = QRLogin(session, _Manager(tmp_path / 'staged', {'Session_id': 'b'}), _verified)
    await login.start(replace_existing=True)
    _write_session(session, Session_id='a', lavka__session='rotated')  # another call saved back
    assert (await login.status())['status'] == 'authenticated'
    assert json.loads(session.read_text())['cookies'][0]['value'] == 'b'


@pytest.mark.asyncio
async def test_promotion_compares_and_replaces_under_the_session_lock(tmp_path, monkeypatch):
    from better_lavka_mcp import paths
    from better_lavka_mcp.auth.tools import QRLogin
    entered = []
    real = paths.session_lock

    @contextlib.contextmanager
    def recording(path, *args, **kwargs):
        entered.append(Path(path))
        with real(path, *args, **kwargs):
            yield

    monkeypatch.setattr(paths, 'session_lock', recording)
    session = _write_session(tmp_path / 'session', Session_id='a')
    login = QRLogin(session, _Manager(tmp_path / 'staged', {'Session_id': 'b'}), _verified)
    await login.start(replace_existing=True)
    assert (await login.status())['status'] == 'authenticated'
    assert entered == [session]


@pytest.mark.asyncio
async def test_another_account_forgets_the_remembered_address_and_card(tmp_path):
    from better_lavka_mcp import app, preferences
    from better_lavka_mcp.auth.tools import QRLogin
    remembered = {'address_id': 'a-home', 'location': [69.2, 41.3]}
    session = _write_session(tmp_path / 'session', Session_id='a')
    app._remember_delivery(remembered)
    preferences.update(address=remembered, card_last4='1111')
    login = QRLogin(session, _Manager(tmp_path / 'staged', {'Session_id': 'b'}),
                    _verified, account_changed=app._forget_account_choices)
    await login.start(replace_existing=True)
    assert (await login.status())['status'] == 'authenticated'
    assert app._delivery is None
    assert preferences.load() == {}


@pytest.mark.asyncio
async def test_relogin_of_the_same_account_keeps_the_remembered_choices(tmp_path):
    from better_lavka_mcp import app, preferences
    from better_lavka_mcp.auth.tools import QRLogin
    remembered = {'address_id': 'a-home', 'location': [69.2, 41.3]}
    session = _write_session(tmp_path / 'session', Session_id='a', yandex_login='alice')
    app._remember_delivery(remembered)
    preferences.update(address=remembered, card_last4='1111')
    # Yandex reissues Session_id on every login; yandex_login still names the same account.
    login = QRLogin(session, _Manager(tmp_path / 'staged', {'Session_id': 'a2', 'yandex_login': 'alice'}),
                    _verified, account_changed=app._forget_account_choices)
    await login.start(replace_existing=True)
    assert (await login.status())['status'] == 'authenticated'
    assert app._delivery == remembered
    assert preferences.load()['card_last4'] == '1111'


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == 'nt', reason='POSIX file modes')
async def test_shared_session_file_is_refused_with_a_fix_instruction(tmp_path):
    from better_lavka_mcp.auth.tools import QRLogin
    session = _write_session(tmp_path / 'session', Session_id='a')
    session.chmod(0o644)
    with pytest.raises(ValueError, match='chmod 600'):
        await QRLogin(session, _Manager(tmp_path / 'staged', {'Session_id': 'b'})).start(replace_existing=True)


@pytest.mark.asyncio
async def test_symlinked_session_file_is_refused(tmp_path):
    from better_lavka_mcp.auth.tools import QRLogin
    target = _write_session(tmp_path / 'elsewhere', Session_id='a')
    session = tmp_path / 'session'
    session.symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        await QRLogin(session, _Manager(tmp_path / 'staged', {'Session_id': 'b'}), _verified).status()


@pytest.mark.asyncio
async def test_registered_tools_are_discoverable(tmp_path):
    from mcp.server.fastmcp import FastMCP

    from better_lavka_mcp.auth.tools import register_qr_tools
    server = FastMCP('test')
    login = register_qr_tools(server, tmp_path / 'session')
    names = {tool.name for tool in await server.list_tools()}
    assert 'login' in names and not names & {'start_login', 'get_login_status', 'cancel_login'}
    assert login.account_changed is not None  # another account drops the remembered address and card
