"""Read-only Uzbekistan saved-card lookup. Never sends an order or payment."""
import math
import re
from dataclasses import dataclass, field

from better_lavka_mcp.constants import BASE
from better_lavka_mcp.errors import UpstreamError
from better_lavka_mcp.headers import api_headers


@dataclass
class PaymentResults:
    masked_cards: list[str]
    _ids: list[str] = field(repr=False)

    def select(self, index):
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(self._ids):
            raise ValueError('Invalid card selection')
        return {'type': 'card', 'id': self._ids[index]}

class PaymentClient:
    def __init__(self, http, props, location):
        if props.get('pageData', {}).get('passportInfo', {}).get('status') != 'VALID':
            raise UpstreamError('An authenticated Uzbekistan session is required')
        if (not isinstance(location, (list, tuple)) or len(location) != 2 or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in location) or
            not (55 <= location[0] <= 74 and 37 <= location[1] <= 46)):
            raise ValueError('A valid Uzbekistan location is required')
        self.http, self.props, self.location = http, props, list(location)

    async def lookup(self):
        p = self.props
        headers = api_headers(p)
        try:
            r = await self.http.post(BASE + '/api/v1/providers/payments/v1/methods', headers=headers,
                                     json={'location': self.location, 'countryIso3': 'UZB'})
        except Exception:
            raise UpstreamError('Payment lookup request failed') from None
        if r.status_code != 200:
            raise UpstreamError(f'Payment lookup HTTP {r.status_code}')
        try:
            data = r.json()
        except ValueError:
            raise UpstreamError('Payment lookup returned non-JSON') from None
        if not isinstance(data, dict) or not isinstance(data.get('methods'), list):
            raise UpstreamError('Unexpected payment lookup response')
        masked, ids = [], []
        for method in data['methods']:
            if not isinstance(method, dict):
                raise UpstreamError('Unexpected payment method')
            if method.get('type') != 'card' or not isinstance(method.get('availability'), dict) or method['availability'].get('available') is not True:
                continue
            number, card_id = method.get('number'), method.get('id')
            if not isinstance(number, str) or not isinstance(card_id, str) or not card_id:
                raise UpstreamError('Available card is missing required details')
            match = re.search(r'([0-9]{4})\s*$', number)
            if not match:
                raise UpstreamError('Available card number cannot be safely masked')
            masked.append('**** ' + match.group(1))
            ids.append(card_id)
        return PaymentResults(masked, ids)


# The site stops polling at fail, success, cancel and need_to_skip; the rest are older or alternative spellings.
TERMINAL_PAYMENT_STATUSES = frozenset({'success', 'need_to_skip', 'paid', 'hold', 'fail', 'failed', 'rejected', 'cancel', 'canceled', 'cancelled'})


async def payment_status(http, props, order_id):
    """Read one order's payment status; returns status and a 3-D Secure redirect when the bank asks for one."""
    if not isinstance(order_id, str) or not order_id or len(order_id) > 128:
        raise ValueError('An order id is required')
    headers = api_headers(props)
    try:
        r = await http.post(BASE + '/api/v1/providers/payments/v1/status', headers=headers,
                            json={'orderId': order_id, 'paymentType': 'card'})
    except Exception:
        raise UpstreamError('Payment status request failed') from None
    if r.status_code != 200:
        raise UpstreamError(f'Payment status HTTP {r.status_code}')
    try:
        data = r.json()
    except ValueError:
        raise UpstreamError('Payment status returned non-JSON') from None
    if not isinstance(data, dict) or not isinstance(data.get('status'), str):
        raise UpstreamError('Unexpected payment status response')
    payload = data.get('payload')
    payload = payload if isinstance(payload, dict) else {}
    redirect = payload.get('redirectUrl') or data.get('redirectUrl')
    result = {'payment_status': data['status'], 'redirect_url': redirect if isinstance(redirect, str) and redirect.startswith('https://') else None}
    # The site shows this title and text to the user when a card payment fails.
    if payload.get('type') == 'error':
        result['payment_error'] = {k: payload[src].strip()[:200] for k, src in (('title', 'errorTitle'), ('text', 'errorSubtitle'))
                                   if isinstance(payload.get(src), str) and payload[src].strip()}
    return result
