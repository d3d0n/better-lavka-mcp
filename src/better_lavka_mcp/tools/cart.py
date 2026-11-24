"""Cart tools: read, change quantities, add a whole list and repeat a past order."""
from decimal import Decimal, InvalidOperation
from typing import TypedDict

import httpx

from better_lavka_mcp import client
from better_lavka_mcp.aliases import resolve
from better_lavka_mcp.app import _location, tool, uz_only
from better_lavka_mcp.cart import CartClient
from better_lavka_mcp.client import _number, clean_text, stock
from better_lavka_mcp.errors import UpstreamError
from better_lavka_mcp.headers import USER_AGENT


async def _cart_client(http, location, props=None):
    client_ = client.Lavka(http)
    props = props or await client_.bootstrap()
    client_._require_login(props)
    info = props.get('pageData', {}).get('serviceInfo', {})
    if not info:
        async with httpx.AsyncClient(timeout=25, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'}) as anonymous:
            info = (await client.Lavka(anonymous).bootstrap()).get('pageData', {}).get('serviceInfo', {})
    if info.get('currency') != 'UZS' or not info.get('currencySign'):
        raise UpstreamError('Uzbekistan currency and sign not confirmed by upstream')
    return client_, CartClient(http, props, location, info['currencySign'])


def _cart_view(cart):
    from better_lavka_mcp.cart import actual_prices, price_changes
    items = []
    current = actual_prices(cart)  # a repriced line still carries its old price until it is re-saved
    for item in cart['items']:
        if not isinstance(item, dict):
            raise UpstreamError('Unexpected cart item')
        items.append({'id': item.get('id'), 'title': clean_text(item.get('title')), 'quantity': item.get('quantity'),
                      'price_uzs': current.get(item.get('id'), item.get('price')), 'pack': item.get('amount'),
                      'available': item.get('isUnavailableOnDepot') is not True, **stock(item)})
    conditions = cart.get('orderConditions') if isinstance(cart.get('orderConditions'), dict) else {}
    return {'items': items, 'currency': 'UZS',
            'total_uzs': cart.get('totalPriceValue'),
            'delivery_uzs': conditions.get('deliveryCost'),
            'eta': conditions.get('eta'),
            'available_for_checkout': cart.get('availableForCheckout'),
            'checkout_unavailable_reason': cart.get('checkoutUnavailableReason'),
            'price_changes': [{**change, 'title': clean_text(change['title'])} for change in price_changes(cart)]}


def _quantity(value):
    """An exact cart quantity, or None when it cannot be trusted."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _display(number):
    """A JSON-safe quantity: int when whole, float for fractional weights."""
    return int(number) if number == number.to_integral_value() else float(number)


async def _add_products(cart_client, wanted, titles):
    """Add quantities on top of the cart, then drop what the store cannot sell and cap at its limits.

    Both writes run under one cart lock so another call can't change the cart in between.
    Weight items already in the cart with a fractional quantity are left untouched and reported as skipped."""
    async with cart_client.transaction():
        return await _add_and_correct(cart_client, wanted, titles)


async def _add_and_correct(cart_client, wanted, titles):
    fresh, outcomes = await cart_client.add_items(dict(wanted))
    by_id = {item.get('id'): item for item in fresh['items'] if isinstance(item, dict)}
    fixes, unavailable, limited, skipped = {}, [], [], []
    for pid, outcome in outcomes.items():
        item = by_id.get(pid, {})
        title = clean_text(item.get('title')) or titles.get(pid) or pid
        if outcome.get('skipped'):
            skipped.append({'title': title, 'reason': outcome['skipped']})
            continue
        if outcome['requested'] > 50:
            limited.append({'title': title, 'requested': _display(outcome['requested']), 'max': 50})
        limit = _number(item.get('quantityLimit'))
        if item.get('isUnavailableOnDepot') is True or limit == 0:
            fixes[pid] = 0
            unavailable.append(title)
        elif limit is not None and outcome['target'] > Decimal(str(limit)):
            fixes[pid] = int(limit)
            limited.append({'title': title, 'requested': _display(outcome['target']), 'max': _display(Decimal(str(limit)))})
    if fixes:
        fresh = await cart_client.set_items(fixes)
    final_by_id = {item.get('id'): item for item in fresh['items'] if isinstance(item, dict)}
    added = []
    for pid, outcome in outcomes.items():
        if outcome.get('skipped'):
            continue
        item = final_by_id.get(pid, {})
        after = _quantity(item.get('quantity'))
        if after is None:
            continue
        change = after - outcome['before']
        if change > 0 and after > 0:
            added.append({'product_id': pid, 'title': clean_text(item.get('title')) or titles.get(pid) or pid,
                          'added': _display(change), 'in_cart': _display(after)})
    return {'added': added, 'unavailable': unavailable, 'limited': limited, 'skipped': skipped, 'cart': _cart_view(fresh)}


@tool()
@uz_only
async def get_cart(longitude: float | None = None, latitude: float | None = None) -> dict:
    """What's in the cart: items, quantities, prices, delivery cost, ETA and total in UZS, for the address chosen with use_address (or at longitude, latitude)."""
    async with client.create_http() as http:
        location = _location(http, longitude, latitude)
        _, cart_client = await _cart_client(http, location)
        return _cart_view(await cart_client.retrieve())


@tool()
@uz_only
async def set_cart_quantity(product_id: str, quantity: int, longitude: float | None = None, latitude: float | None = None) -> dict:
    """Change the cart: set product_id to an exact quantity (0 removes it). Lavka applies its current price. Returns the updated cart."""
    async with client.create_http() as http:
        location = _location(http, longitude, latitude)
        _, cart_client = await _cart_client(http, location)
        return _cart_view(await cart_client.set_items({resolve(product_id): quantity}))


class CartRequest(TypedDict, total=False):
    query: str
    product_id: str
    quantity: int


@tool()
@uz_only
async def add_to_cart(items: list[CartRequest], longitude: float | None = None, latitude: float | None = None) -> dict:
    """Add several products in one go. Each item is {"query": "молоко 2.5%"} or {"product_id": "..."} with an optional "quantity" (default 1). A query adds what you ordered before, or the only match; otherwise nothing is added for it and needs_choice lists up to 5 options to pick from — ask the user, then call again with the product_id. Reports what was added, needs a choice, not found, unavailable or capped at the store limit."""
    if not isinstance(items, list) or not items or len(items) > 50:
        raise ValueError('Pass 1–50 items')
    wanted, titles, not_found, needs_choice = {}, {}, [], []
    async with client.create_http() as http:
        location = _location(http, longitude, latitude)
        lavka, cart_client = await _cart_client(http, location)
        bought = None
        for request in items:
            request = dict(request)
            quantity = request.get('quantity', 1)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 50:
                raise ValueError('Each quantity must be between 1 and 50')
            product_id = resolve(request.get('product_id'))
            if not product_id:
                query = request.get('query')
                if not isinstance(query, str) or not query.strip():
                    raise ValueError('Each item needs a query or a product_id')
                if bought is None:
                    bought = await _bought_before(lavka)
                found = await lavka.search(query, 8, tuple(location))
                candidates = [p for p in found['products'] if p.get('available') and p.get('id')]
                if not candidates:
                    not_found.append(query)
                    continue
                match = next((p for p in candidates if p['id'] in bought), candidates[0] if len(candidates) == 1 else None)
                if match is None:
                    needs_choice.append({'query': query, 'quantity': quantity,
                                         'options': [{'product_id': p['id'], 'title': p['title'], 'price_uzs': p['price'], 'pack': p.get('quantity')}
                                                     for p in candidates[:5]]})
                    continue
                product_id, titles[match['id']] = match['id'], match['title']
            wanted[product_id] = wanted.get(product_id, 0) + quantity
        if not wanted:
            return {'added': [], 'needs_choice': needs_choice, 'not_found': not_found, 'unavailable': [], 'limited': [], 'skipped': [], 'cart': _cart_view(await cart_client.retrieve())}
        result = await _add_products(cart_client, wanted, titles)
    return {**result, 'needs_choice': needs_choice, 'not_found': not_found}


async def _bought_before(lavka):
    """Product ids from recent orders; an unreadable history just means no preference."""
    try:
        history = await lavka.order_history(21)
    except (UpstreamError, ValueError):
        return set()
    return {line['product_id'] for order in history['orders'] for line in order['items']}


@tool()
@uz_only
async def repeat_order(order_id: str = 'last', longitude: float | None = None, latitude: float | None = None) -> dict:
    """Put everything from a past order back into the cart (order_id from list_order_history, or "last"). Lavka applies today's prices; items the store no longer has are left out and listed."""
    async with client.create_http() as http:
        location = _location(http, longitude, latitude)
        lavka, cart_client = await _cart_client(http, location)
        if order_id == 'last':
            history = await lavka.order_history(1)
            if not history['orders']:
                raise ValueError('There are no past orders yet')
            order_id = history['orders'][0]['order_id']
        order = await lavka.order(resolve(order_id))
        wanted, titles, skipped = {}, {}, []
        for line in order['items']:
            quantity = _quantity(line.get('quantity'))
            if quantity is not None and quantity != quantity.to_integral_value():
                skipped.append({'title': line['name'],
                                'reason': 'Historical quantity is fractional (weight item); it cannot be repeated as a unit line'})
                continue
            wanted[line['product_id']] = wanted.get(line['product_id'], 0) + max(int(quantity or 0), 1)
            titles[line['product_id']] = line['name']
        if not wanted:
            if not skipped:
                raise ValueError('That order has no products to repeat')
            result = {'added': [], 'unavailable': [], 'limited': [], 'skipped': skipped,
                      'cart': _cart_view(await cart_client.retrieve())}
        else:
            result = await _add_products(cart_client, wanted, titles)
            result['skipped'] = skipped + result['skipped']
    return {'order': {k: order[k] for k in ('order_id', 'created_at', 'total_uzs')}, **result}
