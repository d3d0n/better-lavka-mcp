"""Unofficial, read-only Yandex Lavka Uzbekistan MCP server."""
import html
import json
import logging
import os
import re
from http.cookiejar import Cookie
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from better_lavka_mcp.constants import BASE
from better_lavka_mcp.errors import UpstreamError

STATE = os.path.expanduser(os.environ.get("YANDEX_SESSION_FILE", "~/.local/share/yandex-lavka-mcp/session.json"))
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

class Lavka:
    def __init__(self, http, region_http=None):
        self.http = http
        self.region_http = region_http
        self.props = None

    async def bootstrap(self):
        # The frontend receives a .yandex.uz identity cookie before API calls.
        # Without it the homepage's BFF CSRF token fails validation.
        if not any(c.name == "yandexuid" and c.domain in (".yandex.uz", "yandex.uz") for c in self.http.cookies.jar):
            identity = await self.http.get("https://yandex.uz/favicon.ico", follow_redirects=True)
            if identity.status_code != 200 or not any(c.name == "yandexuid" and c.domain in (".yandex.uz", "yandex.uz") for c in self.http.cookies.jar):
                raise UpstreamError("Uzbekistan identity bootstrap failed")
        r = await self.http.get(BASE + "/", params={"_lavka_mcp": str(__import__('time').time_ns())})
        if r.status_code != 200:
            raise UpstreamError(f"Homepage HTTP {r.status_code}")
        m = re.search(r'<script id="__page_props__-data"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            raise UpstreamError("Homepage bootstrap data missing")
        self.props = json.loads(m.group(1))
        return self.props

    async def call(self, path, body):
        if path not in ("/api/v1/providers/search/v3/lavka", "/api/v1/providers/geo/v1/suggest", "/api/v1/providers/address/v1/get-favorite-addresses", "/api/v1/providers/order-log/v1/retrieve"):
            raise ValueError("Endpoint is not allowlisted")
        p = self.props or await self.bootstrap()
        locale = p.get("pageEnv", {}).get("locale", "ru-RU")
        if isinstance(locale, dict):
            locale = f"{locale.get('lang', 'ru')}-{locale.get('region', 'RU')}"
        headers = {"Origin": BASE, "Referer": BASE + "/", "X-Requested-With": "XMLHttpRequest", "X-Csrf-Token": p.get("authProxyCsrfToken") or "", "X-Csrf-Token-Bff": p.get("csrfToken") or "", "X-Lavka-Web-Locale": locale, "X-Lavka-Web-City": str(p.get("pageEnv", {}).get("cityId", ""))}
        r = await self.http.post(BASE + path, headers=headers, json=body)
        if r.status_code in (401, 403):
            raise UpstreamError(f"Upstream HTTP {r.status_code}: authorization or CSRF rejected; session/API bootstrap is not verified")
        if r.status_code != 200:
            raise UpstreamError(f"Upstream HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise UpstreamError("Upstream returned non-JSON") from None
        if isinstance(data, dict) and data.get("type") == "captcha":
            raise UpstreamError("Upstream requires CAPTCHA")
        return data

    async def search(self, query, limit, location):
        if not query.strip() or not 1 <= limit <= 50:
            raise ValueError("Query required; limit must be 1–50")
        if location is None:
            raise ValueError("Both longitude and latitude are required for search")
        p = self.props or await self.bootstrap()
        info = p.get("pageData", {}).get("serviceInfo", {})
        region_source = "session_homepage"
        if not info:
            if self.region_http is not None:
                anon = await Lavka(self.region_http).bootstrap()
            else:
                async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}) as anonymous_http:
                    anon = await Lavka(anonymous_http).bootstrap()
            info = anon.get("pageData", {}).get("serviceInfo", {})
            region_source = "independent_anonymous_uz_homepage"
        if info.get("currency") != "UZS":
            raise UpstreamError("Uzbekistan currency not confirmed by upstream")
        body = {"depotType": "regular", "currencySign": info.get("currencySign"), "text": query, "productsLimit": limit, "subcategoriesLimit": 0, "useRetail": True, "source": "manual_input"}
        if location is not None:
            lon, lat = location
            if not (55 <= lon <= 74 and 37 <= lat <= 46):
                raise ValueError("Coordinates must be within Uzbekistan bounding box")
            body["position"] = {"location": [lon, lat]}
        raw = await self.call("/api/v1/providers/search/v3/lavka", body)
        if not isinstance(raw, dict) or not isinstance(raw.get("cacheProducts"), list):
            raise UpstreamError("Unexpected search response; results unavailable")
        products = []
        for item in raw["cacheProducts"][:limit]:
            if isinstance(item, dict):
                products.append({"id": item.get("id"), "title": html.unescape(re.sub(r"</?notr>", "", str(item.get("title") or ""))), "price": item.get("currentPrice"), "currency": "UZS", "quantity": item.get("amount"), "available": item.get("available")})
        return {"products": products, "location_supplied": location is not None, "currency_source": region_source, "note": "Availability and price are location dependent; no delivery coverage is implied."}

    async def suggest(self, query):
        if not query.strip():
            raise ValueError("Address query required")
        return await self.call("/api/v1/providers/geo/v1/suggest", {"query": query, "action": "user_input", "lang": "ru"})

    async def saved_address_count(self):
        p = self.props or await self.bootstrap()
        if p.get("pageData", {}).get("passportInfo", {}).get("status") != "VALID":
            raise UpstreamError("An authenticated Uzbekistan session is required")
        result = await self.call("/api/v1/providers/address/v1/get-favorite-addresses", {})
        if not isinstance(result, list):
            raise UpstreamError("Unexpected saved addresses response")
        return {"count": len(result)}

    async def order_history(self, count=21, older_than=None):
        from datetime import datetime
        if type(count) is not int or not 1 <= count <= 21:
            raise ValueError('count must be between 1 and 21')
        if older_than is not None:
            try:
                if not isinstance(older_than, str) or len(older_than) > 64:
                    raise ValueError()
                dt = datetime.fromisoformat(older_than.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise ValueError('older_than must be a timezone-aware ISO timestamp') from None
        p = self.props or await self.bootstrap()
        if p.get('pageData', {}).get('passportInfo', {}).get('status') != 'VALID':
            raise UpstreamError('An authenticated Uzbekistan session is required')
        uids = [c.value for c in self.http.cookies.jar if c.name == 'yandexuid' and c.domain in ('.yandex.uz', 'yandex.uz') and c.path == '/' and not c.is_expired()]
        if len(uids) != 1 or not re.fullmatch(r'[0-9]{1,64}', uids[0]):
            raise UpstreamError('Uzbekistan identity cookie missing or ambiguous')
        range_ = {'count': count}
        if older_than is not None:
            range_['olderThan'] = older_than
        data = await self.call('/api/v1/providers/order-log/v1/retrieve', {'range': range_, 'userIdentity': {'yandexUid': uids[0], 'boundYandexUids': []}, 'includeServiceMetadata': False})
        if not isinstance(data, dict) or not isinstance(data.get('orders'), list) or not isinstance(data.get('tabs'), dict) or len(data['orders']) > count:
            raise UpstreamError('Unexpected order history response')
        orders = []
        fields = {'order_id':'orderId', 'short_order_id':'shortOrderId', 'created_at':'createdAt', 'status':'status', 'status_text':'statusText', 'closed_at':'closedAt'}
        for item in data['orders']:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ('orderId', 'createdAt', 'status')):
                raise UpstreamError('Unexpected order history entry')
            view = {k: item.get(v) for k, v in fields.items()}
            if any(v is not None and (not isinstance(v, str) or len(v) > 256 or any(ord(c) < 32 for c in v)) for v in view.values()):
                raise UpstreamError('Unexpected order history field')
            orders.append(view)
        return {'orders': orders, 'next_older_than': orders[-1]['created_at'] if len(orders) == count else None, 'note': 'Point-in-time history; pagination uses the last creation timestamp and may not capture tied timestamps.'}


def create_http():
    client = httpx.AsyncClient(timeout=25, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    # Preserve original domain scope; never copy .yandex.ru credentials to .uz.
    if Path(STATE).is_file():
        data = json.loads(Path(STATE).read_text())
        for item in data.get("cookies", []):
            try:
                params = dict(item)
                params["rest"] = params.pop("_rest", {})
                client.cookies.jar.set_cookie(Cookie(**params))
            except (TypeError, KeyError, ValueError):
                raise UpstreamError("Invalid saved cookie jar") from None
    return client

# One stdio server process serves one client session. Never persist this selection.
# All state transitions and the guard admission happen synchronously on the event
# loop, before any await; active operations pin the region until they finish.
from functools import wraps

import os
_selected_region = os.environ.get('YANDEX_LAVKA_REGION', 'UZ')
if _selected_region not in ('UZ', 'RU', 'KZ'):
    raise ValueError('YANDEX_LAVKA_REGION must be UZ, RU or KZ')
_active_uz_operations = 0


def uz_only(fn):
    @wraps(fn)
    async def guarded(*args, **kwargs):
        global _active_uz_operations
        if _selected_region != 'UZ':
            raise ValueError('UZ-only tool: select UZ before using account, cart, login or location tools')
        _active_uz_operations += 1
        try:
            return await fn(*args, **kwargs)
        finally:
            _active_uz_operations -= 1
    return guarded

mcp = FastMCP("Yandex Lavka Uzbekistan")

@mcp.tool()
async def select_country(country: str) -> dict:
    """Set the default country for this server session to UZ, RU or KZ (in memory only; not saved). UZ enables account, cart and checkout tools; RU and KZ allow only search_products and get_storefront_status. Places no order."""
    global _selected_region
    if country not in ('UZ', 'RU', 'KZ'):
        raise ValueError('Unsupported country: supported countries are RU, KZ, UZ')
    if _active_uz_operations and country != _selected_region:
        raise ValueError('Cannot change region while a UZ operation is in progress')
    _selected_region = country
    uz = country == 'UZ'
    return {'country': country, 'supported': True, 'persistent': False, 'session_scoped': True,
            'read_only_tools': ['get_storefront_status', 'search_products'],
            'uz_account_and_checkout_tools_available': uz,
            'note': ('Selection lasts for this stdio session; status/search use it unless overridden. '
                     + ('UZ account, cart and checkout tools are available but not guaranteed to succeed.' if uz else
                        'RU/KZ reads are anonymous; account, cart and checkout tools are UZ-only. Do not use them for this region.'))}


@mcp.tool()
@uz_only
async def check_coordinates(country: str, longitude: float, latitude: float) -> dict:
    """Read-only: validate longitude, latitude against a coarse Uzbekistan bounding box and return them for later search and cart calls. Does not select an address, verify delivery coverage or persist anything."""
    if country != 'UZ':
        raise ValueError('Unsupported country: check_coordinates is UZ-only')
    if (type(longitude) not in (int, float) or type(latitude) not in (int, float)
            or not (55 <= longitude <= 74 and 37 <= latitude <= 46)):
        raise ValueError('Coordinates must be within the Uzbekistan bounding box')
    return {'country': 'UZ', 'longitude': longitude, 'latitude': latitude,
            'source': 'coordinates', 'coverage_verified': False, 'address_selected': False,
            'note': 'Pass these coordinates to each location-dependent tool. Refresh search and cart after a location change; coverage and exact delivery address are not established.'}


@mcp.tool()
@uz_only
async def get_saved_address_coordinates(country: str, saved_address_index: int | None = None) -> dict:
    """Read-only: return the coordinates of one saved address; saved_address_index is required when more than one exists, otherwise the result says ambiguous or none. Coordinates are private and this does not select the address for checkout."""
    if country != 'UZ':
        raise ValueError('Unsupported country: saved-address detection is UZ-only')
    from better_lavka_mcp.orders.submit import addresses
    async with create_http() as http:
        _, entries = await addresses(http)
    if saved_address_index is None:
        if len(entries) != 1:
            return {'country': 'UZ', 'source': 'ambiguous' if entries else 'none',
                    'count': len(entries), 'coverage_verified': False, 'address_selected': False,
                    'note': 'No location inferred. Use list_saved_addresses and explicitly select an index, or supply coordinates.'}
        index, source = 0, 'single_saved_address'
    else:
        if type(saved_address_index) is not int or not 0 <= saved_address_index < len(entries):
            raise ValueError('Select a valid saved address index')
        index, source = saved_address_index, 'saved_address_index'
    loc = entries[index]['address'].get('location')
    if not isinstance(loc, list) or len(loc) != 2:
        raise ValueError('Saved address has no usable location')
    result = await check_coordinates(country, loc[0], loc[1])
    result['source'] = source
    result['note'] += ' Favorite coordinates are a candidate, not evidence of the selected checkout address. Indexes can change; recheck the exact saved address.'
    return result


@mcp.tool()
async def get_storefront_status(country: str | None = None) -> dict:
    """Read-only: return homepage storefront info (currency, open/disabled, delivery time; UZ also reports login status) for the selected country or an explicit country. Not a quote and not proof of delivery coverage at an address."""
    country = _selected_region if country is None else country
    if country not in ('UZ', 'RU', 'KZ'):
        raise ValueError('Supported countries: RU, KZ, UZ')
    if country != 'UZ':
        from better_lavka_mcp.region import RegionalStorefront
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}) as http:
            return await RegionalStorefront(country, http).status()
    async with create_http() as http:
        p = await Lavka(http).bootstrap()
        data = p.get("pageData", {})
        service = data.get("serviceInfo", {})
        return {"currency": service.get("currency"), "currency_sign": service.get("currencySign"), "advertised_delivery_time": service.get("deliveryTimeText"), "is_open": service.get("isOpen"), "is_disabled": service.get("isDisabled"), "passport_status": data.get("passportInfo", {}).get("status"), "address_coverage_verified": False, "note": "Homepage default storefront only; not a quote for your address."}

@mcp.tool()
async def search_products(query: str, limit: int = 20, longitude: float | None = None, latitude: float | None = None, country: str | None = None) -> dict:
    """Read-only: search products at longitude, latitude (UZ session) or an isolated anonymous RU/KZ storefront; limit is 1–50. Prices and availability are location-dependent and do not establish delivery coverage."""
    if (longitude is None) != (latitude is None):
        raise ValueError("Both longitude and latitude are required")
    country = _selected_region if country is None else country
    if country not in ('UZ', 'RU', 'KZ'):
        raise ValueError('Supported countries: RU, KZ, UZ')
    if country != 'UZ':
        if longitude is None:
            raise ValueError('Both longitude and latitude are required')
        from better_lavka_mcp.region import RegionalStorefront
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}) as http:
            return await RegionalStorefront(country, http).search(query, limit, longitude, latitude)
    async with create_http() as http:
        return await Lavka(http).search(query, limit, None if longitude is None else (longitude, latitude))

@mcp.tool()
@uz_only
async def search_addresses(query: str) -> dict:
    """Read-only: return address suggestions for a free-text query. Suggestions do not change the saved delivery address or confirm that delivery is available."""
    async with create_http() as http:
        result = await Lavka(http).suggest(query)
        if not isinstance(result, (dict, list)):
            raise UpstreamError("Unexpected address response")
        return {"suggestions": result, "coverage_verified": False}

@mcp.tool()
@uz_only
async def count_saved_addresses() -> dict:
    """Read-only: return the number of saved favorite addresses without their details. Requires an authenticated Uzbekistan session."""
    async with create_http() as http:
        return await Lavka(http).saved_address_count()

async def _cart_client(http, longitude, latitude):
    from better_lavka_mcp.cart import CartClient
    client = Lavka(http)
    props = await client.bootstrap()
    if props.get('pageData', {}).get('passportInfo', {}).get('status') != 'VALID':
        raise UpstreamError('An authenticated Uzbekistan session is required')
    info = props.get('pageData', {}).get('serviceInfo', {})
    if not info:
        async with httpx.AsyncClient(timeout=25, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}) as anonymous:
            info = (await Lavka(anonymous).bootstrap()).get('pageData', {}).get('serviceInfo', {})
    if info.get('currency') != 'UZS' or not info.get('currencySign'):
        raise UpstreamError('Uzbekistan currency and sign not confirmed by upstream')
    return CartClient(http, props, [longitude, latitude], info['currencySign'])


def _cart_view(cart):
    items = []
    for item in cart['items']:
        if not isinstance(item, dict):
            raise UpstreamError('Unexpected cart item')
        items.append({k: item.get(k) for k in ('id', 'title', 'quantity', 'price')})
    return {'items': items, 'currency': 'UZS',
            'total_uzs': cart.get('totalPriceValue'),
            'available_for_checkout': cart.get('availableForCheckout'),
            'checkout_unavailable_reason': cart.get('checkoutUnavailableReason'),
            'note': 'Point-in-time cart at supplied coordinates; address and payment selection are not established.'}


@mcp.tool()
@uz_only
async def get_cart(longitude: float, latitude: float) -> dict:
    """Read-only: return the cart at longitude, latitude (items, quantities, prices, total UZS, checkout availability). Does not select a delivery address or payment card."""
    async with create_http() as http:
        client = await _cart_client(http, longitude, latitude)
        return _cart_view(await client.retrieve())


@mcp.tool()
@uz_only
async def set_cart_quantity(longitude: float, latitude: float, product_id: str, quantity: int, price_uzs: str) -> dict:
    """Modifies the real cart: set product_id to an absolute integer quantity (0 removes it) at longitude, latitude, passing price_uzs from the current search. Sent once with a cart readback check; never retried automatically."""
    async with create_http() as http:
        client = await _cart_client(http, longitude, latitude)
        return _cart_view(await client.set_item(product_id, quantity, price_uzs))


@mcp.tool()
@uz_only
async def list_saved_cards(longitude: float, latitude: float) -> dict:
    """Read-only: list saved cards as masked suffixes such as "**** 1234" at longitude, latitude (the payment API requires coordinates). Does not select a card or charge anything."""
    from better_lavka_mcp.payment import PaymentClient
    async with create_http() as http:
        props = await Lavka(http).bootstrap()
        cards = await PaymentClient(http, props, [longitude, latitude]).lookup()
        return {'cards': cards.masked_cards, 'payment_selected': False}


@mcp.tool()
@uz_only
async def list_order_history(limit: int = 21, older_than: str | None = None) -> dict:
    """Read-only: list up to 21 past orders (ID, timestamps, status), optionally before a timezone-aware older_than timestamp. Pagination may miss orders that share the last timestamp."""
    async with create_http() as http:
        return await Lavka(http).order_history(limit, older_than)


@mcp.tool()
@uz_only
async def list_active_orders() -> dict:
    """Read-only: return the active-order tracking feed. An empty feed does not prove there are no orders and cannot reconcile an uncertain submission."""
    async with create_http() as http:
        props = await Lavka(http).bootstrap()
        if props.get('pageData', {}).get('passportInfo', {}).get('status') != 'VALID':
            raise UpstreamError('An authenticated Uzbekistan session is required')
        headers = {'Origin': BASE, 'Referer': BASE + '/', 'X-Requested-With': 'XMLHttpRequest',
                   'X-Csrf-Token': props.get('authProxyCsrfToken') or '',
                   'X-Csrf-Token-Bff': props.get('csrfToken') or ''}
        try:
            response = await http.get(BASE + '/api/v1/providers/orders-tracking/v1/tracked-orders', headers=headers)
            if response.status_code != 200:
                raise UpstreamError(f'Active-order endpoint HTTP {response.status_code}')
            data = response.json()
        except UpstreamError:
            raise
        except Exception:
            raise UpstreamError('Active-order read failed or was not JSON') from None
        from better_lavka_mcp.orders.tracking import project_tracking_response, TrackingShapeError
        try:
            return project_tracking_response(data)
        except TrackingShapeError:
            raise UpstreamError('Unverified active-order response; no conclusion possible') from None


@mcp.tool()
@uz_only
async def list_saved_addresses() -> dict:
    """Read-only: list saved addresses with the index needed by get_saved_address_coordinates and preview_order. Contains private address details."""
    from better_lavka_mcp.orders.submit import addresses
    async with create_http() as http:
        _, entries = await addresses(http)
        return {'addresses': [{'index': i, 'address': entry['address']} for i, entry in enumerate(entries)]}


@mcp.tool()
@uz_only
async def preview_order(saved_address_index: int, card_last4: str) -> dict:
    """Creates a single-use five-minute local preview token for saved_address_index and card_last4, returning address, items, total UZS and masked card. Places no order and charges nothing, but records a local preview row; the cart must be available and non-rover."""
    from better_lavka_mcp.orders.submit import preview
    async with create_http() as http:
        return await preview(http, saved_address_index, card_last4)


@mcp.tool()
@uz_only
async def submit_order(order_preview_token: str, confirmed_total_uzs: str, explicit_confirmation: bool) -> dict:
    """Charges money: place one order from an unexpired order_preview_token after explicit_confirmation=true and confirmed_total_uzs exactly matching the preview total. Sent at most once; the result can be submitted, rejected or unknown, and payment is never verified."""
    from better_lavka_mcp.orders.submit import submit
    async with create_http() as http:
        return await submit(http, order_preview_token, confirmed_total_uzs, explicit_confirmation)


@mcp.tool()
@uz_only
async def reconcile_order_attempt(checked_order_history: bool, checked_payment: bool, accepts_duplicate_risk: bool, no_request_in_progress: bool, explicit_confirmation: bool) -> dict:
    """Local state only: record that you checked official order history and payment, that no request is in progress, and that you accept duplicate-order risk, then release the unknown checkout block. Verifies nothing, places no order, and cannot rule out delayed processing."""
    from better_lavka_mcp.orders.submit import reconcile_checkout as reconcile
    return reconcile(history_checked=checked_order_history, payment_checked=checked_payment,
                     acknowledge_uncertainty=accepts_duplicate_risk,
                     no_request_in_progress=no_request_in_progress, explicit_confirmation=explicit_confirmation)


@mcp.tool()
@uz_only
async def cancel_order(order_id: str, explicit_confirmation: bool) -> dict:
    """Cancels an order: send at most one cancellation request for the exact order_id after explicit_confirmation=true. A response does not prove cancellation, refund status is unknown, and repeat cancellation attempts are blocked."""
    from better_lavka_mcp.orders.submit import cancel
    async with create_http() as http:
        async def history():
            return await Lavka(http).order_history()
        return await cancel(http, order_id, explicit_confirmation, history)


from better_lavka_mcp.auth.tools import register_qr_tools
register_qr_tools(mcp, STATE, uz_only)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
