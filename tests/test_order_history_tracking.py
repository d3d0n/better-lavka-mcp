import json

import httpx
import pytest

from better_lavka_mcp import client, render
from better_lavka_mcp.constants import PROPS_PAGE
from better_lavka_mcp.tools import account

# Synthetic order-log entries and a tracking-feed entry shaped like the live responses.
ORDERS = [{'orderId': 'o-live', 'createdAt': '2026-11-24T14:34:00+00:00', 'status': 'created', 'statusText': 'created',
           'calculation': {'finalCost': '220560', 'items': [{'name': 'Кинза', 'count': 1, 'cost': '11490', 'productId': 'p1'}]}},
          {'orderId': 'o-old', 'createdAt': '2026-11-13T10:00:00+00:00', 'status': 'closed', 'statusText': 'Доставлен',
           'calculation': {'finalCost': '193110', 'items': [{'name': 'Хлеб', 'count': 1, 'cost': '4990', 'productId': 'p2'}]}}]
FEED = [{'id': 'o-live', 'shortOrderId': '000000-111-2222', 'status': 'delivering', 'paymentStatus': 'success',
         'isCancelable': True, 'deliveryEtaMin': 7, 'actions': [{'type': 'cancel'}],
         'trackingInfo': {'title': 'Ещё ~7 минут', 'subtitle': 'Ali везёт вам заказ'},
         'contact': {'phone': 'secret-phone'}}]


def _lavka(feed_status=200, feed=FEED, passport='VALID'):
    def handle(request):
        if request.url.host == 'yandex.uz':
            return httpx.Response(200, headers={'set-cookie': 'yandexuid=anon; Domain=.yandex.uz; Path=/'})
        if request.url.path == PROPS_PAGE:
            props = {'pageData': {'passportInfo': {'status': passport}}}
            return httpx.Response(200, text='<script id="__page_props__-data">' + json.dumps(props) + '</script>')
        if request.url.path.endswith('/order-log/v1/retrieve'):
            return httpx.Response(200, json={'orders': ORDERS})
        assert request.method == 'GET' and request.url.path == '/api/v1/providers/orders-tracking/v1/tracked-orders'
        return httpx.Response(feed_status, json=feed)
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle))


@pytest.mark.asyncio
async def test_history_carries_the_live_status_of_orders_lavka_still_tracks(monkeypatch):
    monkeypatch.setattr(client, 'create_http', _lavka())
    result = await account.list_order_history(2)
    live, old = result['orders']
    assert live['tracking']['status'] == 'delivering' and 'tracking' not in old
    text = render.render('list_order_history', result)
    assert '“Ещё ~7 минут — Ali везёт вам заказ” (delivering). About 7 min to go; order № 000000-111-2222. Can still be canceled.' in text
    assert 'secret' not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('feed_status,feed', [(500, []), (200, {'orders': []})])
async def test_history_still_answers_when_tracking_does_not(monkeypatch, feed_status, feed):
    monkeypatch.setattr(client, 'create_http', _lavka(feed_status, feed))
    result = await account.list_order_history(2)
    assert [o['order_id'] for o in result['orders']] == ['o-live', 'o-old'] and result['tracking'] == 'unknown'
    assert "Live status: unknown (Lavka's tracking didn't answer)" in render.render('list_order_history', result)


@pytest.mark.asyncio
async def test_an_order_on_its_way_is_shown_even_when_it_is_not_on_the_page(monkeypatch):
    # limit=1 returns only the newest order; the delivering one is older but must still be visible.
    feed = [{**FEED[0], 'id': 'o-old', 'trackingInfo': {'title': 'Ещё ~3 минуты', 'subtitle': 'Курьер в пути'}},
            {'id': 'o-done', 'status': 'closed', 'resolution': 'succeeded', 'trackingInfo': {'title': 'Заказ доставлен'}}]
    monkeypatch.setattr(client, 'create_http', _lavka(feed=feed))
    result = await account.list_order_history(1)
    assert [o['order_id'] for o in result['orders']] == ['o-live'] and result['next_older_than'] == 'o-live'
    assert [t['order_id'] for t in result['active_elsewhere']] == ['o-old']  # closed ones elsewhere stay out
    text = render.render('list_order_history', result)
    assert 'Also in progress, not on this page:' in text and '“Ещё ~3 минуты — Курьер в пути” (delivering)' in text
