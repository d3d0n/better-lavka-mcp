import httpx
import pytest

from better_lavka_mcp import app, client
from better_lavka_mcp.orders import submit as orders
from better_lavka_mcp.tools import account

# The real factory: it loads the saved session file, like every tool does.
REAL_CREATE_HTTP = client.create_http
ANONYMOUS = httpx.AsyncClient()  # no login cookies: the account is unknown, nothing to compare

# Synthetic saved addresses shaped like get-favorite-addresses on lavka.yandex.uz.
ENTRIES = [
    {'addressId': 'a-home', 'address': {'label': 'Дом', 'shortAddress': 'Example street, 1', 'location': [69.2, 41.3], 'flat': 'private'}},
    {'addressId': 'a-work', 'address': {'label': 'Работа', 'shortAddress': 'Sample avenue, 7', 'location': [69.3, 41.2]}},
    {'addressId': 'a-mom', 'address': {'label': '', 'shortAddress': 'Sample avenue, 9', 'location': [69.4, 41.1]}},
]


@pytest.fixture
def saved(monkeypatch):
    async def addresses(http):
        return None, ENTRIES
    monkeypatch.setattr(orders, 'addresses', addresses)
    monkeypatch.setattr(client, 'create_http', lambda: httpx.AsyncClient())
    monkeypatch.setattr(app, '_delivery', None)


@pytest.mark.asyncio
async def test_use_address_matches_name_alias_street_or_index(saved):
    assert (await account.use_address('home'))['label'] == 'Дом'
    assert app._location(ANONYMOUS) == [69.2, 41.3]
    assert (await account.use_address('работа'))['index'] == 1
    assert (await account.use_address('avenue, 9'))['index'] == 2
    assert (await account.use_address('0'))['label'] == 'Дом'
    # "1" is an index, not the house number in "Example street, 1".
    assert (await account.use_address('1'))['label'] == 'Работа'
    assert 'private' not in str(await account.use_address('дом'))


@pytest.mark.asyncio
async def test_chosen_address_is_remembered_for_the_next_session(saved, monkeypatch):
    from better_lavka_mcp import preferences
    await account.use_address('home')
    monkeypatch.setattr(app, '_delivery', None)  # a fresh server process
    assert app._location(ANONYMOUS) == [69.2, 41.3]
    assert app._current_delivery(ANONYMOUS)['address_id'] == 'a-home'
    assert 'private' not in preferences.PATH.read_text()  # flat, door code etc. are not stored


def _sign_in(login, session_id):
    """Write the shared session file the way a QR login in any server process does."""
    from better_lavka_mcp.auth.qr import save_session
    http = httpx.AsyncClient()
    http.cookies.set('Session_id', session_id, domain='.yandex.uz', path='/')
    http.cookies.set('yandex_login', login, domain='.yandex.uz', path='/')
    save_session(http, client.STATE)


def _client_for(login, session_id):
    def create():
        http = httpx.AsyncClient()
        http.cookies.set('Session_id', session_id, domain='.yandex.uz', path='/')
        http.cookies.set('yandex_login', login, domain='.yandex.uz', path='/')
        return http
    return create


@pytest.mark.asyncio
async def test_address_of_the_previous_account_is_not_used_after_another_process_signs_in(saved, monkeypatch):
    from better_lavka_mcp import preferences
    _sign_in('alice', 'sid-a')
    monkeypatch.setattr(client, 'create_http', _client_for('alice', 'sid-a'))
    await account.use_address('home')
    assert app._location(REAL_CREATE_HTTP()) == [69.2, 41.3]
    assert 'sid-a' not in preferences.PATH.read_text()  # only a hash of the login cookies is kept
    # A second server process signs in to bob; this process still holds alice's address in memory.
    _sign_in('bob', 'sid-b')
    with pytest.raises(ValueError, match='No delivery address yet'):
        app._location(REAL_CREATE_HTTP())
    # Session_id is reissued on every login; the same login keeps its address.
    _sign_in('alice', 'sid-a2')
    await account.use_address('home')
    _sign_in('alice', 'sid-a3')
    assert app._location(REAL_CREATE_HTTP()) == [69.2, 41.3]


@pytest.mark.asyncio
async def test_slow_use_address_finishing_after_a_login_does_not_carry_over(saved, monkeypatch):
    _sign_in('bob', 'sid-b')
    # use_address read alice's list with alice's cookies, then bob signed in before it saved.
    monkeypatch.setattr(client, 'create_http', _client_for('alice', 'sid-a'))
    await account.use_address('home')
    monkeypatch.setattr(app, '_delivery', None)  # and in a fresh process too
    assert app._current_delivery(REAL_CREATE_HTTP()) is None


@pytest.mark.asyncio
async def test_login_landing_between_tool_start_and_client_creation_is_caught(saved, monkeypatch):
    from better_lavka_mcp.tools import storefront
    _sign_in('alice', 'sid-a')
    monkeypatch.setattr(client, 'create_http', _client_for('alice', 'sid-a'))
    await account.use_address('home')
    def bob_signs_in_first():
        _sign_in('bob', 'sid-b')  # another process, right before this tool loads the session
        return REAL_CREATE_HTTP()
    monkeypatch.setattr(client, 'create_http', bob_signs_in_first)
    searched = []
    async def search(self, query, limit, location):
        searched.append(location)
        return {'products': []}
    monkeypatch.setattr(client.Lavka, 'search', search)
    with pytest.raises(ValueError, match='No delivery address yet'):
        await storefront.search_products('кефир')
    assert searched == []  # alice's coordinates never went out with bob's cookies

@pytest.mark.asyncio
async def test_use_address_returns_candidates_when_unclear(saved):
    result = await account.use_address('avenue')
    assert result['selected'] is False and result['reason'] == 'ambiguous'
    assert [c['index'] for c in result['candidates']] == [1, 2]
    result = await account.use_address('dacha')
    assert result['reason'] == 'not_found' and len(result['candidates']) == 3
    assert app._delivery is None


@pytest.mark.asyncio
async def test_location_needs_address_or_valid_coordinates(saved):
    with pytest.raises(ValueError, match='use_address'):
        app._location(ANONYMOUS)
    assert app._location(ANONYMOUS, 69.0, 41.0) == [69.0, 41.0]
    for lon, lat in [(54, 41), (69, 47), (float('nan'), 41), (True, 41), (69, None)]:
        with pytest.raises(ValueError):
            app._location(ANONYMOUS, lon, lat)
