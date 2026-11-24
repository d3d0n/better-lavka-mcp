"""Uzbekistan cart access: read the cart and set absolute item quantities."""
import asyncio
import contextlib
import uuid
import weakref
from decimal import Decimal, InvalidOperation

from better_lavka_mcp.constants import BASE
from better_lavka_mcp.errors import UpstreamError
from better_lavka_mcp.headers import api_headers

# Lavka rejects an empty line price but reprices every line to the current
# catalog price (verified live on lavka.yandex.uz for new and existing items).
PLACEHOLDER_PRICE = '1'

# One cart write at a time per event loop: read, update and read-back must not interleave.
_write_locks = weakref.WeakKeyDictionary()


def _write_lock():
    loop = asyncio.get_running_loop()
    lock = _write_locks.get(loop)
    if lock is None:
        lock = _write_locks[loop] = asyncio.Lock()
    return lock


def _diff_lines(cart):
    lines = cart.get('diffDataProducts')
    return [line for line in lines if isinstance(line, dict) and isinstance(line.get('id'), str)] if isinstance(lines, list) else []


def actual_prices(cart):
    """Lavka's current price per product for lines whose price changed after they were added."""
    return {line['id']: str(line['price']) for line in _diff_lines(cart) if isinstance(line.get('price'), (int, str)) and str(line['price'])}


def price_changes(cart):
    """[{'id', 'title', 'old_uzs', 'new_uzs'}] for lines Lavka repriced since they were added."""
    def amount(value):
        digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
        return digits or None
    return [{'id': line['id'], 'title': line.get('title'), 'old_uzs': amount(line.get('oldPriceWithSign')), 'new_uzs': amount(line.get('price'))}
            for line in _diff_lines(cart)]


class CartClient:
    _held = False  # True while this client holds the write lock inside transaction()

    def __init__(self, http, props, location, currency_sign):
        if props.get('pageData', {}).get('passportInfo', {}).get('status') != 'VALID':
            raise UpstreamError('An authenticated Uzbekistan session is required')
        if not isinstance(location, (list, tuple)) or len(location) != 2 or not (55 <= location[0] <= 74 and 37 <= location[1] <= 46):
            raise ValueError('A valid Uzbekistan location is required')
        if not currency_sign or currency_sign in ('₽', 'руб'):
            raise ValueError('Uzbekistan currency sign is required')
        self.http, self.props, self.location, self.currency_sign = http, props, list(location), currency_sign

    # The site's own checkout-page calls; they take snake_case bodies and a one-time X-Idempotency-Token.
    SITE_WRITES = ('/api/v2/lavka/v1/cart/v1/set-payment', '/api/v2/lavka/v1/cart/v1/set-cashback-flow',
                   '/api/v2/lavka/v1/cart/v1/apply-promocode')
    SITE_READS = ('/api/v2/lavka/v1/cart/v1/promocodes-list',)

    async def _post(self, path, body):
        if path not in ('/api/v1/providers/cart/v1/retrieve', '/api/v1/providers/cart/v1/update', *self.SITE_WRITES, *self.SITE_READS):
            raise ValueError('Endpoint is not allowlisted')
        p = self.props
        headers = api_headers(p)
        if path in self.SITE_WRITES or path in self.SITE_READS:
            headers['X-Idempotency-Token'] = uuid.uuid4().hex
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

    async def prepare_checkout(self, card, cashback_flow=None):
        """Put the card and the cashback mode on the cart, as the site's checkout page does when it opens.

        Seen live: a cart without a payment method was charged and then canceled by Lavka, while the
        same request on a cart the site had prepared went through. Writes only what differs; returns the cart.
        A cashback mode the user already chose (e.g. spending points) is left alone.
        """
        async with contextlib.AsyncExitStack() as stack:
            if not self._held:
                await stack.enter_async_context(_write_lock())
            cart = await self.retrieve()
            method = cart.get('paymentMethod') if isinstance(cart.get('paymentMethod'), dict) else {}
            if (method.get('type'), method.get('id')) != (card.get('type'), card.get('id')):
                await self._post(self.SITE_WRITES[0], self._site_body(cart, payment_method={'type': card['type'], 'id': card['id']}))
                cart = await self.retrieve()
                method = cart.get('paymentMethod') if isinstance(cart.get('paymentMethod'), dict) else {}
                if (method.get('type'), method.get('id')) != (card.get('type'), card.get('id')):
                    raise UpstreamError('Lavka did not keep the chosen card on the cart')
            cashback = cart.get('cashback') if isinstance(cart.get('cashback'), dict) else {}
            # 'charge' spends Plus points on this order (Lavka lowers the total), 'gain' earns them; with no
            # request, a mode the user already chose stays and an unset one becomes 'gain', as on the site.
            wanted = cashback_flow or cashback.get('flow') or 'gain'
            if cashback.get('exist') is True and cashback.get('flow') != wanted:
                await self._post(self.SITE_WRITES[1], self._site_body(cart, cashback_flow=wanted))
                cart = await self.retrieve()
            return cart

    async def apply_promocode(self, code):
        """Put a promo code on the cart, as the site's checkout does; returns the cart. Lavka keeps one code per cart."""
        if not isinstance(code, str) or not code.strip() or len(code) > 64:
            raise ValueError('A promo code is required')
        async with contextlib.AsyncExitStack() as stack:
            if not self._held:
                await stack.enter_async_context(_write_lock())
            cart = await self.retrieve()
            try:
                await self._post(self.SITE_WRITES[2], self._site_body(cart, promocode=code.strip()))
            except UpstreamError:
                raise ValueError("Lavka didn't accept that promo code for this cart") from None
            cart = await self.retrieve()
            applied = cart.get('promocode') if isinstance(cart.get('promocode'), dict) else {}
            if str(applied.get('code') or '').casefold() != code.strip().casefold():
                raise ValueError("Lavka didn't keep that promo code on the cart")
            return cart

    async def promocodes(self):
        """The user's promo codes and coupons Lavka offers for this address: [{title, subtitle, code, valid, expires}]."""
        raw = await self._post(self.SITE_READS[0], {'position': {'location': self.location}})
        found = raw.get('promocodes') if isinstance(raw.get('promocodes'), list) else []
        return [{'title': p.get('title'), 'subtitle': p.get('subtitle'), 'code': p.get('promocode'), 'valid': p.get('valid') is True,
                 'expires': p.get('expire_at')} for p in found if isinstance(p, dict) and isinstance(p.get('promocode'), str)]

    def _site_body(self, cart, **fields):
        return {'cart_id': cart['cartId'], 'cart_version': cart['cartVersion'], 'position': {'location': self.location},
                'is_supermarket': False, **fields}

    async def set_items(self, quantities, add=False):
        """Set absolute quantities for several products in one update; 0 removes.

        With add=True the numbers are added to what the cart holds, computed from
        the same snapshot whose cartVersion is sent (capped at 50). Fractional
        quantities already in the cart stay Decimal (1.5 + 1 -> 2.5), never int().
        Lavka requires a non-empty price per line but reprices every line itself,
        so the current cart price is reused and new products get a placeholder.
        """
        if not isinstance(quantities, dict) or not quantities:
            raise ValueError('At least one product is required')
        lowest = 1 if add else 0
        for product_id, quantity in quantities.items():
            if not isinstance(product_id, str) or not product_id or isinstance(quantity, bool) or not isinstance(quantity, int) or not lowest <= quantity <= 50:
                raise ValueError(f'Each product needs an id and a quantity between {lowest} and 50')
        fresh, _ = await self._locked_write(quantities, add)
        return fresh

    async def add_items(self, increments):
        """Add integer quantities and report what each line actually did.

        Returns (cart, outcomes): outcomes[product_id] holds Decimal before,
        requested (before + increment), target (requested capped at 50), added
        (read-back quantity minus before) and skipped when the existing quantity
        is fractional — a weight item, not safely writable as a 'unit' line.
        Increments may exceed 50 so the caller can report the per-line cap.
        """
        if not isinstance(increments, dict) or not increments:
            raise ValueError('At least one product is required')
        for product_id, quantity in increments.items():
            if not isinstance(product_id, str) or not product_id or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise ValueError('Each product needs an id and a positive integer quantity')
        return await self._locked_write(increments, True, skip_fractional=True)

    @contextlib.asynccontextmanager
    async def transaction(self):
        """Hold the cart write lock across several set_items calls, e.g. add and then cap at store limits."""
        async with _write_lock():
            self._held = True
            try:
                yield self
            finally:
                self._held = False

    async def accept_price_changes(self):
        """Re-save the lines whose price changed since they were added, at Lavka's current price.

        This is what the Lavka site does on "price-mismatch". Returns the changes it accepted,
        [{'id', 'title', 'old_uzs', 'new_uzs'}], or [] when nothing was pending.
        """
        async with contextlib.AsyncExitStack() as stack:
            if not self._held:
                await stack.enter_async_context(_write_lock())
            cart = await self.retrieve()
            changes = price_changes(cart)
            if cart.get('checkoutUnavailableReason') != 'price-mismatch' or not changes:
                return []
            quantities = {}
            for line in cart['diffDataProducts']:
                if line.get('quantityType') != 'unit':
                    raise UpstreamError('A weighed item changed price; accept it in the Lavka app')
                quantities[line['id']] = Decimal(str(line.get('quantity')))
            fresh, _ = await self._write(quantities, add=False)
            if fresh.get('checkoutUnavailableReason') == 'price-mismatch':
                raise UpstreamError('Lavka still reports changed prices after re-saving them')
            return changes

    async def _locked_write(self, quantities, add, skip_fractional=False):
        if self._held:
            return await self._write(quantities, add, skip_fractional=skip_fractional)
        async with _write_lock():
            return await self._write(quantities, add, skip_fractional=skip_fractional)

    async def _write(self, quantities, add, skip_fractional=False):
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
        outcomes, written = {}, {}
        for product_id, quantity in quantities.items():
            start = before.get(product_id, Decimal(0))
            requested = start + quantity if add else Decimal(quantity)
            target = min(requested, Decimal(50)) if add else requested
            outcomes[product_id] = {'before': start, 'requested': requested, 'target': target, 'added': Decimal(0)}
            if skip_fractional and start != start.to_integral_value():
                outcomes[product_id]['skipped'] = 'Cart already holds a fractional (weight) quantity; it was not changed'
                continue
            written[product_id] = target
        if not written:
            return cart, outcomes
        prices = {item['id']: str(item.get('price') or '') for item in cart['items']}
        prices.update(actual_prices(cart))  # a line written at a stale price keeps the cart in price-mismatch
        lines = [{'id': product_id, 'quantity': str(quantity), 'pricePerCount': '1', 'quantityType': 'unit',
                  'price': prices.get(product_id) or PLACEHOLDER_PRICE, 'title': '', 'currency': 'UZS'}
                 for product_id, quantity in written.items()]
        body = {'items': lines,
                'deliveryTimeInfo': cart.get('deliveryTimeInfo'), 'deliveryType': cart.get('deliveryType'),
                'position': {'location': self.location}, 'additionalData': {}, 'cartId': cart['cartId'],
                'cartVersion': cart['cartVersion'], 'idempotencyToken': uuid.uuid4().hex,
                'isUserOrderEdit': False, 'depotType': 'regular'}
        await self._post('/api/v1/providers/cart/v1/update', body)
        fresh = await self.retrieve()
        if fresh['cartId'] != cart['cartId']:
            raise UpstreamError('Cart identity changed during update; outcome is uncertain')
        final = counts(fresh['items'])
        for product_id, quantity in written.items():
            if final.get(product_id, 0) != quantity:
                raise UpstreamError('Cart readback did not verify requested item quantity')
        for product_id, outcome in outcomes.items():
            if product_id in written:
                outcome['added'] = final.get(product_id, Decimal(0)) - outcome['before']
        remaining_before = {pid: qty for pid, qty in before.items() if pid not in written}
        remaining_final = {pid: qty for pid, qty in final.items() if pid not in written}
        if remaining_before != remaining_final:
            raise UpstreamError('Unrelated cart items changed during update; outcome is uncertain')
        return fresh, outcomes
