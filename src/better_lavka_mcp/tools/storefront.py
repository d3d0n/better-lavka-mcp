"""Storefront status and product search for every supported region."""
import asyncio

import httpx

from better_lavka_mcp import client
from better_lavka_mcp.app import _current_delivery, _location, selected_region, tool, uz_only


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
    """Start here. Read-only: whether Lavka is open and its delivery time; in Uzbekistan also whether you are signed in, the chosen delivery address, what is in the cart and the latest order with its status. Parts Lavka didn't answer are marked unknown. Not a quote and not proof of delivery coverage at an address."""
    country = selected_region() if country is None else country
    if country not in ('UZ', 'RU', 'KZ'):
        raise ValueError('Supported countries: RU, KZ, UZ')
    if country != 'UZ':
        from better_lavka_mcp.region import RegionalStorefront
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}) as http:
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
async def search_products(query: str, limit: int = 20, longitude: float | None = None, latitude: float | None = None, country: str | None = None) -> dict:
    """Find products and prices. In Uzbekistan it searches at the address chosen with use_address (or at longitude, latitude); RU and KZ need coordinates and search anonymously. limit is 1–50."""
    if (longitude is None) != (latitude is None):
        raise ValueError("Both longitude and latitude are required")
    country = selected_region() if country is None else country
    if country not in ('UZ', 'RU', 'KZ'):
        raise ValueError('Supported countries: RU, KZ, UZ')
    if country != 'UZ':
        if longitude is None:
            raise ValueError('Both longitude and latitude are required')
        from better_lavka_mcp.region import RegionalStorefront
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}) as http:
            return await RegionalStorefront(country, http).search(query, limit, longitude, latitude)
    async with client.create_http() as http:
        location = _location(http, longitude, latitude)
        return await client.Lavka(http).search(query, limit, tuple(location))


@tool()
@uz_only
async def search_addresses(query: str) -> dict:
    """Find any address by text, e.g. "Amir Temur 1": street, area and coordinates. Pass the coordinates as longitude, latitude to search or cart tools to shop there."""
    async with client.create_http() as http:
        return {"addresses": await client.Lavka(http).suggest(query)}
