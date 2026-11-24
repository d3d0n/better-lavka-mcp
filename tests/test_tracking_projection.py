import json

import pytest

from better_lavka_mcp import render
from better_lavka_mcp.orders.tracking import TrackingShapeError, project_tracking_response

# Synthetic entry shaped like the live tracked-orders/state answer for an order Lavka canceled after creating it.
CANCELED = {'id': 'order-1', 'shortOrderId': '000000-111-3333', 'status': 'closed', 'resolution': 'canceled',
            'paymentStatus': 'unknown', 'isCancelable': False, 'clientPrice': '0', 'originalPrice': '218560',
            'trackingInfo': {'title': 'Заказ отменён', 'subtitle': 'Вернём деньги на карту'},
            'contact': {'phone': '+998 secret'}, 'couriers': [{'name': 'secret courier'}],
            'depots': [{'depotInfo': {'directions': 'secret directions'}}]}


def test_tracked_order_keeps_lavkas_verdict_and_drops_personal_data():
    result = project_tracking_response([CANCELED])
    order = result['orders'][0]
    assert {k: order[k] for k in ('order_id', 'status', 'resolution', 'title', 'subtitle', 'cancelable', 'price_uzs', 'number')} == {
        'order_id': 'order-1', 'status': 'closed', 'resolution': 'canceled', 'title': 'Заказ отменён',
        'subtitle': 'Вернём деньги на карту', 'cancelable': False, 'price_uzs': '0', 'number': '000000-111-3333'}
    assert 'secret' not in json.dumps(result, ensure_ascii=False)
    assert result['reconciles_uncertain_submission'] is False


def test_tracked_orders_read_as_lavka_tells_the_user():
    text = render.render('list_active_orders', project_tracking_response([CANCELED]))
    assert 'Lavka says: “Заказ отменён — Вернём деньги на карту” (closed/canceled).' in text
    # A price of 0 on a canceled order is not proof that nothing is held on the card.
    assert "Lavka's price for it is now 0 (was 218 560 сум); whether the card was refunded isn't shown here." in text
    assert 'charged' not in text
    assert 'Can still be canceled' not in text
    assert 'tracking no orders' in render.render('list_active_orders', project_tracking_response([]))


@pytest.mark.parametrize('data', [{}, {'orders': []}, None, 'secret', 0, False, [{'status': 'closed'}], ['secret']])
def test_other_shapes_fail_without_leaking_data(data):
    with pytest.raises(TrackingShapeError) as exc:
        project_tracking_response(data)
    assert 'secret' not in str(exc.value)


# Synthetic entry shaped like a live order on its way (fields from the live tracking state).
DELIVERING = {'id': 'order-2', 'shortOrderId': '000000-111-2222', 'status': 'delivering', 'resolution': None,
              'paymentStatus': 'success', 'isCancelable': False, 'clientPrice': '220560', 'originalPrice': '220560',
              'deliveryEtaMin': 7, 'localizedPromise': 'Заказ приедет к ~19:55', 'promiseMax': '2026-11-24T14:55:00+00:00',
              'statusStep': 3, 'actions': [{'type': 'edit_address'}], 'specialConditions': {'heavyOrder': True},
              'location': [69.24000, 41.31000],
              'trackingInfo': {'title': 'Доставим до ~19:55', 'subtitle': 'Курьер в пути', 'courierImageType': 'bicycle'},
              'mapPointsV2': {'couriers': [{'coordinates': [69.2459, 41.3100], 'animationRoute': [[1, 2]]}]},
              'couriers': [{'name': 'secret courier', 'phone': '+998 secret'}]}


def test_order_on_its_way_shows_time_left_courier_distance_and_number():
    text = render.render('list_active_orders', project_tracking_response([DELIVERING]))
    assert '“Доставим до ~19:55 — Курьер в пути” (delivering).' in text
    # ~0.49 km between the courier and the door, from the [lon, lat] points Lavka sends.
    assert 'About 7 min to go; courier on bicycle 493 m away; heavy order; order № 000000-111-2222.' in text
    assert 'Can still be given another address.' in text and 'canceled' not in text
    assert 'secret' not in json.dumps(project_tracking_response([DELIVERING]), ensure_ascii=False)
