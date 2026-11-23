import json
import httpx
import pytest

@pytest.mark.asyncio
async def test_retrieve_requires_authenticated_session_and_returns_live_cart():
    from better_lavka_mcp.cart import CartClient
    async def handle(request):
        assert request.url.path == '/api/v1/providers/cart/v1/retrieve'
        assert json.loads(request.content)['position']['location'] == [65.0, 40.0]
        return httpx.Response(200, json={'cartId': 'cart', 'cartVersion': 1, 'items': []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = CartClient(http, {'pageData': {'passportInfo': {'status': 'VALID'}}, 'csrfToken': 'a', 'authProxyCsrfToken': 'b'}, [65.0, 40.0], 'сум')
        assert (await client.retrieve())['items'] == []


@pytest.mark.asyncio
async def test_set_items_sends_one_update_and_lets_lavka_price_lines():
    from better_lavka_mcp.cart import CartClient
    calls = []
    def handle(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith('/update'):
            lines = {line['id']: line for line in body['items']}
            assert set(lines) == {'old', 'new'} and body['cartVersion'] == 1
            assert all(line['currency'] == 'UZS' and line['price'] for line in lines.values())
            # Existing lines keep the cart price; Lavka reprices new ones from a placeholder.
            assert lines['old']['price'] == '4990' and lines['old']['quantity'] == '0'
            assert lines['new']['price'] == '1' and lines['new']['quantity'] == '2'
            return httpx.Response(200, json={'ok': True})
        items = [{'id': 'old', 'quantity': '1', 'price': '4990'}] if len(calls) == 1 else [{'id': 'new', 'quantity': '2', 'price': '16990'}]
        return httpx.Response(200, json={'cartId': 'cart', 'cartVersion': len(calls), 'items': items})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = CartClient(http, {'pageData': {'passportInfo': {'status': 'VALID'}}}, [65.0, 40.0], 'сум')
        result = await client.set_items({'old': 0, 'new': 2})
        assert result['items'] == [{'id': 'new', 'quantity': '2', 'price': '16990'}]
        assert [path for path, _ in calls].count('/api/v1/providers/cart/v1/update') == 1


@pytest.mark.asyncio
async def test_add_counts_from_the_snapshot_whose_version_is_sent():
    from better_lavka_mcp.cart import CartClient
    retrieves, updates = [], []
    def handle(request):
        body = json.loads(request.content)
        if request.url.path.endswith('/update'):
            updates.append(body)
            return httpx.Response(200, json={})
        retrieves.append(1)
        # Another writer bumps the cart between reads: v1 has 1 milk, v2 has 3.
        if len(retrieves) == 1:
            return httpx.Response(200, json={'cartId': 'cart', 'cartVersion': 1, 'items': [{'id': 'milk', 'quantity': '1', 'price': '100'}]})
        return httpx.Response(200, json={'cartId': 'cart', 'cartVersion': 2, 'items': [{'id': 'milk', 'quantity': '3', 'price': '100'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = CartClient(http, {'pageData': {'passportInfo': {'status': 'VALID'}}}, [65.0, 40.0], 'сум')
        await client.set_items({'milk': 2}, add=True)
    assert len(updates) == 1 and len(retrieves) == 2
    assert (updates[0]['cartVersion'], updates[0]['items'][0]['quantity']) == (1, '3')


@pytest.mark.asyncio
async def test_concurrent_adds_do_not_overwrite_each_other():
    import asyncio
    from better_lavka_mcp.cart import CartClient
    cart = {'version': 1, 'milk': 0}
    async def handle(request):
        await asyncio.sleep(0)
        if request.url.path.endswith('/update'):
            body = json.loads(request.content)
            assert body['cartVersion'] == cart['version']
            cart['milk'], cart['version'] = int(body['items'][0]['quantity']), cart['version'] + 1
            return httpx.Response(200, json={})
        items = [{'id': 'milk', 'quantity': str(cart['milk']), 'price': '100'}] if cart['milk'] else []
        return httpx.Response(200, json={'cartId': 'cart', 'cartVersion': cart['version'], 'items': items})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = CartClient(http, {'pageData': {'passportInfo': {'status': 'VALID'}}}, [65.0, 40.0], 'сум')
        await asyncio.gather(client.set_items({'milk': 1}, add=True), client.set_items({'milk': 2}, add=True))
    assert cart['milk'] == 3


@pytest.mark.asyncio
async def test_unverified_readback_fails_closed():
    from better_lavka_mcp.cart import CartClient
    from better_lavka_mcp.server import UpstreamError
    def handle(request):
        if request.url.path.endswith('/update'):
            return httpx.Response(200, json={'ok': True})
        return httpx.Response(200, json={'cartId': 'cart', 'cartVersion': 1, 'items': []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        c = CartClient(http, {'pageData': {'passportInfo': {'status': 'VALID'}}}, [65.0, 40.0], 'сум')
        with pytest.raises(UpstreamError, match='readback'):
            await c.set_items({'p': 1})


def test_rejects_unauthenticated_and_ruble_context():
    from better_lavka_mcp.cart import CartClient
    from better_lavka_mcp.server import UpstreamError
    with pytest.raises(UpstreamError, match='authenticated'):
        CartClient(None, {'pageData': {'passportInfo': {'status': 'INVALID_PARAMS'}}}, [69, 41], 'сум')
    with pytest.raises(ValueError, match='currency'):
        CartClient(None, {'pageData': {'passportInfo': {'status': 'VALID'}}}, [69, 41], '₽')
