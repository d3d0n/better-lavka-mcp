import httpx
import pytest

from better_lavka_mcp import app, client
from better_lavka_mcp.orders import submit as orders
from better_lavka_mcp.tools import account

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
    assert app._location() == [69.2, 41.3]
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
    assert app._location() == [69.2, 41.3]
    assert app._current_delivery()['address_id'] == 'a-home'
    assert 'private' not in preferences.PATH.read_text()  # flat, door code etc. are not stored


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
        app._location()
    assert app._location(69.0, 41.0) == [69.0, 41.0]
    for lon, lat in [(54, 41), (69, 47), (float('nan'), 41), (True, 41), (69, None)]:
        with pytest.raises(ValueError):
            app._location(lon, lat)


@pytest.mark.asyncio
async def test_country_selection_fails_closed_without_network(monkeypatch):
    monkeypatch.setattr(client, 'create_http', lambda: pytest.fail('network not expected'))
    for country in ('UZ', 'RU', 'KZ'):
        result = await app.select_country(country)
        assert result['country'] == country and result['persistent'] is False
        assert result['read_only_tools'] == ['get_storefront_status', 'search_products']
        assert result['uz_account_and_checkout_tools_available'] is (country == 'UZ')
    with pytest.raises(ValueError, match='Unsupported'):
        await app.select_country('US')
    for country in ('RU', 'KZ'):
        await app.select_country(country)
        with pytest.raises(ValueError, match='UZ-only'):
            await account.use_address('home')
    await app.select_country('UZ')
