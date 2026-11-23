"""Unofficial Yandex Lavka Uzbekistan MCP server."""
import html
import inspect
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
        if path not in ("/api/v1/providers/search/v3/lavka", "/api/v1/providers/address/v1/suggest-search", "/api/v1/providers/address/v1/get-favorite-addresses", "/api/v1/providers/order-log/v1/retrieve", "/api/v1/providers/v1/product", "/api/v1/providers/v1/layout", "/api/v1/providers/v2/category"):
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
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Address query required")
        raw = await self.call("/api/v1/providers/address/v1/suggest-search", {"input": query})
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise UpstreamError("Unexpected address response")
        found = []
        for item in raw["items"]:
            position = item.get("position") if isinstance(item, dict) else None
            if isinstance(position, list) and len(position) == 2:
                found.append({"title": clean_text(item.get("title")).strip(), "area": clean_text(item.get("label")).strip(),
                              "longitude": position[0], "latitude": position[1]})
        return found

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
        body = {**await self._storefront_body(location),
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

    async def _storefront_body(self, location):
        p = self.props or await self.bootstrap()
        info = p.get('pageData', {}).get('serviceInfo', {})
        return {'depotType': 'regular', 'currencySign': info.get('currencySign'), 'position': {'location': list(location)}}

    async def categories(self, location):
        raw = await self.call('/api/v1/providers/v1/layout', {**await self._storefront_body(location), 'layoutSlug': 'grocery'})
        if not isinstance(raw, dict) or not isinstance(raw.get('sections'), list):
            raise UpstreamError('Unexpected catalog response')
        groups = []
        for section in raw['sections']:
            group = ((section.get('categoryGroup') or {}).get('categoryGroupInfo') or {}) if isinstance(section, dict) else {}
            categories = [{'category_id': info['id'], 'title': clean_text(info.get('title'))}
                          for info in ((c.get('categoryInfo') or {}) for c in section.get('categories') or [] if isinstance(c, dict))
                          if info.get('id') and info.get('available') is not False]
            if group.get('id') and categories:
                groups.append({'group_id': group['id'], 'title': clean_text(group.get('title')), 'categories': categories})
        return groups

    async def category(self, category_id, location, subcategory=None, limit=30):
        if not isinstance(category_id, str) or not category_id or len(category_id) > 128:
            raise ValueError('A category id from list_categories is required')
        group_id = next((g['group_id'] for g in await self.categories(location)
                         if any(c['category_id'] == category_id for c in g['categories'])), None)
        if group_id is None:
            raise ValueError('Unknown or unavailable category; pick one from list_categories')
        raw = await self.call('/api/v1/providers/v2/category', {**await self._storefront_body(location), 'modes': ['grocery'],
                                                                'categoryId': category_id,
                                                                'categorySlugPath': {'layoutSlug': 'grocery', 'groupId': group_id}})
        if not isinstance(raw, dict) or not isinstance(raw.get('categories'), list) or not isinstance(raw.get('products'), list):
            raise UpstreamError('Unexpected category response')
        cards = {p['id']: p for p in raw['products'] if isinstance(p, dict) and p.get('id')}
        shelves = []
        for node in raw['categories']:
            for shelf in (node.get('items') or []) if isinstance(node, dict) else []:
                value = shelf.get('value') if isinstance(shelf, dict) else None
                if isinstance(value, dict) and value.get('type') == 'subcategory':
                    ids = [g.get('id') for g in shelf.get('items') or [] if isinstance(g, dict) and g.get('id') in cards]
                    shelves.append((clean_text(value.get('title')), ids))
        wanted = subcategory.strip().casefold() if isinstance(subcategory, str) and subcategory.strip() else None
        products = []
        for title, ids in shelves:
            if wanted and wanted not in title.casefold():
                continue
            for pid in ids:
                card = cards[pid]
                products.append({'id': pid, 'title': clean_text(card.get('title')), 'price_uzs': card.get('currentPrice'),
                                 'pack': card.get('amount'), 'available': card.get('available'), 'subcategory': title})
        return {'subcategories': [{'title': title, 'count': len(ids)} for title, ids in shelves],
                'products': products[:limit], 'total': len(products)}


def _jar_values(jar):
    return {(c.domain, c.path, c.name): c.value for c in jar}


class _SessionClient(httpx.AsyncClient):
    """Saves cookies Lavka rotated during the calls back to the session file on close."""

    _loaded_from = None

    async def __aexit__(self, *exc):
        try:
            self._save_rotated_cookies()
        except Exception:
            pass  # Saving is best effort; the call's own result matters more.
        return await super().__aexit__(*exc)

    def _save_rotated_cookies(self):
        if self._loaded_from is None:
            return
        raw, values = self._loaded_from
        current = _jar_values(self.cookies.jar)
        if current == values:
            return
        # Never save a jar that lost the login cookie: keep the old session instead.
        if not any(name == 'Session_id' for _, _, name in current):
            return
        path = Path(STATE)
        # Another process (a new QR login) replaced the file: keep its session.
        if path.read_bytes() != raw:
            return
        from better_lavka_mcp.auth.qr import save_session
        save_session(self, path)


def create_http():
    client = _SessionClient(timeout=25, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    # Preserve original domain scope; never copy .yandex.ru credentials to .uz.
    if Path(STATE).is_file():
        raw = Path(STATE).read_bytes()
        data = json.loads(raw)
        for item in data.get("cookies", []):
            try:
                params = dict(item)
                params["rest"] = params.pop("_rest", {})
                client.cookies.jar.set_cookie(Cookie(**params))
            except (TypeError, KeyError, ValueError):
                raise UpstreamError("Invalid saved cookie jar") from None
        client._loaded_from = (raw, _jar_values(client.cookies.jar))
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

mcp = FastMCP("Better Lavka", instructions=(
    "Grocery shopping in Yandex Lavka Uzbekistan. Usual flow: get_storefront_status shows whether the user is signed in "
    "(if not, start_login and poll get_login_status); use_address once (it is remembered); then search_products, "
    "add_to_cart (a whole list at once), repeat_order or the catalog; preview_order; and submit_order only after the user "
    "agreed to the exact total. Every result says what to do next. Ids in backticks are short three-word ids such as "
    "`amber fox wave`: pass them exactly as shown wherever a product_id, order_id or category_id is asked."))

from better_lavka_mcp.aliases import resolve


def tool():
    """Register a tool whose dictionary result is rendered as text for the model (JSON with BETTER_LAVKA_MCP_OUTPUT=json).

    The undecorated function stays importable and returns the dictionary, which keeps the logic easy to test."""
    from better_lavka_mcp.render import json_output, render

    def register(fn):
        @wraps(fn)
        async def as_text(*args, **kwargs):
            result = await fn(*args, **kwargs)
            return result if json_output() else render(fn.__name__, result)
        as_text.__signature__ = inspect.signature(fn).replace(return_annotation=str)
        mcp.tool(structured_output=False)(as_text)
        return fn
    return register

@tool()
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


# The delivery address chosen with use_address; remembered between sessions.
_delivery = None


def _current_delivery():
    global _delivery
    if _delivery is None:
        from better_lavka_mcp import preferences
        saved = preferences.load().get('address')
        if isinstance(saved, dict) and isinstance(saved.get('location'), list) and len(saved['location']) == 2:
            _delivery = saved
    return _delivery


def _location(longitude=None, latitude=None):
    """Explicit coordinates win; otherwise use the address chosen with use_address."""
    if longitude is None and latitude is None:
        delivery = _current_delivery()
        if delivery is None:
            raise ValueError('No delivery address yet: call use_address (for example use_address("home")) or pass longitude and latitude')
        return list(delivery['location'])
    if (type(longitude) not in (int, float) or type(latitude) not in (int, float)
            or not (55 <= longitude <= 74 and 37 <= latitude <= 46)):
        raise ValueError('Pass both longitude and latitude inside Uzbekistan')
    return [longitude, latitude]


def _address_summary(entry, index):
    address = entry['address']
    return {'index': index, 'label': address.get('label') or None,
            'short_address': address.get('shortAddress') or address.get('fullAddress')}


@tool()
@uz_only
async def use_address(address: str) -> dict:
    """Choose the delivery address by saved-address name ("Дом", "home"), part of the street, or index from list_saved_addresses. It is remembered for later sessions; search, cart and ordering then use it without coordinates."""
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
    from better_lavka_mcp import preferences
    preferences.update(address=_delivery)
    return {'selected': True, **_address_summary(entries[index], index)}


@tool()
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

@tool()
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

@tool()
@uz_only
async def search_addresses(query: str) -> dict:
    """Find any address by text, e.g. "Amir Temur 1": street, area and coordinates. Pass the coordinates as longitude, latitude to search or cart tools to shop there."""
    async with create_http() as http:
        return {"addresses": await Lavka(http).suggest(query)}

@tool()
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


@tool()
@uz_only
async def get_cart(longitude: float | None = None, latitude: float | None = None) -> dict:
    """What's in the cart: items, quantities, prices, delivery cost, ETA and total in UZS, for the address chosen with use_address (or at longitude, latitude)."""
    location = _location(longitude, latitude)
    async with create_http() as http:
        _, client = await _cart_client(http, location)
        return _cart_view(await client.retrieve())


@tool()
@uz_only
async def set_cart_quantity(product_id: str, quantity: int, longitude: float | None = None, latitude: float | None = None) -> dict:
    """Change the cart: set product_id to an exact quantity (0 removes it). Lavka applies its current price. Returns the updated cart."""
    location = _location(longitude, latitude)
    async with create_http() as http:
        _, client = await _cart_client(http, location)
        return _cart_view(await client.set_items({resolve(product_id): quantity}))


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
    location = _location(longitude, latitude)
    wanted, titles, not_found, needs_choice = {}, {}, [], []
    async with create_http() as http:
        lavka, client = await _cart_client(http, location)
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
            return {'added': [], 'needs_choice': needs_choice, 'not_found': not_found, 'unavailable': [], 'limited': [], 'cart': _cart_view(await client.retrieve())}
        result = await _add_products(client, wanted, titles)
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
    location = _location(longitude, latitude)
    async with create_http() as http:
        lavka, client = await _cart_client(http, location)
        if order_id == 'last':
            history = await lavka.order_history(1)
            if not history['orders']:
                raise ValueError('There are no past orders yet')
            order_id = history['orders'][0]['order_id']
        order = await lavka.order(resolve(order_id))
        wanted, titles = {}, {}
        for line in order['items']:
            wanted[line['product_id']] = wanted.get(line['product_id'], 0) + max(_quantity(line.get('quantity')), 1)
            titles[line['product_id']] = line['name']
        if not wanted:
            raise ValueError('That order has no products to repeat')
        result = await _add_products(client, wanted, titles)
    return {'order': {k: order[k] for k in ('order_id', 'created_at', 'total_uzs')}, **result}


@tool()
@uz_only
async def get_product(product_id: str, longitude: float | None = None, latitude: float | None = None) -> dict:
    """Product card: price, pack size, availability, description, calories and protein/fat/carbs per 100 g, shelf life, brand and country."""
    location = _location(longitude, latitude)
    async with create_http() as http:
        return await Lavka(http).product(resolve(product_id), location)


@tool()
@uz_only
async def list_categories(longitude: float | None = None, latitude: float | None = None) -> dict:
    """The store's catalog: groups ("Молочный прилавок", "Готовая еда"…) with their categories and category_id, for the delivery address."""
    location = _location(longitude, latitude)
    async with create_http() as http:
        return {'groups': await Lavka(http).categories(location)}


@tool()
@uz_only
async def get_category_products(category_id: str, subcategory: str | None = None, limit: int = 30,
                                longitude: float | None = None, latitude: float | None = None) -> dict:
    """Browse one category from list_categories: its shelves (subcategories) with counts and products with prices. Pass subcategory (e.g. "Яйца") to see one shelf. limit is 1–100."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    location = _location(longitude, latitude)
    async with create_http() as http:
        return await Lavka(http).category(resolve(category_id), location, subcategory, limit)


@tool()
@uz_only
async def list_saved_cards(longitude: float | None = None, latitude: float | None = None) -> dict:
    """Your saved cards as "**** 1234", for the address chosen with use_address (or at longitude, latitude)."""
    from better_lavka_mcp.payment import PaymentClient
    location = _location(longitude, latitude)
    async with create_http() as http:
        props = await Lavka(http).bootstrap()
        cards = await PaymentClient(http, props, location).lookup()
        return {'cards': cards.masked_cards}


@tool()
@uz_only
async def list_order_history(limit: int = 21, older_than: str | None = None) -> dict:
    """Past orders, newest first, with their items, total and address. For the next page pass next_older_than as older_than. limit is 1–21."""
    async with create_http() as http:
        return await Lavka(http).order_history(limit, None if older_than is None else resolve(older_than))


@tool()
@uz_only
async def get_order(order_id: str) -> dict:
    """One past order in full: items, products, delivery, service fee, discount, total, address and status."""
    async with create_http() as http:
        return await Lavka(http).order(resolve(order_id))


@tool()
@uz_only
async def get_payment_status(order_id: str) -> dict:
    """Payment status of an order (success, wait_user_action, failed…). When the bank asks for 3-D Secure it also returns redirect_url to open."""
    from better_lavka_mcp.payment import payment_status
    async with create_http() as http:
        props = await Lavka(http).bootstrap()
        return await payment_status(http, props, resolve(order_id))


@tool()
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


@tool()
@uz_only
async def list_saved_addresses() -> dict:
    """Your saved delivery addresses with their index and name. Pick one for the session with use_address."""
    from better_lavka_mcp.orders.submit import addresses
    async with create_http() as http:
        _, entries = await addresses(http)
        return {'addresses': [{'index': i, 'address': entry['address']} for i, entry in enumerate(entries)]}


@tool()
@uz_only
async def preview_order(saved_address_index: int | None = None, card_last4: str | None = None) -> dict:
    """Show the order before paying: address, items, total UZS and card, plus a five-minute token for submit_order. Defaults to the address chosen with use_address and to the card used last time (or your only card). Charges nothing."""
    from better_lavka_mcp import preferences
    from better_lavka_mcp.orders.submit import addresses, preview
    from better_lavka_mcp.payment import PaymentClient
    chosen_card = card_last4
    async with create_http() as http:
        if saved_address_index is None or card_last4 is None:
            _, entries = await addresses(http)
            if saved_address_index is None:
                delivery = _current_delivery()
                if delivery is None:
                    raise ValueError('Choose the delivery address first with use_address, or pass saved_address_index')
                ids = [i for i, entry in enumerate(entries) if entry.get('addressId') == delivery['address_id']]
                if len(ids) != 1:
                    raise ValueError('The address chosen with use_address is gone; call use_address again')
                saved_address_index = ids[0]
            if card_last4 is None:
                location = entries[saved_address_index]['address'].get('location') if 0 <= saved_address_index < len(entries) else None
                props = await Lavka(http).bootstrap()
                cards = [mask[-4:] for mask in (await PaymentClient(http, props, location).lookup()).masked_cards]
                remembered = preferences.load().get('card_last4')
                if remembered in cards:
                    card_last4 = remembered
                elif len(cards) == 1:
                    card_last4 = cards[0]
                else:
                    return {'preview': False, 'cards': ['**** ' + c for c in cards], 'note': 'Ask which card to use and pass card_last4; it is remembered for next time.'}
        result = await preview(http, saved_address_index, card_last4)
    if chosen_card is not None:
        preferences.update(card_last4=chosen_card)
    return result


@tool()
@uz_only
async def submit_order(order_preview_token: str, confirmed_total_uzs: str, explicit_confirmation: bool) -> dict:
    """Charges money: place the order from preview_order's token. Pass the exact total from the preview as confirmed_total_uzs and explicit_confirmation=true once the user agreed. Sent once; then waits briefly for the payment status and returns redirect_url if the bank asks for 3-D Secure."""
    from better_lavka_mcp.orders.submit import submit
    async with create_http() as http:
        return await submit(http, order_preview_token, confirmed_total_uzs, explicit_confirmation)


@tool()
@uz_only
async def reconcile_order_attempt(checked_order_history: bool, checked_payment: bool, accepts_duplicate_risk: bool, no_request_in_progress: bool, explicit_confirmation: bool) -> dict:
    """Local state only: record that you checked official order history and payment, that no request is in progress, and that you accept duplicate-order risk, then release the unknown checkout block. Verifies nothing, places no order, and cannot rule out delayed processing."""
    from better_lavka_mcp.orders.submit import reconcile_checkout as reconcile
    return reconcile(history_checked=checked_order_history, payment_checked=checked_payment,
                     acknowledge_uncertainty=accepts_duplicate_risk,
                     no_request_in_progress=no_request_in_progress, explicit_confirmation=explicit_confirmation)


@tool()
@uz_only
async def cancel_order(order_id: str, explicit_confirmation: bool) -> dict:
    """Cancels an order: send at most one cancellation request for the exact order_id after explicit_confirmation=true. A response does not prove cancellation, refund status is unknown, and repeat cancellation attempts are blocked."""
    from better_lavka_mcp.orders.submit import cancel
    async with create_http() as http:
        async def history():
            return await Lavka(http).order_history()
        return await cancel(http, resolve(order_id), explicit_confirmation, history)


from better_lavka_mcp.auth.tools import register_qr_tools
register_qr_tools(mcp, STATE, uz_only)
