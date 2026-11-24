import json

import httpx
import pytest

from better_lavka_mcp.client import Lavka
from better_lavka_mcp.errors import UpstreamError


@pytest.mark.asyncio
async def test_saved_address_count_requires_valid_session_and_hides_details():
    def handle(request):
        if request.url.host == 'yandex.uz':
            return httpx.Response(200, headers={'set-cookie': 'yandexuid=anon; Domain=.yandex.uz; Path=/'})
        if request.method == 'GET':
            page = {'csrfToken': 'x', 'authProxyCsrfToken': 'y', 'pageData': {'passportInfo': {'status': 'VALID'}}}
            return httpx.Response(200, text='<script id="__page_props__-data">' + json.dumps(page) + '</script>')
        assert request.url.path == '/api/v1/providers/address/v1/get-favorite-addresses'
        return httpx.Response(200, json=[{'id': 'private', 'address': 'private'}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        assert await Lavka(http).saved_address_count() == {'count': 1}

@pytest.mark.asyncio
async def test_saved_address_count_rejects_anonymous_session():
    async with httpx.AsyncClient() as http:
        l = Lavka(http)
        l.props = {'pageData': {'passportInfo': {'status': 'INVALID_PARAMS'}}}
        with pytest.raises(UpstreamError, match='authenticated'):
            await l.saved_address_count()


@pytest.mark.asyncio
async def test_use_address_rejects_out_of_range_digit_without_substring_match(monkeypatch):
    from better_lavka_mcp import app, client
    from better_lavka_mcp.orders import submit as orders
    from better_lavka_mcp.tools import account
    entries = [
        {'addressId': 'a-home', 'address': {'label': 'Дом', 'shortAddress': 'Example street, 1', 'location': [69.2, 41.3]}},
        {'addressId': 'a-mom', 'address': {'label': '', 'shortAddress': 'Sample avenue, 9', 'location': [69.4, 41.1]}},
    ]
    async def addresses(http):
        return None, entries
    monkeypatch.setattr(orders, 'addresses', addresses)
    monkeypatch.setattr(client, 'create_http', lambda: httpx.AsyncClient())
    monkeypatch.setattr(app, '_delivery', None)
    result = await account.use_address('9')
    assert result['selected'] is False and result['reason'] == 'invalid_index'
    assert [c['index'] for c in result['candidates']] == [0, 1]
    assert app._delivery is None
    # An in-range number is still an index, not a house number.
    assert (await account.use_address('1'))['index'] == 1
