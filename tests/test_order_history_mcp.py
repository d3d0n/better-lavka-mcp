import json

import httpx
import pytest

from better_lavka_mcp.client import Lavka

# Synthetic order-log entry shaped like a live lavka.yandex.uz response (2026-11).
ORDER = {
    'orderId': 'order-2', 'shortOrderId': '000000-111', 'createdAt': '2026-01-02T10:00:00+00:00',
    'closedAt': '2026-01-02T10:30:00+00:00', 'status': 'closed', 'statusText': 'Доставлен',
    'calculation': {'finalCost': '50000', 'deliveryCost': '10', 'serviceFeeV2': '3990', 'discount': '1000', 'currencyCode': 'UZS',
                    'items': [{'name': 'Тво\xadрог <notr>Example</notr>', 'count': 2, 'cost': '18000', 'productId': 'prod-a', 'positionId': 'pos-a'},
                              {'name': 'Хлеб', 'count': 1, 'cost': '4000', 'productId': 'prod-b', 'positionId': 'pos-b'}]},
    'productsPrice': '40000',
    'destinations': [{'shortText': 'Example street, 1', 'point': [69.0, 41.0]}],
    'contact': {'phone': 'secret-phone'}, 'yandexUid': 'secret-uid', 'receipts': [{'url': 'secret-receipt'}],
}


@pytest.mark.asyncio
async def test_history_lists_items_pages_by_order_id_and_hides_contact_data():
    seen = []
    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'orders': [ORDER], 'tabs': {}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = Lavka(http)
        client.props = {'pageData': {'passportInfo': {'status': 'VALID'}}}
        result = await client.order_history(count=1, older_than='order-1')
    assert seen == [{'range': {'count': 1, 'olderThan': 'order-1'}, 'includeServiceMetadata': False}]
    order = result['orders'][0]
    assert order['total_uzs'] == '50000' and order['address'] == 'Example street, 1'
    assert order['items'] == [{'product_id': 'prod-a', 'name': 'Творог Example', 'quantity': 2, 'price_uzs': '18000'},
                              {'product_id': 'prod-b', 'name': 'Хлеб', 'quantity': 1, 'price_uzs': '4000'}]
    assert result['next_older_than'] == 'order-2'
    assert 'secret' not in json.dumps(result)


@pytest.mark.asyncio
async def test_get_order_reads_one_order_with_fees():
    async def handler(request):
        assert json.loads(request.content)['range'] == {'orderId': 'order-2'}
        return httpx.Response(200, json={'orders': [ORDER]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = Lavka(http)
        client.props = {'pageData': {'passportInfo': {'status': 'VALID'}}}
        order = await client.order('order-2')
    assert (order['products_uzs'], order['delivery_uzs'], order['service_fee_uzs'], order['discount_uzs']) == ('40000', '10', '3990', '1000')
    assert 'secret' not in json.dumps(order)


@pytest.mark.asyncio
async def test_mcp_history_tool_delegates_to_authenticated_client(monkeypatch):
    from better_lavka_mcp import client
    from better_lavka_mcp.tools import account
    async def fake_history(self, count, older_than):
        return {"orders": [], "count": count, "cursor": older_than}
    monkeypatch.setattr(client.Lavka, "order_history", fake_history)
    result = await account.list_order_history(5, None)
    assert result == {"orders": [], "count": 5, "cursor": None}


@pytest.mark.asyncio
async def test_cancel_route_is_not_allowlisted_even_with_mock_transport():
    async def handler(request):
        pytest.fail("cancel request must not be sent")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError, match="allowlisted"):
            await Lavka(http).call("/api/v1/providers/orders/v1/cancel", {"orderId": "test"})
