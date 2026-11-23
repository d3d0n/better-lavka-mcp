"""Uzbekistan cart access: read the cart and set absolute item quantities."""
import uuid
from decimal import Decimal, InvalidOperation

from better_lavka_mcp.constants import BASE
from better_lavka_mcp.errors import UpstreamError

# Lavka rejects an empty line price but reprices every line to the current
# catalog price (verified live on lavka.yandex.uz for new and existing items).
PLACEHOLDER_PRICE = '1'


class CartClient:
    def __init__(self, http, props, location, currency_sign):
        if props.get('pageData', {}).get('passportInfo', {}).get('status') != 'VALID':
            raise UpstreamError('An authenticated Uzbekistan session is required')
        if not isinstance(location, (list, tuple)) or len(location) != 2 or not (55 <= location[0] <= 74 and 37 <= location[1] <= 46):
            raise ValueError('A valid Uzbekistan location is required')
        if not currency_sign or currency_sign in ('₽', 'руб'):
            raise ValueError('Uzbekistan currency sign is required')
        self.http, self.props, self.location, self.currency_sign = http, props, list(location), currency_sign

    async def _post(self, path, body):
        if path not in ('/api/v1/providers/cart/v1/retrieve', '/api/v1/providers/cart/v1/update'):
            raise ValueError('Endpoint is not allowlisted')
        p = self.props
        headers = {'Origin': BASE, 'Referer': BASE + '/', 'X-Requested-With': 'XMLHttpRequest',
                   'X-Csrf-Token': p.get('authProxyCsrfToken') or '', 'X-Csrf-Token-Bff': p.get('csrfToken') or ''}
        try:
            r = await self.http.post(BASE + path, headers=headers, json=body)
        except Exception:
            raise UpstreamError('Cart request failed; update outcome may be unknown') from None
        if r.status_code != 200:
            raise UpstreamError(f'Cart endpoint HTTP {r.status_code}')
        try:
            result = r.json()
        except ValueError:
            raise UpstreamError('Cart endpoint returned non-JSON') from None
        if not isinstance(result, dict) or result.get('type') == 'captcha':
            raise UpstreamError('Cart endpoint returned CAPTCHA or unexpected data')
        return result

    async def retrieve(self):
        raw = await self._post('/api/v1/providers/cart/v1/retrieve', {'depotType': 'regular', 'currencySign': self.currency_sign, 'position': {'location': self.location}})
        cart = raw.get('cart', raw)
        if not isinstance(cart, dict) or not isinstance(cart.get('items'), list) or not cart.get('cartId') or cart.get('cartVersion') is None:
            raise UpstreamError('Unexpected cart response')
        return cart

    async def set_items(self, quantities):
        """Set absolute quantities for several products in one update; 0 removes.

        Lavka requires a non-empty price per line but reprices every line itself,
        so the current cart price is reused and new products get a placeholder.
        """
        if not isinstance(quantities, dict) or not quantities:
            raise ValueError('At least one product is required')
        for product_id, quantity in quantities.items():
            if not isinstance(product_id, str) or not product_id or isinstance(quantity, bool) or not isinstance(quantity, int) or not 0 <= quantity <= 50:
                raise ValueError('Each product needs an id and a quantity between 0 and 50')

        def counts(items):
            result = {}
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or item['id'] in result:
                    raise UpstreamError('Cart items cannot be safely verified')
                try:
                    count = Decimal(str(item.get('quantity')))
                    if not count.is_finite() or count < 0:
                        raise ValueError
                except (InvalidOperation, ValueError):
                    raise UpstreamError('Cart item quantity cannot be safely verified') from None
                result[item['id']] = count
            return result

        cart = await self.retrieve()
        before = counts(cart['items'])
        prices = {item['id']: str(item.get('price') or '') for item in cart['items']}
        lines = [{'id': product_id, 'quantity': str(quantity), 'pricePerCount': '1', 'quantityType': 'unit',
                  'price': prices.get(product_id) or PLACEHOLDER_PRICE, 'title': '', 'currency': 'UZS'}
                 for product_id, quantity in quantities.items()]
        body = {'items': lines,
                'deliveryTimeInfo': cart.get('deliveryTimeInfo'), 'deliveryType': cart.get('deliveryType'),
                'position': {'location': self.location}, 'additionalData': {}, 'cartId': cart['cartId'],
                'cartVersion': cart['cartVersion'], 'idempotencyToken': uuid.uuid4().hex,
                'isUserOrderEdit': False, 'depotType': 'regular'}
        await self._post('/api/v1/providers/cart/v1/update', body)
        fresh = await self.retrieve()
        if fresh['cartId'] != cart['cartId']:
            raise UpstreamError('Cart identity changed during update; outcome is uncertain')
        after = counts(fresh['items'])
        for product_id, quantity in quantities.items():
            if after.get(product_id, 0) != quantity:
                raise UpstreamError('Cart readback did not verify requested item quantity')
            before.pop(product_id, None)
            after.pop(product_id, None)
        if before != after:
            raise UpstreamError('Unrelated cart items changed during update; outcome is uncertain')
        return fresh
