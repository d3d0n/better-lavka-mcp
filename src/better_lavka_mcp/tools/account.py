"""Account and history tools: saved addresses, cards, orders and the tracking feed."""
from better_lavka_mcp import client
from better_lavka_mcp.aliases import resolve
from better_lavka_mcp.app import _location, _remember_delivery, tool, uz_only
from better_lavka_mcp.constants import BASE
from better_lavka_mcp.errors import UpstreamError
from better_lavka_mcp.orders import submit as orders


def _address_summary(entry, index):
    address = entry['address']
    return {'index': index, 'label': address.get('label') or None,
            'short_address': address.get('shortAddress') or address.get('fullAddress')}


@tool()
@uz_only
async def use_address(address: str) -> dict:
    """Choose the delivery address by saved-address name ("Дом", "home"), part of the street, or index from list_saved_addresses. It is remembered for later sessions; search, cart and ordering then use it without coordinates."""
    if not isinstance(address, str) or not address.strip():
        raise ValueError('Name the saved address, for example "home"')
    async with client.create_http() as http:
        _, entries = await orders.addresses(http)
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
    delivery = {'address_id': entries[index].get('addressId'), 'location': location, **_address_summary(entries[index], index)}
    _remember_delivery(delivery)
    from better_lavka_mcp import preferences
    preferences.update(address=delivery)
    return {'selected': True, **_address_summary(entries[index], index)}


@tool()
@uz_only
async def count_saved_addresses() -> dict:
    """Read-only: return the number of saved favorite addresses without their details. Requires an authenticated Uzbekistan session."""
    async with client.create_http() as http:
        return await client.Lavka(http).saved_address_count()


@tool()
@uz_only
async def list_saved_addresses() -> dict:
    """Your saved delivery addresses with their index and name. Pick one for the session with use_address."""
    async with client.create_http() as http:
        _, entries = await orders.addresses(http)
        return {'addresses': [{'index': i, 'address': entry['address']} for i, entry in enumerate(entries)]}


@tool()
@uz_only
async def list_saved_cards(longitude: float | None = None, latitude: float | None = None) -> dict:
    """Your saved cards as "**** 1234", for the address chosen with use_address (or at longitude, latitude)."""
    from better_lavka_mcp.payment import PaymentClient
    location = _location(longitude, latitude)
    async with client.create_http() as http:
        props = await client.Lavka(http).bootstrap()
        cards = await PaymentClient(http, props, location).lookup()
        return {'cards': cards.masked_cards}


@tool()
@uz_only
async def list_order_history(limit: int = 21, older_than: str | None = None) -> dict:
    """Past orders, newest first, with their items, total and address. For the next page pass next_older_than as older_than. limit is 1–21."""
    async with client.create_http() as http:
        return await client.Lavka(http).order_history(limit, None if older_than is None else resolve(older_than))


@tool()
@uz_only
async def get_order(order_id: str) -> dict:
    """One past order in full: items, products, delivery, service fee, discount, total, address and status."""
    async with client.create_http() as http:
        return await client.Lavka(http).order(resolve(order_id))


@tool()
@uz_only
async def get_payment_status(order_id: str) -> dict:
    """Payment status of an order (success, wait_user_action, failed…). When the bank asks for 3-D Secure it also returns redirect_url to open."""
    from better_lavka_mcp.payment import payment_status
    async with client.create_http() as http:
        props = await client.Lavka(http).bootstrap()
        return await payment_status(http, props, resolve(order_id))


@tool()
@uz_only
async def list_active_orders() -> dict:
    """Read-only: return the active-order tracking feed. An empty feed does not prove there are no orders and cannot reconcile an uncertain submission."""
    async with client.create_http() as http:
        props = await client.Lavka(http).bootstrap()
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
        from better_lavka_mcp.orders.tracking import TrackingShapeError, project_tracking_response
        try:
            return project_tracking_response(data)
        except TrackingShapeError:
            raise UpstreamError('Unverified active-order response; no conclusion possible') from None
