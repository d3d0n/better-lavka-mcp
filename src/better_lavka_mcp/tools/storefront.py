"""Storefront status and product search for every supported region."""
import asyncio

import httpx

from better_lavka_mcp import client
from better_lavka_mcp.app import _current_delivery, _location, selected_region, tool, uz_only
from better_lavka_mcp.headers import USER_AGENT


async def _cart_summary(http, delivery, props):
    from better_lavka_mcp.tools.cart import _cart_client, _cart_view
    if delivery is None:
        return {'state': 'no_address'}
    _, cart_client = await _cart_client(http, list(delivery['location']), props)
    cart = _cart_view(await cart_client.retrieve())
    return {'state': 'known', 'item_count': len(cart['items']), 'total_uzs': cart['total_uzs'],
            'available_for_checkout': cart['available_for_checkout'], 'checkout_unavailable_reason': cart['checkout_unavailable_reason']}


async def _latest_order(lavka):
    orders = (await lavka.order_history(1))['orders']
    if not orders:
        return {'state': 'none'}
    o = orders[0]
    # The status is Lavka's own text, passed on as is: it is the latest known one, not proof the order is active or done.
    return {'state': 'known', 'order_id': o['order_id'], 'created_at': o.get('created_at'),
            'status': o.get('status'), 'status_text': o.get('status_text'), 'total_uzs': o.get('total_uzs')}


@tool()
async def get_storefront_status(country: str | None = None) -> dict:
    """Start here: is Lavka open, delivery time, sign-in, chosen address, cart and latest order."""
    country = selected_region() if country is None else country
    if country not in ('UZ', 'RU', 'KZ'):
        raise ValueError('Supported countries: RU, KZ, UZ')
    if country != 'UZ':
        from better_lavka_mcp.region import RegionalStorefront
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'}) as http:
            return await RegionalStorefront(country, http).status()
    async with client.create_http() as http:
        lavka = client.Lavka(http)
        p = await lavka.bootstrap()  # one homepage read shared by the cart and history below
        data = p.get("pageData", {})
        service = data.get("serviceInfo", {})
        result = {"currency": service.get("currency"), "currency_sign": service.get("currencySign"), "advertised_delivery_time": service.get("deliveryTimeText"), "is_open": service.get("isOpen"), "is_disabled": service.get("isDisabled"), "passport_status": data.get("passportInfo", {}).get("status"), "address_coverage_verified": False, "note": "Homepage default storefront only; not a quote for your address."}
        if result['passport_status'] != 'VALID':
            return result
        delivery = _current_delivery(http)
        result['delivery_address'] = None if delivery is None else {'label': delivery.get('label'), 'short_address': delivery.get('short_address')}
        cart, latest = await asyncio.gather(_cart_summary(http, delivery, p), _latest_order(lavka), return_exceptions=True)
        for outcome in (cart, latest):
            if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
                raise outcome  # cancellation and interrupts are not "Lavka didn't answer"
        # A failed read is reported as unknown, never left out: a missing line would read as an empty cart or no order.
        result['cart'] = {'state': 'unknown'} if isinstance(cart, Exception) else cart
        result['latest_order'] = {'state': 'unknown'} if isinstance(latest, Exception) else latest
        return result


@tool()
async def search_products(query: str = '', limit: int = 20, longitude: float | None = None, latitude: float | None = None, country: str | None = None) -> dict:
    """Find products with price, discount and stock; favorites and ones bought before are marked. Empty query = the user's favorites and usual buys. RU/KZ: pass country and coordinates. limit 1–50."""
    if (longitude is None) != (latitude is None):
        raise ValueError("Both longitude and latitude are required")
    country = selected_region() if country is None else country
    if country not in ('UZ', 'RU', 'KZ'):
        raise ValueError('Supported countries: RU, KZ, UZ')
    if country != 'UZ':
        if longitude is None:
            raise ValueError('Both longitude and latitude are required')
        if not query.strip():
            raise ValueError('A query is required outside Uzbekistan')
        from better_lavka_mcp.region import RegionalStorefront
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'}) as http:
            return await RegionalStorefront(country, http).search(query, limit, longitude, latitude)
    async with client.create_http() as http:
        location = tuple(_location(http, longitude, latitude))
        lavka = client.Lavka(http)
        if not query.strip():
            # Hearted products first, then what was bought here, most recent first; each kept once.
            hearted, bought = await asyncio.gather(lavka.favorites(location), lavka.recents(location), return_exceptions=True)
            if isinstance(hearted, BaseException) and isinstance(bought, BaseException):
                raise bought
            hearted = [] if isinstance(hearted, BaseException) else hearted
            bought = [] if isinstance(bought, BaseException) else bought
            bought_ids, usual, seen = {p['id'] for p in bought}, [], set()
            for p in [*({**p, 'favorite': True} for p in hearted), *bought]:
                if p['id'] not in seen:
                    seen.add(p['id'])
                    usual.append({**p, 'price': p['price_uzs'], 'quantity': p['pack'], 'bought_before': p['id'] in bought_ids})
            return {'usual': True, 'products': usual[:limit]}
        found, hearted, bought = await asyncio.gather(lavka.search(query, limit, location), lavka.favorites(location),
                                                      lavka.recents(location), return_exceptions=True)
        if isinstance(found, BaseException):
            raise found
        # An unreadable list just means no marks.
        favorite_ids = set() if isinstance(hearted, BaseException) else {p['id'] for p in hearted}
        bought_ids = set() if isinstance(bought, BaseException) else {p['id'] for p in bought}
        for product in found['products']:
            product['favorite'] = product.get('id') in favorite_ids
            product['bought_before'] = product.get('id') in bought_ids
        return found


@tool()
@uz_only
async def search_addresses(query: str) -> dict:
    """Find an address by text ("Amir Temur 1"); returns coordinates usable as longitude, latitude in search and cart tools."""
    async with client.create_http() as http:
        return {"addresses": await client.Lavka(http).suggest(query)}
