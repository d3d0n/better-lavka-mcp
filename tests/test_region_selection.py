import pytest
import json
from better_lavka_mcp import server
from better_lavka_mcp.orders import submit as orders
import httpx


@pytest.mark.asyncio
async def test_detect_location_requires_unambiguous_favorite(monkeypatch):
    entries = [{'address': {'location': [69.2, 41.3], 'flat': 'private'}}]
    async def addresses(http):
        return None, entries
    monkeypatch.setattr(orders, 'addresses', addresses)
    monkeypatch.setattr(server, 'create_http', lambda: httpx.AsyncClient())
    result = await server.get_saved_address_coordinates('UZ')
    assert result['source'] == 'single_saved_address'
    assert result['longitude'] == 69.2 and result['address_selected'] is False
    assert 'private' not in str(result)
    entries.append({'address': {'location': [69.2, 41.3]}})
    result = await server.get_saved_address_coordinates('UZ')
    assert result['source'] == 'ambiguous' and 'longitude' not in result
    selected = await server.get_saved_address_coordinates('UZ', saved_address_index=1)
    assert selected['source'] == 'saved_address_index'


@pytest.mark.asyncio
async def test_country_selection_fails_closed_without_network(monkeypatch):
    monkeypatch.setattr(server, 'create_http', lambda: pytest.fail('network not expected'))
    for country in ('UZ', 'RU', 'KZ'):
        result = await server.select_country(country)
        assert result['country'] == country and result['persistent'] is False
        assert result['read_only_tools'] == ['get_storefront_status', 'search_products']
        assert result['uz_account_and_checkout_tools_available'] is (country == 'UZ')
    with pytest.raises(ValueError, match='Unsupported'):
        await server.select_country('US')
    await server.select_country('UZ')
    for country in ('RU', 'KZ'):
        with pytest.raises(ValueError, match='UZ-only'):
            await server.get_saved_address_coordinates(country)


@pytest.mark.asyncio
async def test_explicit_location_is_stateless_and_not_coverage():
    result = await server.check_coordinates('UZ', longitude=69.2, latitude=41.3)
    assert result['longitude'] == 69.2 and result['latitude'] == 41.3
    assert result['coverage_verified'] is False and result['address_selected'] is False
    assert result['source'] == 'coordinates'
    assert 'cart' in result['note'].lower()


@pytest.mark.asyncio
async def test_location_bounds_and_invalid_values_fail_closed():
    for lon, lat in [(54, 41), (69, 47), (float('nan'), 41), (True, 41)]:
        with pytest.raises(ValueError):
            await server.check_coordinates('UZ', lon, lat)
    with pytest.raises(ValueError, match='Unsupported'):
        await server.check_coordinates('RU', 69, 41)


@pytest.mark.asyncio
async def test_detection_uses_authenticated_read_only_endpoint(monkeypatch):
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.host == 'yandex.uz':
            return httpx.Response(200, headers={'set-cookie': 'yandexuid=123; Domain=.yandex.uz; Path=/'})
        if request.method == 'GET':
            props = {'pageData': {'passportInfo': {'status': 'VALID'}}}
            return httpx.Response(200, text='<script id="__page_props__-data">' + json.dumps(props) + '</script>')
        assert request.url.path.endswith('get-favorite-addresses')
        return httpx.Response(200, json=[{'address': {'location': [69.2, 41.3], 'flat': 'secret'}}])
    monkeypatch.setattr(server, 'create_http', lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    result = await server.get_saved_address_coordinates('UZ')
    assert result['source'] == 'single_saved_address' and 'secret' not in str(result)
    assert calls == [('GET', '/favicon.ico'), ('GET', '/'), ('POST', '/api/v1/providers/address/v1/get-favorite-addresses')]
    names = {tool.name for tool in await server.mcp.list_tools()}
    assert {'select_country', 'check_coordinates', 'get_saved_address_coordinates'} <= names
