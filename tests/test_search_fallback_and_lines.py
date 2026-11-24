import json

import httpx
import pytest

from better_lavka_mcp import render
from better_lavka_mcp.client import Lavka, _order_view, is_service_line

PROPS = {'pageData': {'passportInfo': {'status': 'VALID'}, 'serviceInfo': {'currency': 'UZS', 'currencySign': 'сум'}}}


@pytest.mark.asyncio
async def test_search_retries_a_word_lavka_only_finds_by_its_stem():
    # Live: "каймак" returns nothing while "кайм" returns two каймаки.
    asked = []
    def handle(request):
        text = json.loads(request.content)['text']
        asked.append(text)
        found = [{'id': 'k1', 'title': 'Кай\xadмак 45% Essi', 'currentPrice': 23490, 'oldPrice': 24990, 'amount': '180 г', 'available': True,
                  'quantityLimit': '9', 'discountPricing': {'discountAnalyticsCategories': [{'percentageValue': '6'}]}}] if text == 'кайм' else []
        return httpx.Response(200, json={'cacheProducts': found})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        lavka = Lavka(http)
        lavka.props = PROPS
        result = await lavka.search('каймак', 5, (69.27, 41.32))
    assert asked == ['каймак', 'кайма', 'кайм']
    assert result['searched_for'] == 'кайм' and [p['id'] for p in result['products']] == ['k1']
    text = render.render('search_products', result)
    assert 'results for «кайм»' in text and 'Каймак 45% Essi — 23 490 сум (was 24 990 сум, −6%), 180 г' in text


@pytest.mark.asyncio
async def test_search_stops_at_the_first_query_that_finds_something():
    asked = []
    def handle(request):
        asked.append(json.loads(request.content)['text'])
        return httpx.Response(200, json={'cacheProducts': [{'id': 'b1', 'title': 'Бедро', 'currentPrice': 41240, 'available': True}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        lavka = Lavka(http)
        lavka.props = PROPS
        result = await lavka.search('бедро', 5, (69.27, 41.32))
    assert asked == ['бедро'] and 'searched_for' not in result


def test_bags_stay_on_the_order_but_are_marked_as_service_lines():
    order = {'orderId': 'o1', 'createdAt': '2026-11-24T14:34:00+00:00', 'status': 'closed', 'statusText': 'Доставлен',
             'calculation': {'finalCost': '219910', 'items': [
                 {'name': 'Кинза', 'count': 1, 'cost': '11490', 'productId': 'p-kinza'},
                 {'name': 'Пакет «Яндекс Лавка»', 'count': 2, 'cost': '650', 'productId': 'f97388:st-sa-bag'}]}}
    view = _order_view(order)
    # The order shows what was billed, bags included (that is how a missing bag gets noticed).
    assert [i['product_id'] for i in view['items']] == ['p-kinza', 'f97388:st-sa-bag']
    assert [is_service_line(i['product_id']) for i in view['items']] == [False, True]
