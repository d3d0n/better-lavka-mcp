"""What the model actually receives through MCP: text with the data and the next step."""
import json

import httpx
import pytest

from better_lavka_mcp import render, server
import test_shopping_tools as shop


@pytest.fixture
def lavka(monkeypatch):
    fake = shop.FakeLavka()
    monkeypatch.setattr(server, 'create_http', lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)))
    monkeypatch.setattr(server, '_delivery', {'address_id': 'a-home', 'location': shop.HOME, 'index': 0, 'label': 'Дом', 'short_address': 'Example street, 1'})
    monkeypatch.delenv('BETTER_LAVKA_MCP_OUTPUT', raising=False)
    return fake


async def text_of(tool, args):
    content = await server.mcp.call_tool(tool, args)
    content = content[0] if isinstance(content, tuple) else content
    return '\n'.join(c.text for c in content if c.type == 'text')


@pytest.mark.asyncio
async def test_add_to_cart_text_names_choices_ids_and_next_step(lavka):
    text = await text_of('add_to_cart', {'items': [{'query': 'каймак'}, {'query': 'йогурт', 'quantity': 2}, {'query': 'ананас'}]})
    assert '**Added:** Каймак ×1' in text
    assert 'Pick one for "йогурт"** ×2' in text and 'Ask the user which one' in text
    assert '- Йогурт клубничный — 9 990 сум · `yogurt-straw`' in text
    assert '**Not found:** ананас' in text
    assert 'Total **21 990 сум** incl. delivery 10 сум · arrives in 15 мин' in text
    assert 'Ready to order: preview_order' in text
    assert '{' not in text  # no JSON


@pytest.mark.asyncio
async def test_empty_cart_and_json_mode(lavka, monkeypatch):
    assert (await text_of('get_cart', {})).startswith('**Cart is empty.**')
    monkeypatch.setenv('BETTER_LAVKA_MCP_OUTPUT', 'json')
    assert '"total_uzs": "0"' in await text_of('get_cart', {})


def test_preview_hands_over_token_and_exact_total():
    text = render.render('preview_order', {
        'preview_token': 'tok-123', 'expires_in_seconds': 300, 'total_uzs': '87630', 'currency': 'UZS', 'card': '**** 3456',
        'delivery': 'non-rover', 'address': {'street': 'Example street', 'house': '1', 'flat': '5'},
        'items': [{'title': 'Тво\xadрог', 'quantity': '1', 'price': '19990'}]})
    assert 'nothing is charged yet' in text and 'card **** 3456' in text and '(flat 5)' in text
    assert '- Творог ×1 — 19 990 сум' in text
    assert 'submit_order(order_preview_token="tok-123", confirmed_total_uzs="87630", explicit_confirmation=true)' in text


@pytest.mark.parametrize('result, expected', [
    ({'outcome': 'submitted', 'order_id': 'o1', 'payment_status': 'wait_user_action', 'redirect_url': 'https://bank.example/3ds'},
     'Give the user this link to confirm: https://bank.example/3ds'),
    ({'outcome': 'submitted', 'order_id': 'o1', 'payment_status': 'success', 'redirect_url': None}, 'Payment status: success — paid'),
    ({'outcome': 'unknown', 'http_status': 502}, 'Do NOT submit again'),
    ({'outcome': 'rejected', 'codes': ['grocery_unavailable_for_checkout'], 'recovery': {'kind': 'checkout_price_or_fee_changed'}},
     'Prices or fees changed'),
])
def test_submit_outcomes_say_what_to_do(result, expected):
    assert expected in render.render('submit_order', result)


@pytest.mark.asyncio
async def test_address_search_uses_suggest_search(lavka, monkeypatch):
    def handle(request):
        if request.url.path.endswith('/address/v1/suggest-search'):
            assert json.loads(request.content) == {'input': 'Amir Temur 1'}
            return httpx.Response(200, json={'items': [
                {'title': 'улица Амира Темура, 1', 'label': 'Ташкент', 'position': [69.28, 41.33]},
                {'title': 'no point', 'label': 'x'}]})
        return lavka.handle(request)
    monkeypatch.setattr(server, 'create_http', lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    text = await text_of('search_addresses', {'query': 'Amir Temur 1'})
    assert '- улица Амира Темура, 1, Ташкент — longitude 69.28, latitude 41.33' in text and 'no point' not in text
