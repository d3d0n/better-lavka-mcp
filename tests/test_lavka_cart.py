import json
from decimal import Decimal

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
async def test_add_keeps_a_fractional_quantity_exact():
    from better_lavka_mcp.cart import CartClient
    updates, retrieves = [], []
    def handle(request):
        if request.url.path.endswith('/update'):
            updates.append(json.loads(request.content))
            return httpx.Response(200, json={'ok': True})
        retrieves.append(1)
        quantity = '1.5' if len(retrieves) == 1 else '2.5'
        return httpx.Response(200, json={'cartId': 'cart', 'cartVersion': len(retrieves),
                                         'items': [{'id': 'cheese', 'quantity': quantity, 'price': '100'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = CartClient(http, {'pageData': {'passportInfo': {'status': 'VALID'}}}, [65.0, 40.0], 'сум')
        cart = await client.set_items({'cheese': 1}, add=True)
    assert updates[0]['items'][0]['quantity'] == '2.5'
    assert cart['items'][0]['quantity'] == '2.5'


@pytest.mark.asyncio
async def test_add_items_skips_a_fractional_weight_item_and_reports_outcomes():
    from better_lavka_mcp.cart import CartClient
    updates = []
    def handle(request):
        if request.url.path.endswith('/update'):
            updates.append(json.loads(request.content))
            return httpx.Response(200, json={'ok': True})
        return httpx.Response(200, json={'cartId': 'cart', 'cartVersion': 1,
                                         'items': [{'id': 'cheese', 'quantity': '1.5', 'price': '100'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = CartClient(http, {'pageData': {'passportInfo': {'status': 'VALID'}}}, [65.0, 40.0], 'сум')
        cart, outcomes = await client.add_items({'cheese': 1})
    assert updates == []
    assert outcomes['cheese']['target'] == Decimal('2.5') and outcomes['cheese']['skipped']
    assert cart['items'][0]['quantity'] == '1.5'


@pytest.mark.asyncio
async def test_add_items_reports_the_capped_target_and_delta():
    from better_lavka_mcp.cart import CartClient
    updates, retrieves = [], []
    def handle(request):
        if request.url.path.endswith('/update'):
            updates.append(json.loads(request.content))
            return httpx.Response(200, json={'ok': True})
        retrieves.append(1)
        quantity = '45' if len(retrieves) == 1 else '50'
        return httpx.Response(200, json={'cartId': 'cart', 'cartVersion': len(retrieves),
                                         'items': [{'id': 'water', 'quantity': quantity, 'price': '100'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = CartClient(http, {'pageData': {'passportInfo': {'status': 'VALID'}}}, [65.0, 40.0], 'сум')
        _, outcomes = await client.add_items({'water': 10})
    assert updates[0]['items'][0]['quantity'] == '50'
    assert outcomes['water'] == {'before': Decimal('45'), 'requested': Decimal('55'),
                                 'target': Decimal('50'), 'added': Decimal('5')}


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
async def test_removal_during_add_and_cap_is_not_undone():
    import asyncio

    from better_lavka_mcp.cart import CartClient
    from better_lavka_mcp.tools.cart import _add_products
    cart = {'version': 1, 'milk': 3}
    first_update = asyncio.Event()
    async def handle(request):
        await asyncio.sleep(0)
        if request.url.path.endswith('/update'):
            body = json.loads(request.content)
            assert body['cartVersion'] == cart['version']
            cart['milk'], cart['version'] = int(body['items'][0]['quantity']), cart['version'] + 1
            first_update.set()
            return httpx.Response(200, json={})
        items = [{'id': 'milk', 'quantity': str(cart['milk']), 'price': '100', 'quantityLimit': '3'}] if cart['milk'] else []
        return httpx.Response(200, json={'cartId': 'cart', 'cartVersion': cart['version'], 'items': items})
    props = {'pageData': {'passportInfo': {'status': 'VALID'}}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        adder, remover = CartClient(http, props, [65.0, 40.0], 'сум'), CartClient(http, props, [65.0, 40.0], 'сум')
        async def remove_after_first_write():
            await first_update.wait()  # the add is written; its cap step has not run yet
            await remover.set_items({'milk': 0})
        # Adding 1 to 3 hits the limit of 3 and gets capped; the removal must land after that, not in between.
        await asyncio.gather(_add_products(adder, {'milk': 1}, {}), remove_after_first_write())
    assert cart['milk'] == 0


@pytest.mark.asyncio
async def test_unverified_readback_fails_closed():
    from better_lavka_mcp.cart import CartClient
    from better_lavka_mcp.errors import UpstreamError
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
    from better_lavka_mcp.errors import UpstreamError
    with pytest.raises(UpstreamError, match='authenticated'):
        CartClient(None, {'pageData': {'passportInfo': {'status': 'INVALID_PARAMS'}}}, [69, 41], 'сум')
    with pytest.raises(ValueError, match='currency'):
        CartClient(None, {'pageData': {'passportInfo': {'status': 'VALID'}}}, [69, 41], '₽')
