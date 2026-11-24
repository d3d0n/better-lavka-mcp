import json

import httpx
import pytest

from better_lavka_mcp import client
from better_lavka_mcp.tools import account, cart


@pytest.mark.asyncio
async def test_cart_mcp_read_update_and_masked_cards(monkeypatch):
    calls = []
    def handle(request):
        path = request.url.path
        calls.append(path)
        if request.url.host == 'yandex.uz':
            return httpx.Response(200, headers={'set-cookie': 'yandexuid=anon; Domain=.yandex.uz; Path=/'})
        if request.method == 'GET':
            props = {'pageData': {'passportInfo': {'status': 'VALID'}, 'serviceInfo': {'currency': 'UZS', 'currencySign': 'сум'}}}
            return httpx.Response(200, text='<script id="__page_props__-data">' + json.dumps(props) + '</script>')
        if path.endswith('/retrieve'):
            items = [{'id': 'product', 'title': 'Milk', 'quantity': '1', 'price': '100'}] if '/api/v1/providers/cart/v1/update' in calls else []
            return httpx.Response(200, json={'cartId': 'secret-cart', 'cartVersion': 1, 'items': items, 'availableForCheckout': True})
        if path.endswith('/update'):
            assert json.loads(request.content)['items'][0]['quantity'] == '1'
            return httpx.Response(200, json={})
        if path.endswith('/methods'):
            return httpx.Response(200, json={'methods': [{'type': 'card', 'availability': {'available': True}, 'id': 'secret-card', 'number': '1234567890123456'}]})
        raise AssertionError(path)
    monkeypatch.setattr(client, 'create_http', lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert (await cart.get_cart(69, 41))['items'] == []
    result = await cart.set_cart_quantity('product', 1, 69, 41)
    assert result['items'][0]['quantity'] == '1'
    assert 'secret-cart' not in json.dumps(result)
    assert (await account.list_saved_cards(69, 41))['cards'] == ['**** 3456']
    assert not any('submit' in path for path in calls)
