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
    return {'order_id': entry['id'], 'status': _text(entry.get('status')), 'resolution': _text(entry.get('resolution')),
            'payment_status': _text(entry.get('paymentStatus')), 'title': _text(info.get('title')),
            'subtitle': _text(info.get('subtitle')), 'cancelable': entry.get('isCancelable') is True,
            'price_uzs': _text(entry.get('clientPrice'))}


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
