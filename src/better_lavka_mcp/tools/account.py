"""Account and history tools: saved addresses, cards, orders and the tracking feed."""
from better_lavka_mcp import client
from better_lavka_mcp.aliases import resolve
from better_lavka_mcp.app import _current_delivery, _remember_delivery, account_fingerprint, tool, uz_only
from better_lavka_mcp.errors import UpstreamError
from better_lavka_mcp.orders import submit as orders


def _address_summary(entry, index):
    address = entry['address']
    return {'index': index, 'label': address.get('label') or None,
            'short_address': address.get('shortAddress') or address.get('fullAddress')}


@tool()
@uz_only
async def use_address(address: str) -> dict:
    """Choose the delivery address by its saved name ("Дом", "home"), part of the street, or its number in get_account. Remembered; search, cart and ordering then use it."""
    if not isinstance(address, str) or not address.strip():
        raise ValueError('Name the saved address, for example "home"')
    from better_lavka_mcp.auth.qr import cookie_identity
    async with client.create_http() as http:
        _, entries = await orders.addresses(http)
        account = cookie_identity(http.cookies.jar)
    query = address.strip().casefold()
    aliases = {'home': ('дом', 'uy'), 'дом': ('home', 'uy'), 'uy': ('home', 'дом'),
               'work': ('работа', 'ish'), 'работа': ('work', 'ish'), 'ish': ('work', 'работа')}
    wanted = (query,) + aliases.get(query, ())
    labels = [(entry['address'].get('label') or '').strip().casefold() for entry in entries]
    if query.isdigit():
        index = int(query)
        if index >= len(entries):
            return {'selected': False, 'reason': 'invalid_index',
                    'candidates': [_address_summary(entry, i) for i, entry in enumerate(entries)],
                    'note': 'Ask which one and call use_address again with its name or index.'}
        matches = [index]
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
    delivery = {'address_id': entries[index].get('addressId'), 'location': location, **_address_summary(entries[index], index),
                'account': [list(item) for item in account_fingerprint(account) or ()]}
    _remember_delivery(delivery)
    from better_lavka_mcp import preferences
    preferences.update(address=delivery)
    return {'selected': True, **_address_summary(entries[index], index)}


@tool()
@uz_only
async def get_account() -> dict:
    """Who is signed in, saved addresses (chosen one marked) and saved cards (remembered one marked)."""
    from better_lavka_mcp import preferences
    from better_lavka_mcp.payment import PaymentClient
    async with client.create_http() as http:
        props = await client.Lavka(http).bootstrap()
        if props.get('pageData', {}).get('passportInfo', {}).get('status') != 'VALID':
            return {'signed_in': False}
        login = next((c.value for c in http.cookies.jar if c.name == 'yandex_login' and c.value), None)
        _, entries = await orders.addresses(http)
        delivery = _current_delivery(http)
        chosen = delivery.get('address_id') if delivery else None
        location = delivery['location'] if delivery else next(
            (e['address'].get('location') for e in entries if isinstance(e['address'].get('location'), list)), None)
        cards = (await PaymentClient(http, props, location).lookup()).masked_cards if location else []
    remembered = preferences.load().get('card_last4')
    return {'signed_in': True, 'login': login,
            'addresses': [{'index': i, 'address': e['address'], 'chosen': chosen is not None and e.get('addressId') == chosen}
                          for i, e in enumerate(entries)],
            'cards': [{'card': mask, 'remembered': mask[-4:] == remembered} for mask in cards]}


@tool()
@uz_only
async def list_order_history(limit: int = 21, older_than: str | None = None) -> dict:
    """Orders, newest first, with items and total; orders still in progress show their live status (minutes left, courier distance). Next page: older_than=next_older_than. limit 1–21."""
    from better_lavka_mcp.orders.tracking import tracked_orders
    async with client.create_http() as http:
        lavka = client.Lavka(http)
        history = await lavka.order_history(limit, None if older_than is None else resolve(older_than))
        try:
            live = await tracked_orders(http, lavka.props)
        except UpstreamError:
            history['tracking'] = 'unknown'
            return history
        on_page = {order['order_id'] for order in history['orders']}
        for order in history['orders']:
            if order['order_id'] in live:
                order['tracking'] = live[order['order_id']]
        # An order still on its way must not hide behind the page size (e.g. limit=1 after a newer canceled one).
        history['active_elsewhere'] = [t for oid, t in live.items() if oid not in on_page and t.get('status') != 'closed']
        return history


@tool()
@uz_only
async def get_order(order_id: str) -> dict:
    """One order in full: items, fees, total, payment status (3-D Secure link if the bank asks) and live status."""
    from better_lavka_mcp.orders.tracking import tracking_state
    from better_lavka_mcp.payment import payment_status
    order_id = resolve(order_id)
    async with client.create_http() as http:
        lavka = client.Lavka(http)
        order = await lavka.order(order_id)
        try:
            order['tracking'] = (await tracking_state(http, lavka.props, [order_id])).get(order_id)
        except UpstreamError:
            order['tracking'] = {'state': 'unknown'}
        try:
            order['payment'] = await payment_status(http, lavka.props, order_id)
        except UpstreamError:
            order['payment'] = {'state': 'unknown'}
        return order
