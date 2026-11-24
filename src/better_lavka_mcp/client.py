"""Lavka upstream client: allowlisted storefront calls and the persistent HTTP session."""
import html
import json
import logging
import re
import time
from http.cookiejar import Cookie
from pathlib import Path

import httpx

from better_lavka_mcp.constants import BASE
from better_lavka_mcp.errors import UpstreamError
from better_lavka_mcp.paths import session_file

STATE = session_file()
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
    calc = item.get('calculation')
    calc = calc if isinstance(calc, dict) else {}
    lines = []
    for line in calc.get('items') or []:
        if isinstance(line, dict) and line.get('productId'):
            lines.append({'product_id': line['productId'], 'name': clean_text(line.get('name')),
                          'quantity': line.get('count'), 'price_uzs': line.get('cost')})
    destinations = item.get('destinations')
    destinations = destinations if isinstance(destinations, list) else []
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
        r = await self.http.get(BASE + "/", params={"_lavka_mcp": str(time.time_ns())})
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
        range_: dict[str, int | str] = {'count': count}
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
        options = item.get('options')
        options = options if isinstance(options, dict) else {}
        ingredients = options.get('ingredients')
        ingredients = ingredients if isinstance(ingredients, dict) else {}
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

    _loaded_from: tuple[bytes, dict] | None = None

    async def __aexit__(self, exc_type=None, exc_value=None, traceback=None):
        try:
            self._save_rotated_cookies()
        except Exception:
            pass  # Saving is best effort; the call's own result matters more.
        return await super().__aexit__(exc_type, exc_value, traceback)

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
