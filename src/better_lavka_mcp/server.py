"""Unofficial Yandex Lavka Uzbekistan MCP server."""
import html
import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from http.cookiejar import Cookie
from pathlib import Path
from typing import TypedDict

import httpx
from mcp.server.fastmcp import FastMCP
from better_lavka_mcp.constants import BASE
from better_lavka_mcp.errors import UpstreamError

STATE = os.path.expanduser(os.environ.get("YANDEX_SESSION_FILE", "~/.local/share/yandex-lavka-mcp/session.json"))
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

def clean_text(value):
    """Strip Lavka's display markup: HTML entities, <notr> tags, soft hyphens."""
    text = html.unescape(re.sub(r"</?notr>", "", str(value or "")))
    return text.replace("\xad", "").replace("\xa0", " ")


def _number(value):
    """Lavka sends decimals as strings with a comma ("0,4"); return float or None."""
    try:
        return float(str(value).replace(",", ".").replace("\xa0", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _order_view(item, detailed=False):
    if not isinstance(item, dict) or not isinstance(item.get('orderId'), str) or not item['orderId']:
        raise UpstreamError('Unexpected order history entry')
    calc = item.get('calculation') if isinstance(item.get('calculation'), dict) else {}
    lines = []
    for line in calc.get('items') or []:
        if isinstance(line, dict) and line.get('productId'):
            lines.append({'product_id': line['productId'], 'name': clean_text(line.get('name')),
                          'quantity': line.get('count'), 'price_uzs': line.get('cost')})
    destinations = item.get('destinations') if isinstance(item.get('destinations'), list) else []
    view = {'order_id': item['orderId'], 'short_order_id': item.get('shortOrderId'),
            'created_at': item.get('createdAt'), 'closed_at': item.get('closedAt'),
            'status': item.get('status'), 'status_text': item.get('statusText'),
            'total_uzs': calc.get('finalCost') or item.get('cartTotalPrice'),
            'address': destinations[0].get('shortText') if destinations and isinstance(destinations[0], dict) else None,
            'items': lines}
    if detailed:
        view.update({'products_uzs': item.get('productsPrice'), 'delivery_uzs': calc.get('deliveryCost') or item.get('deliveryCost'),
                     'service_fee_uzs': calc.get('serviceFeeV2') or item.get('serviceFeeV2'), 'discount_uzs': calc.get('discount')})
    return view


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
        if path not in ("/api/v1/providers/search/v3/lavka", "/api/v1/providers/geo/v1/suggest", "/api/v1/providers/address/v1/get-favorite-addresses", "/api/v1/providers/order-log/v1/retrieve", "/api/v1/providers/v1/product"):
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
                products.append({"id": item.get("id"), "title": clean_text(item.get("title")), "price": item.get("currentPrice"), "currency": "UZS", "quantity": item.get("amount"), "available": item.get("available")})
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

    def _require_login(self, props):
        if props.get('pageData', {}).get('passportInfo', {}).get('status') != 'VALID':
            raise UpstreamError('An authenticated Uzbekistan session is required')

    async def _order_log(self, range_):
        self._require_login(self.props or await self.bootstrap())
        data = await self.call('/api/v1/providers/order-log/v1/retrieve', {'range': range_, 'includeServiceMetadata': False})
        if not isinstance(data, dict) or not isinstance(data.get('orders'), list):
            raise UpstreamError('Unexpected order history response')
        return data['orders']

    async def order_history(self, count=21, older_than=None):
        if type(count) is not int or not 1 <= count <= 21:
            raise ValueError('limit must be between 1 and 21')
        range_ = {'count': count}
        if older_than is not None:
            if not isinstance(older_than, str) or not older_than or len(older_than) > 128:
                raise ValueError('older_than must be the order_id of the last order on the previous page')
            range_['olderThan'] = older_than
        orders = [_order_view(item) for item in await self._order_log(range_)][:count]
        return {'orders': orders, 'next_older_than': orders[-1]['order_id'] if len(orders) == count else None}

    async def order(self, order_id):
        if not isinstance(order_id, str) or not order_id or len(order_id) > 128:
            raise ValueError('An order id is required')
        orders = [item for item in await self._order_log({'orderId': order_id}) if isinstance(item, dict) and item.get('orderId') == order_id]
        if not orders:
            raise ValueError('Order not found')
        return _order_view(orders[0], detailed=True)

    async def product(self, product_id, location):
        if not isinstance(product_id, str) or not product_id or len(product_id) > 256:
            raise ValueError('A product id is required')
        p = self.props or await self.bootstrap()
        info = p.get('pageData', {}).get('serviceInfo', {})
        body = {'depotType': 'regular', 'currencySign': info.get('currencySign'), 'position': {'location': list(location)},
                'productId': product_id, 'needCatalogPaths': True, 'isEcomboReward': False,
                'rewardPriceTemplate': '', 'enableUnavailable': True}
        raw = await self.call('/api/v1/providers/v1/product', body)
        item = raw.get('product') if isinstance(raw, dict) else None
        if not isinstance(item, dict) or not item.get('id'):
            raise UpstreamError('Unexpected product response')
        options = item.get('options') if isinstance(item.get('options'), dict) else {}
        ingredients = options.get('ingredients') if isinstance(options.get('ingredients'), dict) else {}
        names = {'calories': 'kcal', 'protein': 'protein', 'fat': 'fat', 'carbohydrate': 'carbs'}
        per_100g = {}
        for trait in ingredients.get('pfcTraits') or []:
            if isinstance(trait, dict) and trait.get('id') in names:
                per_100g[names[trait['id']]] = _number((trait.get('measures') or {}).get('per100g'))
        details = {clean_text(block.get('title')).strip(): clean_text(block.get('value')).strip()
                   for block in item.get('descriptionBlocks') or []
                   if isinstance(block, dict) and block.get('title') and block.get('value')}
        return {'id': item['id'], 'title': clean_text(item.get('title')), 'brand': clean_text(item.get('brand')) or None,
                'price_uzs': item.get('currentPrice'), 'pack': item.get('amount'), 'available': item.get('available'),
                'max_quantity': _number(item.get('quantityLimit')),
                'description': clean_text(item.get('description')) or None,
                'nutrition_per_100g': per_100g or None,
                'composition': clean_text(ingredients.get('description')) or None,
                'details': details}


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


# The delivery address chosen with use_address; lives for this server process only.
_delivery = None


def _location(longitude=None, latitude=None):
    """Explicit coordinates win; otherwise use the address chosen with use_address."""
    if longitude is None and latitude is None:
        if _delivery is None:
            raise ValueError('No delivery address yet: call use_address (for example use_address("home")) or pass longitude and latitude')
        return list(_delivery['location'])
    if (type(longitude) not in (int, float) or type(latitude) not in (int, float)
            or not (55 <= longitude <= 74 and 37 <= latitude <= 46)):
        raise ValueError('Pass both longitude and latitude inside Uzbekistan')
    return [longitude, latitude]


def _address_summary(entry, index):
    address = entry['address']
    return {'index': index, 'label': address.get('label') or None,
            'short_address': address.get('shortAddress') or address.get('fullAddress')}


@mcp.tool()
@uz_only
async def use_address(address: str) -> dict:
    """Choose the delivery address for this session by saved-address name ("Дом", "home"), part of the street, or index from list_saved_addresses. Search, cart and ordering then use it without coordinates."""
    global _delivery
    from better_lavka_mcp.orders.submit import addresses
    if not isinstance(address, str) or not address.strip():
        raise ValueError('Name the saved address, for example "home"')
    async with create_http() as http:
        _, entries = await addresses(http)
    query = address.strip().casefold()
    aliases = {'home': ('дом', 'uy'), 'дом': ('home', 'uy'), 'uy': ('home', 'дом'),
               'work': ('работа', 'ish'), 'работа': ('work', 'ish'), 'ish': ('work', 'работа')}
    wanted = (query,) + aliases.get(query, ())
    labels = [(entry['address'].get('label') or '').strip().casefold() for entry in entries]
    if query.isdigit() and int(query) < len(entries):
        matches = [int(query)]
    else:
        matches = [i for i, label in enumerate(labels) if label in wanted]
    if not matches:
        matches = [i for i, entry in enumerate(entries)
                   if any(query in str(entry['address'].get(k) or '').casefold() for k in ('label', 'shortAddress', 'fullAddress'))]
    if len(matches) != 1:
        return {'selected': False,
                'reason': 'ambiguous' if matches else 'not_found',
                'candidates': [_address_summary(entries[i], i) for i in (matches or range(len(entries)))],
                'note': 'Ask which one and call use_address again with its name or index.'}
    index = matches[0]
    location = entries[index]['address'].get('location')
    if not isinstance(location, list) or len(location) != 2:
        raise ValueError('This saved address has no map point; choose another one')
    _delivery = {'address_id': entries[index].get('addressId'), 'location': location, **_address_summary(entries[index], index)}
    return {'selected': True, **_address_summary(entries[index], index)}


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
    """Find products and prices. In Uzbekistan it searches at the address chosen with use_address (or at longitude, latitude); RU and KZ need coordinates and search anonymously. limit is 1–50."""
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
    location = _location(longitude, latitude)
    async with create_http() as http:
        return await Lavka(http).search(query, limit, tuple(location))

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

async def _cart_client(http, location):
    from better_lavka_mcp.cart import CartClient
    client = Lavka(http)
    props = await client.bootstrap()
    client._require_login(props)
    info = props.get('pageData', {}).get('serviceInfo', {})
    if not info:
        async with httpx.AsyncClient(timeout=25, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}) as anonymous:
            info = (await Lavka(anonymous).bootstrap()).get('pageData', {}).get('serviceInfo', {})
    if info.get('currency') != 'UZS' or not info.get('currencySign'):
        raise UpstreamError('Uzbekistan currency and sign not confirmed by upstream')
    return client, CartClient(http, props, location, info['currencySign'])


def _cart_view(cart):
    items = []
    for item in cart['items']:
        if not isinstance(item, dict):
            raise UpstreamError('Unexpected cart item')
        items.append({'id': item.get('id'), 'title': clean_text(item.get('title')), 'quantity': item.get('quantity'),
                      'price_uzs': item.get('price'), 'pack': item.get('amount'),
                      'available': item.get('isUnavailableOnDepot') is not True})
    conditions = cart.get('orderConditions') if isinstance(cart.get('orderConditions'), dict) else {}
    return {'items': items, 'currency': 'UZS',
            'total_uzs': cart.get('totalPriceValue'),
            'delivery_uzs': conditions.get('deliveryCost'),
            'eta': conditions.get('eta'),
            'available_for_checkout': cart.get('availableForCheckout'),
            'checkout_unavailable_reason': cart.get('checkoutUnavailableReason')}


def _quantity(value):
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return 0


async def _add_products(client, wanted, titles):
    """Add quantities on top of the cart, then drop what the store cannot sell and cap at its limits."""
    fresh = await client.set_items({pid: min(qty, 50) for pid, qty in wanted.items()}, add=True)
    by_id = {item.get('id'): item for item in fresh['items'] if isinstance(item, dict)}
    targets = {pid: _quantity(by_id.get(pid, {}).get('quantity')) for pid in wanted}
    fixes, unavailable, limited = {}, [], []
    for pid, target in targets.items():
        item = by_id.get(pid, {})
        title = clean_text(item.get('title')) or titles.get(pid) or pid
        limit = _number(item.get('quantityLimit'))
        if item.get('isUnavailableOnDepot') is True or limit == 0:
            fixes[pid] = 0
            unavailable.append(title)
        elif limit is not None and target > limit:
            fixes[pid] = int(limit)
            limited.append({'title': title, 'requested': target, 'max': int(limit)})
    if fixes:
        fresh = await client.set_items(fixes)
    added = [{'product_id': pid, 'title': clean_text(by_id.get(pid, {}).get('title')) or titles.get(pid),
              'quantity': fixes.get(pid, target)} for pid, target in targets.items() if fixes.get(pid, target)]
    return {'added': added, 'unavailable': unavailable, 'limited': limited, 'cart': _cart_view(fresh)}


@mcp.tool()
@uz_only
async def get_cart(longitude: float | None = None, latitude: float | None = None) -> dict:
    """What's in the cart: items, quantities, prices, delivery cost, ETA and total in UZS, for the address chosen with use_address (or at longitude, latitude)."""
    location = _location(longitude, latitude)
    async with create_http() as http:
        _, client = await _cart_client(http, location)
        return _cart_view(await client.retrieve())


@mcp.tool()
@uz_only
async def set_cart_quantity(product_id: str, quantity: int, longitude: float | None = None, latitude: float | None = None) -> dict:
    """Change the cart: set product_id to an exact quantity (0 removes it). Lavka applies its current price. Returns the updated cart."""
    location = _location(longitude, latitude)
    async with create_http() as http:
        _, client = await _cart_client(http, location)
        return _cart_view(await client.set_items({product_id: quantity}))


class CartRequest(TypedDict, total=False):
    query: str
    product_id: str
    quantity: int


@mcp.tool()
@uz_only
async def add_to_cart(items: list[CartRequest], longitude: float | None = None, latitude: float | None = None) -> dict:
    """Add several products in one go. Each item is {"query": "молоко 2.5%"} or {"product_id": "..."} with an optional "quantity" (default 1); a query adds the first available search result. Reports what was added, not found, unavailable or capped at the store limit."""
    if not isinstance(items, list) or not items or len(items) > 50:
        raise ValueError('Pass 1–50 items')
    location = _location(longitude, latitude)
    wanted, titles, not_found = {}, {}, []
    async with create_http() as http:
        lavka, client = await _cart_client(http, location)
        for request in items:
            request = dict(request)
            quantity = request.get('quantity', 1)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 50:
                raise ValueError('Each quantity must be between 1 and 50')
            product_id = request.get('product_id')
            if not product_id:
                query = request.get('query')
                if not isinstance(query, str) or not query.strip():
                    raise ValueError('Each item needs a query or a product_id')
                found = await lavka.search(query, 5, tuple(location))
                match = next((p for p in found['products'] if p.get('available') and p.get('id')), None)
                if match is None:
                    not_found.append(query)
                    continue
                product_id, titles[match['id']] = match['id'], match['title']
            wanted[product_id] = wanted.get(product_id, 0) + quantity
        if not wanted:
            return {'added': [], 'not_found': not_found, 'unavailable': [], 'limited': [], 'cart': _cart_view(await client.retrieve())}
        result = await _add_products(client, wanted, titles)
    return {**result, 'not_found': not_found}


@mcp.tool()
@uz_only
async def repeat_order(order_id: str = 'last', longitude: float | None = None, latitude: float | None = None) -> dict:
    """Put everything from a past order back into the cart (order_id from list_order_history, or "last"). Lavka applies today's prices; items the store no longer has are left out and listed."""
    location = _location(longitude, latitude)
    async with create_http() as http:
        lavka, client = await _cart_client(http, location)
        if order_id == 'last':
            history = await lavka.order_history(1)
            if not history['orders']:
                raise ValueError('There are no past orders yet')
            order_id = history['orders'][0]['order_id']
        order = await lavka.order(order_id)
        wanted, titles = {}, {}
        for line in order['items']:
            wanted[line['product_id']] = wanted.get(line['product_id'], 0) + max(_quantity(line.get('quantity')), 1)
            titles[line['product_id']] = line['name']
        if not wanted:
            raise ValueError('That order has no products to repeat')
        result = await _add_products(client, wanted, titles)
    return {'order': {k: order[k] for k in ('order_id', 'created_at', 'total_uzs')}, **result}


@mcp.tool()
@uz_only
async def get_product(product_id: str, longitude: float | None = None, latitude: float | None = None) -> dict:
    """Product card: price, pack size, availability, description, calories and protein/fat/carbs per 100 g, shelf life, brand and country."""
    location = _location(longitude, latitude)
    async with create_http() as http:
        return await Lavka(http).product(product_id, location)


@mcp.tool()
@uz_only
async def list_saved_cards(longitude: float | None = None, latitude: float | None = None) -> dict:
    """Your saved cards as "**** 1234", for the address chosen with use_address (or at longitude, latitude)."""
    from better_lavka_mcp.payment import PaymentClient
    location = _location(longitude, latitude)
    async with create_http() as http:
        props = await Lavka(http).bootstrap()
        cards = await PaymentClient(http, props, location).lookup()
        return {'cards': cards.masked_cards}


@mcp.tool()
@uz_only
async def list_order_history(limit: int = 21, older_than: str | None = None) -> dict:
    """Past orders, newest first, with their items, total and address. For the next page pass next_older_than as older_than. limit is 1–21."""
    async with create_http() as http:
        return await Lavka(http).order_history(limit, older_than)


@mcp.tool()
@uz_only
async def get_order(order_id: str) -> dict:
    """One past order in full: items, products, delivery, service fee, discount, total, address and status."""
    async with create_http() as http:
        return await Lavka(http).order(order_id)


@mcp.tool()
@uz_only
async def get_payment_status(order_id: str) -> dict:
    """Payment status of an order (success, wait_user_action, failed…). When the bank asks for 3-D Secure it also returns redirect_url to open."""
    from better_lavka_mcp.payment import payment_status
    async with create_http() as http:
        props = await Lavka(http).bootstrap()
        return await payment_status(http, props, order_id)


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
    """Your saved delivery addresses with their index and name. Pick one for the session with use_address."""
    from better_lavka_mcp.orders.submit import addresses
    async with create_http() as http:
        _, entries = await addresses(http)
        return {'addresses': [{'index': i, 'address': entry['address']} for i, entry in enumerate(entries)]}


@mcp.tool()
@uz_only
async def preview_order(saved_address_index: int | None = None, card_last4: str | None = None) -> dict:
    """Show the order before paying: address, items, total UZS and card, plus a five-minute token for submit_order. Defaults to the address chosen with use_address and to your only saved card. Charges nothing."""
    from better_lavka_mcp.orders.submit import addresses, preview
    from better_lavka_mcp.payment import PaymentClient
    async with create_http() as http:
        if saved_address_index is None or card_last4 is None:
            _, entries = await addresses(http)
            if saved_address_index is None:
                if _delivery is None:
                    raise ValueError('Choose the delivery address first with use_address, or pass saved_address_index')
                ids = [i for i, entry in enumerate(entries) if entry.get('addressId') == _delivery['address_id']]
                if len(ids) != 1:
                    raise ValueError('The address chosen with use_address is gone; call use_address again')
                saved_address_index = ids[0]
            if card_last4 is None:
                location = entries[saved_address_index]['address'].get('location') if 0 <= saved_address_index < len(entries) else None
                props = await Lavka(http).bootstrap()
                cards = (await PaymentClient(http, props, location).lookup()).masked_cards
                if len(cards) != 1:
                    return {'preview': False, 'cards': cards, 'note': 'Ask which card to use and pass card_last4.'}
                card_last4 = cards[0][-4:]
        return await preview(http, saved_address_index, card_last4)


@mcp.tool()
@uz_only
async def submit_order(order_preview_token: str, confirmed_total_uzs: str, explicit_confirmation: bool) -> dict:
    """Charges money: place the order from preview_order's token. Pass the exact total from the preview as confirmed_total_uzs and explicit_confirmation=true once the user agreed. Sent once; then waits briefly for the payment status and returns redirect_url if the bank asks for 3-D Secure."""
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
