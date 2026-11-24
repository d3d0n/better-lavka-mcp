"""Order tracking as Lavka's own tracking screen shows it: status, outcome and the text it tells the user.

Only a fixed set of fields is kept; couriers, addresses, phones and maps are dropped.
"""
from better_lavka_mcp.constants import BASE
from better_lavka_mcp.errors import UpstreamError
from better_lavka_mcp.headers import api_headers


class TrackingShapeError(ValueError):
    """The response is not a list of tracked orders."""


def _text(value):
    return value.strip()[:200] if isinstance(value, str) and value.strip() else None


def project_order(entry):
    """One tracked order. `status`/`resolution` are Lavka's words (e.g. closed/canceled), passed on as is."""
    if not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or not entry['id']:
        raise TrackingShapeError('Tracked order without an id')
    info = entry.get('trackingInfo') if isinstance(entry.get('trackingInfo'), dict) else {}
    eta = entry.get('deliveryEtaMin')
    actions = [a.get('type') for a in entry.get('actions') or [] if isinstance(a, dict) and isinstance(a.get('type'), str)]
    special = entry.get('specialConditions') if isinstance(entry.get('specialConditions'), dict) else {}
    depot = {}
    for candidate in entry.get('depots') or []:
        found = candidate.get('depotInfo') if isinstance(candidate, dict) else None
        if isinstance(found, dict):
            depot = found
            break
    return {'order_id': entry['id'], 'status': _text(entry.get('status')), 'resolution': _text(entry.get('resolution')),
            'payment_status': _text(entry.get('paymentStatus')), 'title': _text(info.get('title')),
            'subtitle': _text(info.get('subtitle')), 'cancelable': entry.get('isCancelable') is True,
            'price_uzs': _text(entry.get('clientPrice')), 'original_price_uzs': _text(entry.get('originalPrice')),
            # What the user and support see: the order number, the promised time and how far along it is.
            'number': _text(entry.get('shortOrderId')), 'promise': _text(entry.get('localizedPromise')),
            'promised_by': _text(entry.get('promiseMax') or entry.get('deliveryPromiseTs')),
            'eta_minutes': eta if isinstance(eta, int) and not isinstance(eta, bool) and eta >= 0 else None,
            'step': entry.get('statusStep') if isinstance(entry.get('statusStep'), int) else None,
            'status_since': _text(entry.get('statusUpdated')),
            'can_edit_address': 'edit_address' in actions, 'heavy': special.get('heavyOrder') is True,
            'depot': _text(depot.get('shortAddress')), 'courier': _courier(entry)}


def _courier(entry):
    """Courier transport and, once Lavka shares it, the distance to the door; no name, phone or route."""
    info = entry.get('trackingInfo') if isinstance(entry.get('trackingInfo'), dict) else {}
    kind = _text(info.get('courierImageType'))
    destination = entry.get('location') if isinstance(entry.get('location'), list) else None
    position = None
    points = entry.get('mapPointsV2') if isinstance(entry.get('mapPointsV2'), dict) else {}
    for courier in points.get('couriers') or []:
        if isinstance(courier, dict) and isinstance(courier.get('coordinates'), list) and len(courier['coordinates']) == 2:
            position = courier['coordinates']  # [lon, lat], like every other point here
            break
    moving = _dict(_dict(entry.get('trackingMapInfo')).get('courier'))
    spot = _dict(moving.get('position'))
    if position is None and isinstance(spot.get('lon'), (int, float)) and isinstance(spot.get('lat'), (int, float)):
        position = [spot['lon'], spot['lat']]
    if not kind and position is None:
        return None
    distance = None
    if position is not None and destination and len(destination) == 2:
        try:
            distance = round(_meters(position, destination))
        except (TypeError, ValueError):
            distance = None
    return {'transport': kind, 'distance_m': distance}


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _meters(a, b):
    from math import asin, cos, radians, sin, sqrt
    lon1, lat1, lon2, lat2 = map(radians, (float(a[0]), float(a[1]), float(b[0]), float(b[1])))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * asin(sqrt(h))


def project_tracking_response(data):
    """The tracked-orders feed: recent orders Lavka still tracks, closed ones included."""
    if not isinstance(data, list):
        raise TrackingShapeError('Unrecognized tracking response; no conclusion possible')
    orders = [project_order(entry) for entry in data]
    return {'orders': orders, 'history_complete': False, 'reconciles_uncertain_submission': False}


async def tracking_state(http, props, order_ids):
    """Tracking for the given orders, keyed by order id (orders Lavka no longer tracks are absent)."""
    try:
        response = await http.get(BASE + '/api/v1/providers/orders-tracking/v1/state', headers=api_headers(props),
                                  params=[('ids[]', order_id) for order_id in order_ids])
    except Exception:
        raise UpstreamError('Tracking request failed') from None
    if response.status_code != 200:
        raise UpstreamError(f'Tracking HTTP {response.status_code}')
    try:
        data = response.json()
    except ValueError:
        raise UpstreamError('Tracking returned non-JSON') from None
    try:
        return {order['order_id']: order for order in project_tracking_response(data)['orders']}
    except TrackingShapeError:
        raise UpstreamError('Unexpected tracking response') from None
