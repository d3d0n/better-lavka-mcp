"""Shopping tools against a synthetic in-memory Lavka shaped like lavka.yandex.uz (2026-11)."""
import json

import httpx
import pytest

from better_lavka_mcp import app, client, render
from better_lavka_mcp.orders import submit as orders
from better_lavka_mcp.tools import account, cart, catalog
from better_lavka_mcp.tools import orders as order_tools

HOME = [69.2, 41.3]
# id -> (title as Lavka sends it, current price, unavailable on depot, quantity limit)
CATALOG = {
    'oatmilk': ('Мо\xadло\xadко овсяное', 30990, False, '24'),
    'milk': ('Мо\xadло\xadко <notr>Example</notr> 2,5%', 15990, False, '24'),
    'bread': ('Хлеб', 4990, True, '0'),
    'kaymak': ('Кай\xadмак', 21990, False, '3'),
    'eggs': ('Яйца', 25990, False, '10'),
    'yogurt-straw': ('Йогурт клубничный', 9990, False, '20'),
    'yogurt-peach': ('Йогурт персиковый', 8990, False, '20'),
    'smetana': ('Сметана', 12990, False, '20'),
    'water': ('Вода', 5000, False, '50'),
    'cheese': ('Сыр', 45000, False, '10'),
}
ORDER = {'orderId': 'order-1', 'createdAt': '2026-01-01T09:00:00+00:00', 'status': 'closed',
         'calculation': {'finalCost': '99000', 'items': [
             {'name': 'Молоко', 'count': 2, 'cost': '14990', 'productId': 'milk'},
             {'name': 'Хлеб', 'count': 1, 'cost': '4500', 'productId': 'bread'},
             {'name': 'Каймак', 'count': 5, 'cost': '20990', 'productId': 'kaymak'}]},
         'contact': {'phone': 'secret'}}
FRACTIONAL_ORDER = {'orderId': 'order-2', 'createdAt': '2026-02-01T09:00:00+00:00', 'status': 'closed',
                    'calculation': {'finalCost': '52000', 'items': [
                        {'name': 'Сыр', 'count': 1.5, 'cost': '45000', 'productId': 'cheese'},
                        {'name': 'Молоко', 'count': 2, 'cost': '14990', 'productId': 'milk'}]},
                    'contact': {'phone': 'secret'}}
WEIGHT_ONLY_ORDER = {'orderId': 'order-3', 'createdAt': '2026-03-01T09:00:00+00:00', 'status': 'closed',
                     'calculation': {'finalCost': '22500', 'items': [
                         {'name': 'Сыр', 'count': 0.5, 'cost': '22500', 'productId': 'cheese'}]},
                     'contact': {'phone': 'secret'}}


class FakeLavka:
    def __init__(self):
        self.cart, self.version, self.updates = {}, 1, []
        self.orders = [ORDER]
        self.stale = {}  # product id -> the old price its cart line still holds after Lavka repriced it

    def cart_body(self):
        items = []
        for pid, qty in self.cart.items():
            title, price, unavailable, limit = CATALOG[pid]
            items.append({'id': pid, 'title': title, 'quantity': str(qty), 'price': str(self.stale.get(pid, price)), 'amount': '1 шт',
                          'isUnavailableOnDepot': unavailable, 'quantityLimit': limit})
        total = sum(CATALOG[pid][1] * qty for pid, qty in self.cart.items())
        body = {'cartId': 'cart-1', 'cartVersion': self.version, 'items': items, 'totalPriceValue': str(total),
                'availableForCheckout': bool(items), 'orderConditions': {'deliveryCost': '10', 'eta': '15 мин'}}
        if self.stale:
            # The live shape: the cart total is at the new prices, the lines still at the old ones.
            body.update(availableForCheckout=False, checkoutUnavailableReason='price-mismatch', hidePriceMismatch=True, diffDataProducts=[
                {'id': pid, 'quantityType': 'unit', 'quantity': self.cart[pid], 'price': CATALOG[pid][1],
                 'oldPriceWithSign': f'{old:,} сум'.replace(',', ' '), 'title': CATALOG[pid][0]} for pid, old in self.stale.items()])
        return body

    def handle(self, request):
        path = request.url.path
        if request.url.host == 'yandex.uz':
            return httpx.Response(200, headers={'set-cookie': 'yandexuid=1; Domain=.yandex.uz; Path=/'})
        if request.method == 'GET':
            props = {'pageData': {'passportInfo': {'status': 'VALID'}, 'serviceInfo': {'currency': 'UZS', 'currencySign': 'сум'}}}
            return httpx.Response(200, text='<script id="__page_props__-data">' + json.dumps(props) + '</script>')
        body = json.loads(request.content)
        if path.endswith('/search/v3/lavka'):
            assert body['position'] == {'location': HOME}
            hits = [pid for pid, (title, *_rest) in CATALOG.items() if body['text'].casefold() in title.replace('\xad', '').casefold()]
            return httpx.Response(200, json={'cacheProducts': [{'id': pid, 'title': CATALOG[pid][0], 'currentPrice': CATALOG[pid][1], 'available': not CATALOG[pid][2]} for pid in hits]})
        if path.endswith('/cart/v1/retrieve'):
            assert body['position'] == {'location': HOME}
            return httpx.Response(200, json=self.cart_body())
        if path.endswith('/cart/v1/update'):
            assert body['cartVersion'] == self.version and all(line['price'] for line in body['items'])
            self.updates.append({line['id']: int(line['quantity']) for line in body['items']})
            for line in body['items']:
                if line['price'] == str(CATALOG[line['id']][1]):
                    self.stale.pop(line['id'], None)  # re-saved at the current price
                qty = int(line['quantity'])
                if qty:
                    self.cart[line['id']] = qty
                else:
                    self.cart.pop(line['id'], None)
            self.version += 1
            return httpx.Response(200, json={})
        if path.endswith('/order-log/v1/retrieve'):
            return httpx.Response(200, json={'orders': self.orders})
        if path.endswith('/v1/product'):
            return httpx.Response(200, json={'product': {
                'id': body['productId'], 'title': 'Кар\xadто\xadфель', 'brand': 'Example Farm', 'currentPrice': 16990, 'amount': '800 г',
                'available': True, 'quantityLimit': '24', 'description': 'Для пюре.',
                'descriptionBlocks': [{'title': 'Срок годности ', 'value': '15 д.', 'type': 'text'}, {'title': 'Страна', 'value': 'Узбекистан', 'type': 'text'}],
                'options': {'ingredients': {'description': '', 'pfcTraits': [
                    {'id': 'calories', 'measures': {'per100g': '77'}}, {'id': 'protein', 'measures': {'per100g': '2'}},
                    {'id': 'fat', 'measures': {'per100g': '0,4'}}, {'id': 'carbohydrate', 'measures': {'per100g': '16,3'}}]}}}})
        if path.endswith('/v1/layout'):
            assert body['layoutSlug'] == 'grocery'
            return httpx.Response(200, json={'sections': [
                {'categoryGroup': {'categoryGroupInfo': {'id': 'g-dairy', 'title': 'Молочный прилавок'}},
                 'categories': [{'categoryInfo': {'id': 'c-milk', 'title': 'Молоко, масло и яйца', 'available': True}},
                                {'categoryInfo': {'id': 'c-closed', 'title': 'Сыры', 'available': False}}]},
                {'categoryGroup': {'categoryGroupInfo': {'id': 'g-empty', 'title': 'Пусто'}}, 'categories': []}]})
        if path.endswith('/v2/category'):
            assert body['categoryId'] == 'c-milk' and body['categorySlugPath'] == {'layoutSlug': 'grocery', 'groupId': 'g-dairy'}
            return httpx.Response(200, json={
                'categories': [{'value': {'type': 'category', 'title': 'Молоко, масло и яйца'}, 'items': [
                    {'value': {'type': 'subcategory', 'title': 'Свежее молоко'}, 'items': [{'id': 'milk'}, {'id': 'oatmilk'}]},
                    {'value': {'type': 'subcategory', 'title': 'Яй\xadца'}, 'items': [{'id': 'eggs'}]}]}],
                'products': [{'id': pid, 'title': CATALOG[pid][0], 'currentPrice': CATALOG[pid][1], 'amount': '1 шт', 'available': True}
                             for pid in ('milk', 'oatmilk', 'eggs')]})
        if path.endswith('/payments/v1/status'):
            return httpx.Response(200, json={'status': 'wait_user_action', 'payload': {'redirectUrl': 'https://trust.example/3ds'}})
        raise AssertionError(path)


@pytest.fixture
def lavka(monkeypatch):
    fake = FakeLavka()
    monkeypatch.setattr(client, 'create_http', lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)))
    monkeypatch.setattr(app, '_delivery', {'address_id': 'a-home', 'location': HOME, 'index': 0, 'label': 'Дом', 'short_address': 'Example street, 1'})
    return fake


@pytest.mark.asyncio
async def test_add_to_cart_by_name_in_one_update_and_reports_problems(lavka):
    lavka.cart = {'eggs': 1}
    result = await cart.add_to_cart([{'query': 'молоко', 'quantity': 2}, {'query': 'хлеб'}, {'query': 'каймак', 'quantity': 4},
                                       {'query': 'ананас'}, {'product_id': 'eggs'}, {'query': 'йогурт', 'quantity': 3},
                                       {'query': 'сметана'}])
    # Never bought: "йогурт" has two matches, so nothing is added for it; "сметана" has one, so it is.
    assert lavka.updates[0] == {'milk': 2, 'kaymak': 4, 'eggs': 2, 'smetana': 1}
    assert lavka.updates[1] == {'kaymak': 3}
    assert result['not_found'] == ['хлеб', 'ананас']
    assert result['limited'] == [{'title': 'Каймак', 'requested': 4, 'max': 3}]
    assert {(a['title'], a['added'], a['in_cart']) for a in result['added']} == {
        ('Молоко Example 2,5%', 2, 2), ('Каймак', 3, 3), ('Яйца', 1, 2), ('Сметана', 1, 1)}
    assert result['skipped'] == []
    assert not any('Йогурт' in i['title'] for i in result['cart']['items'])
    # "молоко" matches oat milk first, but the milk you ordered before wins.
    assert result['needs_choice'] == [{'query': 'йогурт', 'quantity': 3, 'options': [
        {'product_id': 'yogurt-straw', 'title': 'Йогурт клубничный', 'price_uzs': 9990, 'pack': None},
        {'product_id': 'yogurt-peach', 'title': 'Йогурт персиковый', 'price_uzs': 8990, 'pack': None}]}]


@pytest.mark.asyncio
async def test_repeat_last_order_drops_what_the_store_cannot_sell(lavka):
    result = await cart.repeat_order()
    assert lavka.updates == [{'milk': 2, 'bread': 1, 'kaymak': 5}, {'bread': 0, 'kaymak': 3}]
    assert result['order'] == {'order_id': 'order-1', 'created_at': '2026-01-01T09:00:00+00:00', 'total_uzs': '99000'}
    assert result['unavailable'] == ['Хлеб'] and result['limited'][0]['max'] == 3
    assert result['skipped'] == []
    assert {(a['title'], a['added'], a['in_cart']) for a in result['added']} == {('Молоко Example 2,5%', 2, 2), ('Каймак', 3, 3)}
    assert lavka.cart == {'milk': 2, 'kaymak': 3}
    assert result['cart']['available_for_checkout'] is True and 'secret' not in json.dumps(result)


@pytest.mark.asyncio
async def test_set_cart_quantity_needs_no_price_or_coordinates(lavka):
    result = await cart.set_cart_quantity('milk', 3)
    assert lavka.updates == [{'milk': 3}]
    assert result['items'][0] == {'id': 'milk', 'title': 'Молоко Example 2,5%', 'quantity': '3', 'price_uzs': '15990', 'pack': '1 шт', 'available': True, 'orderable': 24, 'low_stock': False}
    assert (result['delivery_uzs'], result['eta']) == ('10', '15 мин')


@pytest.mark.asyncio
async def test_add_to_cart_reports_the_fifty_per_line_cap_for_summed_requests(lavka):
    result = await cart.add_to_cart([{'product_id': 'water', 'quantity': 30}, {'product_id': 'water', 'quantity': 30}])
    assert lavka.updates == [{'water': 50}]
    assert result['limited'] == [{'title': 'Вода', 'requested': 60, 'max': 50}]
    assert result['added'] == [{'product_id': 'water', 'title': 'Вода', 'added': 50, 'in_cart': 50}]


@pytest.mark.asyncio
async def test_add_to_cart_reports_the_fifty_per_line_cap_above_an_existing_quantity(lavka):
    lavka.cart = {'water': 45}
    result = await cart.add_to_cart([{'product_id': 'water', 'quantity': 10}])
    assert lavka.updates == [{'water': 50}]
    assert result['limited'] == [{'title': 'Вода', 'requested': 55, 'max': 50}]
    assert result['added'] == [{'product_id': 'water', 'title': 'Вода', 'added': 5, 'in_cart': 50}]


@pytest.mark.asyncio
async def test_add_to_cart_skips_an_existing_fractional_weight_item(lavka):
    lavka.cart = {'cheese': 1.5}
    result = await cart.add_to_cart([{'product_id': 'cheese', 'quantity': 1}])
    assert lavka.updates == []
    assert result['skipped'] == [{'title': 'Сыр', 'reason': 'Cart already holds a fractional (weight) quantity; it was not changed'}]
    assert result['added'] == []
    assert result['cart']['items'][0]['quantity'] == '1.5'


@pytest.mark.asyncio
async def test_repeat_order_skips_fractional_historical_quantities(lavka):
    lavka.orders = [FRACTIONAL_ORDER]
    result = await cart.repeat_order('order-2')
    assert lavka.updates == [{'milk': 2}]
    assert result['skipped'] == [{'title': 'Сыр',
                                  'reason': 'Historical quantity is fractional (weight item); it cannot be repeated as a unit line'}]
    assert result['added'] == [{'product_id': 'milk', 'title': 'Молоко Example 2,5%', 'added': 2, 'in_cart': 2}]
    assert 'secret' not in json.dumps(result)


@pytest.mark.asyncio
async def test_repeat_order_of_only_weight_items_writes_nothing(lavka):
    lavka.orders = [WEIGHT_ONLY_ORDER]
    result = await cart.repeat_order('order-3')
    assert lavka.updates == []
    assert result['added'] == [] and result['skipped'][0]['title'] == 'Сыр'
    assert result['cart']['items'] == []


@pytest.mark.asyncio
async def test_get_product_parses_nutrition_and_details(lavka):
    product = await catalog.get_product('potato')
    assert product['title'] == 'Картофель' and product['pack'] == '800 г'
    assert product['nutrition_per_100g'] == {'kcal': 77.0, 'protein': 2.0, 'fat': 0.4, 'carbs': 16.3}
    assert product['details'] == {'Срок годности': '15 д.', 'Страна': 'Узбекистан'}


@pytest.mark.asyncio
async def test_payment_status_returns_3ds_link(lavka):
    result = await account.get_payment_status('order-1')
    assert (result['payment_status'], result['redirect_url']) == ('wait_user_action', 'https://trust.example/3ds')


@pytest.mark.asyncio
async def test_preview_order_defaults_to_session_address_and_only_card(lavka, monkeypatch):
    seen = {}
    async def addresses(http):
        return None, [{'addressId': 'a-work', 'address': {'location': [69.3, 41.2]}}, {'addressId': 'a-home', 'address': {'location': HOME}}]
    class Cards:
        def __init__(self, http, props, location):
            seen['location'] = location
        async def lookup(self):
            return type('R', (), {'masked_cards': ['**** 4242']})()
    async def preview(http, index, suffix, expected=None):
        seen['expected'] = expected
        return {'index': index, 'suffix': suffix, 'preview_token': 't'}
    monkeypatch.setattr(orders, 'addresses', addresses)
    monkeypatch.setattr(orders, 'preview', preview)
    monkeypatch.setattr('better_lavka_mcp.payment.PaymentClient', Cards)
    assert await order_tools.preview_order() == {'index': 1, 'suffix': '4242'}
    assert seen['location'] == HOME and seen['expected'] == 'a-home'


@pytest.mark.asyncio
async def test_preview_order_remembers_the_chosen_card(lavka, monkeypatch):
    async def addresses(http):
        return None, [{'addressId': 'a-home', 'address': {'location': HOME}}]
    class Cards:
        def __init__(self, http, props, location):
            pass
        async def lookup(self):
            return type('R', (), {'masked_cards': ['**** 1111', '**** 2222']})()
    async def preview(http, index, suffix, expected=None):
        return {'suffix': suffix, 'preview_token': 't'}
    monkeypatch.setattr(orders, 'addresses', addresses)
    monkeypatch.setattr(orders, 'preview', preview)
    monkeypatch.setattr('better_lavka_mcp.payment.PaymentClient', Cards)
    asked = await order_tools.preview_order()
    assert asked['preview'] is False and asked['cards'] == ['**** 1111', '**** 2222']
    assert await order_tools.preview_order(card_last4='2222') == {'suffix': '2222'}
    assert await order_tools.preview_order() == {'suffix': '2222'}


@pytest.mark.asyncio
async def test_preview_order_without_saved_cards_returns_empty_cards(lavka, monkeypatch):
    async def addresses(http):
        return None, [{'addressId': 'a-home', 'address': {'location': HOME}}]
    class Cards:
        def __init__(self, http, props, location):
            pass
        async def lookup(self):
            return type('R', (), {'masked_cards': []})()
    async def preview(http, index, suffix, expected=None):
        pytest.fail('a preview without a card must not be attempted')
    monkeypatch.setattr(orders, 'addresses', addresses)
    monkeypatch.setattr(orders, 'preview', preview)
    monkeypatch.setattr('better_lavka_mcp.payment.PaymentClient', Cards)
    result = await order_tools.preview_order()
    assert result == {'preview': False, 'cards': []}


@pytest.mark.asyncio
async def test_catalog_lists_categories_and_browses_shelves(lavka):
    groups = (await catalog.list_categories())['groups']
    assert groups == [{'group_id': 'g-dairy', 'title': 'Молочный прилавок',
                       'categories': [{'category_id': 'c-milk', 'title': 'Молоко, масло и яйца'}]}]
    shelf = await catalog.get_category_products('c-milk')
    assert shelf['subcategories'] == [{'title': 'Свежее молоко', 'count': 2}, {'title': 'Яйца', 'count': 1}]
    assert [(p['title'], p['subcategory']) for p in shelf['products']] == [
        ('Молоко Example 2,5%', 'Свежее молоко'), ('Молоко овсяное', 'Свежее молоко'), ('Яйца', 'Яйца')]
    eggs = await catalog.get_category_products('c-milk', subcategory='яйца', limit=5)
    assert [p['id'] for p in eggs['products']] == ['eggs'] and eggs['total'] == 1
    with pytest.raises(ValueError, match='list_categories'):
        await catalog.get_category_products('c-closed')


@pytest.mark.asyncio
async def test_preview_accepts_prices_lavka_changed_since_items_were_added(lavka, monkeypatch):
    lavka.cart = {'kaymak': 1, 'eggs': 2}
    lavka.stale = {'kaymak': 19990}
    text = render.render('get_cart', await cart.get_cart())
    assert 'Каймак ×1 — 21 990 сум each' in text  # the current price, not the stale line price
    assert 'Prices changed since these were added:** Каймак 19 990 сум → 21 990 сум' in text and "Can't order yet" not in text
    async def addresses(http):
        return None, [{'addressId': 'a-home', 'address': {'location': HOME}}]
    calls = []
    async def preview(http, index, suffix, expected=None):
        calls.append(dict(lavka.stale))
        if lavka.stale:
            raise orders.CartNotReady('Nonempty available cart and exact total required')
        return {'preview_token': 't', 'total_uzs': lavka.cart_body()['totalPriceValue'], 'card': '**** ' + suffix, 'items': [],
                'address': {'street': 'Example street', 'house': '1'}}
    monkeypatch.setattr(orders, 'addresses', addresses)
    monkeypatch.setattr(orders, 'preview', preview)
    result = await order_tools.preview_order(card_last4='4242')
    # Re-saved once, only the repriced line, at the current price; the other line was not touched.
    assert lavka.updates == [{'kaymak': 1}] and calls == [{'kaymak': 19990}, {}]
    assert result['price_changes'] == [{'id': 'kaymak', 'title': 'Каймак', 'old_uzs': '19990', 'new_uzs': '21990'}]
    assert 'Prices changed since these were added:** Каймак 19 990 сум → 21 990 сум. The total below uses the new prices.' in render.render('preview_order', result)


@pytest.mark.asyncio
async def test_cart_names_what_the_depot_cannot_fill(lavka):
    # The live shape of a sold-out line: not flagged unavailable, but quantityLimit 0 and checkout blocked.
    lavka.cart = {'milk': 2, 'kaymak': 5, 'eggs': 1}
    CATALOG_BEFORE = dict(CATALOG)
    CATALOG['milk'] = (CATALOG['milk'][0], CATALOG['milk'][1], False, '0')
    try:
        result = await cart.get_cart()
    finally:
        CATALOG.update(CATALOG_BEFORE)
    text = render.render('get_cart', {**result, 'available_for_checkout': False, 'checkout_unavailable_reason': 'quantity-over-limit'})
    assert 'Молоко Example 2,5% ×2 — 15 990 сум each — **sold out**' in text
    # quantityLimit is a per-order maximum capped by stock, not a warehouse count.
    assert 'Каймак ×5 — 21 990 сум each — **only 3 can be ordered now**' in text and 'left' not in text
    assert "Can't order yet: Молоко Example 2,5% is sold out; Каймак can be ordered only up to 3." in text


def test_search_marks_sold_out_and_running_low_only():
    products = [{'id': 'a', 'title': 'Морковь', 'price': '15990', 'orderable': 0, 'low_stock': False},
                {'id': 'b', 'title': 'Кефир', 'price': '9990', 'orderable': 2, 'low_stock': True},
                {'id': 'c', 'title': 'Хлеб', 'price': '4990', 'orderable': 30, 'low_stock': False}]
    text = render.render('search_products', {'products': products})
    assert 'Морковь — 15 990 сум, sold out' in text and 'Кефир — 9 990 сум, running low, up to 2 per order' in text
    assert 'Хлеб — 4 990 сум ·' in text  # plenty: no stock noise
