"""What the model actually receives through MCP: text with the data and the next step."""
import json

import httpx
import pytest

import test_shopping_tools as shop
from better_lavka_mcp import aliases, app, client, render


@pytest.fixture
def lavka(monkeypatch):
    fake = shop.FakeLavka()
    monkeypatch.setattr(client, 'create_http', lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)))
    monkeypatch.setattr(app, '_delivery', {'address_id': 'a-home', 'location': shop.HOME, 'index': 0, 'label': 'Дом', 'short_address': 'Example street, 1'})
    monkeypatch.delenv('BETTER_LAVKA_MCP_OUTPUT', raising=False)
    return fake


async def text_of(tool, args):
    content = await app.mcp.call_tool(tool, args)
    content = content[0] if isinstance(content, tuple) else content
    return '\n'.join(c.text for c in content if c.type == 'text')


@pytest.mark.asyncio
async def test_add_to_cart_text_names_choices_ids_and_next_step(lavka):
    text = await text_of('add_to_cart', {'items': [{'query': 'каймак'}, {'query': 'йогурт', 'quantity': 2}, {'query': 'ананас'}]})
    assert '**Added:** Каймак ×1' in text
    assert 'Pick one for "йогурт"** ×2' in text and 'Ask the user which one' in text
    straw = aliases.short('yogurt-straw')
    assert f'- Йогурт клубничный — 9 990 сум · `{straw}`' in text
    assert 'yogurt-straw' not in text  # only short ids reach the model
    assert '**Not found:** ананас' in text
    assert 'Total **21 990 сум** incl. delivery 10 сум · arrives in 15 мин' in text
    assert 'Ready to order: preview_order' in text
    assert '{' not in text  # no JSON
    # The short id goes back in as a product_id and still means the same yogurt.
    await text_of('add_to_cart', {'items': [{'product_id': straw, 'quantity': 2}]})
    assert lavka.cart['yogurt-straw'] == 2


def test_short_ids_are_three_known_words_stable_and_collision_free(monkeypatch):
    first = aliases.short('7acc5006e3dd4636bb613646099df8a5000400020001')
    assert len(first.split()) == 3 and all(w in aliases.WORDS for w in first.split())
    assert aliases.short('7acc5006e3dd4636bb613646099df8a5000400020001') == first
    # Force a collision: the second id must get a different short id, and both must resolve back.
    monkeypatch.setattr(aliases, '_candidate', lambda real_id, attempt: ['amber fox wave', 'amber fox wind'][attempt])
    assert aliases.short('one') == 'amber fox wave' and aliases.short('two') == 'amber fox wind'
    assert aliases.resolve('amber  fox wave') == 'one' and aliases.resolve('two') == 'two'
    with pytest.raises(ValueError, match='Unknown id'):
        aliases.resolve('owl owl owl')


def test_short_ids_are_shared_with_other_processes_and_never_reused(monkeypatch, tmp_path):
    import sqlite3
    import subprocess
    import sys
    monkeypatch.setattr(aliases, '_candidate', lambda real_id, attempt: ['amber fox wave', 'amber fox wind', 'amber fox wing'][attempt])
    assert aliases.short('mine') == 'amber fox wave'
    # Another server process allocates with the same file and the same (colliding) hash.
    script = ('import sys; from better_lavka_mcp import aliases; aliases.PATH = sys.argv[1]; '
              "aliases._candidate = lambda r, a: ['amber fox wave', 'amber fox wind', 'amber fox wing'][a]; "
              "print(aliases.short('theirs'))")
    other = subprocess.run([sys.executable, '-c', script, str(aliases.PATH)], capture_output=True, text=True, check=True).stdout.strip()
    assert other == 'amber fox wind'
    # This process sees the other one's id immediately and never hands the same words to a third product.
    assert aliases.resolve('amber fox wind') == 'theirs'
    assert aliases.short('third') == 'amber fox wing'
    with sqlite3.connect(aliases.PATH) as db:
        assert db.execute('SELECT COUNT(*), COUNT(DISTINCT alias), COUNT(DISTINCT real_id) FROM aliases').fetchone() == (3, 3, 3)


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


def test_order_total_does_not_subtract_the_discount_twice():
    # Live shape: products price is already discounted (189 110 + 10 + 3 990 = 193 110).
    text = render.render('get_order', {'order_id': 'o1', 'created_at': '2026-11-13T16:15:23', 'status_text': 'Доставлен', 'address': 'x',
                                       'items': [], 'products_uzs': '189110', 'delivery_uzs': '10', 'service_fee_uzs': '3990',
                                       'discount_uzs': '21600', 'total_uzs': '193110'})
    assert 'Products 189 110 сум + delivery 10 сум + service fee 3 990 сум = **193 110 сум** (discount of 21 600 сум already included)' in text


def test_tracking_feed_counts_entries_not_orders():
    assert 'feed is empty' in render.render('list_active_orders', {'active_order_count': 0, 'history_complete': False})
    text = render.render('list_active_orders', {'active_order_count': None, 'feed_entry_count': 2})
    assert '2 entries' in text and 'order' not in text.split(';')[0]


def test_product_card_shows_ingredients_and_limit_when_only_in_composition():
    card = {'id': 'p1', 'title': 'Кефир', 'brand': None, 'price_uzs': 18990, 'pack': '900 г', 'available': True,
            'max_quantity': 24.0, 'description': None, 'nutrition_per_100g': None,
            'composition': 'Молоко нормализованное, закваска.', 'details': {'Страна': 'Узбекистан'}}
    text = render.render('get_product', card)
    assert 'Ingredients: Молоко нормализованное, закваска.' in text and 'Up to 24 per order.' in text
    # Not repeated when the same text already came as a detail block.
    card['details']['Состав'] = card['composition']
    assert 'Ingredients:' not in render.render('get_product', card)


@pytest.mark.asyncio
async def test_address_search_uses_suggest_search(lavka, monkeypatch):
    def handle(request):
        if request.url.path.endswith('/address/v1/suggest-search'):
            assert json.loads(request.content) == {'input': 'Amir Temur 1'}
            return httpx.Response(200, json={'items': [
                {'title': 'улица Амира Темура, 1', 'label': 'Ташкент', 'position': [69.28, 41.33]},
                {'title': 'no point', 'label': 'x'}]})
        return lavka.handle(request)
    monkeypatch.setattr(client, 'create_http', lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    text = await text_of('search_addresses', {'query': 'Amir Temur 1'})
    assert '- улица Амира Темура, 1, Ташкент — longitude 69.28, latitude 41.33' in text and 'no point' not in text
