import json

import pytest

from better_lavka_mcp import render
from better_lavka_mcp.orders.tracking import TrackingShapeError, project_tracking_response

# Synthetic entry shaped like the live tracked-orders/state answer for an order Lavka canceled after creating it.
CANCELED = {'id': 'order-1', 'shortOrderId': '000000-111-3333', 'status': 'closed', 'resolution': 'canceled',
            'paymentStatus': 'unknown', 'isCancelable': False, 'clientPrice': '0',
            'trackingInfo': {'title': 'Заказ отменён', 'subtitle': 'Вернём деньги на карту'},
            'contact': {'phone': '+998 secret'}, 'couriers': [{'name': 'secret courier'}],
            'depots': [{'depotInfo': {'directions': 'secret directions'}}]}


def test_tracked_order_keeps_lavkas_verdict_and_drops_personal_data():
    result = project_tracking_response([CANCELED])
    assert result['orders'] == [{'order_id': 'order-1', 'status': 'closed', 'resolution': 'canceled', 'payment_status': 'unknown',
                                 'title': 'Заказ отменён', 'subtitle': 'Вернём деньги на карту', 'cancelable': False,
                                 'price_uzs': '0'}]
    assert 'secret' not in json.dumps(result, ensure_ascii=False)
    assert result['reconciles_uncertain_submission'] is False


def test_tracked_orders_read_as_lavka_tells_the_user():
    text = render.render('list_active_orders', project_tracking_response([CANCELED]))
    assert 'Lavka says: “Заказ отменён — Вернём деньги на карту” (closed/canceled).' in text
    assert 'Can still be canceled' not in text
    assert 'tracking no orders' in render.render('list_active_orders', project_tracking_response([]))


@pytest.mark.parametrize('data', [{}, {'orders': []}, None, 'secret', 0, False, [{'status': 'closed'}], ['secret']])
def test_other_shapes_fail_without_leaking_data(data):
    with pytest.raises(TrackingShapeError) as exc:
        project_tracking_response(data)
    assert 'secret' not in str(exc.value)
