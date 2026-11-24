"""Confirmed, single-attempt Lavka checkout; no automatic write retries."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

from better_lavka_mcp import paths
from better_lavka_mcp.cart import CartClient
from better_lavka_mcp.client import Lavka
from better_lavka_mcp.constants import BASE
from better_lavka_mcp.errors import UpstreamError
from better_lavka_mcp.headers import api_headers
from better_lavka_mcp.orders.cancel import cancel_order as cancel_once
from better_lavka_mcp.orders.guard import GUARD, AttemptBlocked, AttemptGuard
from better_lavka_mcp.orders.payload import build_provider_payload
from better_lavka_mcp.orders.rejection import sanitize_rejection, sanitize_upstream_error
from better_lavka_mcp.payment import PaymentClient

STATE = paths.data_dir() / 'order-previews.sqlite'
CANCEL_STATE = paths.data_dir() / 'cancellation.sqlite'
POSITION_FIELDS = ('location','placeId','country','city','street','house','depotId','floor','flat','comment','entrance','doorcode','buildingName','doorbellName','leftAtDoor','meetOutside','noDoorCall')
WARNING = ('Client-side preflight only: server-side total enforcement and idempotency are not established. '
           'Prices may change after preflight. No automatic retry; inspect official order history and bank on uncertainty. '
           'Other clients do not share this guard. Refund status after cancellation is unknown.')


class CartNotReady(ValueError):
    """The cart is empty, unavailable for checkout or has no exact total."""


def _ensure_default_state_dir(path):
    directory = paths.data_dir()
    if Path(path).parent != directory:
        return
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not paths.private_dir(info):
        raise ValueError('State directory must be a private owner-only directory')


def _db():
    path = Path(STATE)
    _ensure_default_state_dir(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if not paths.private_file(info):
            raise ValueError('Preview state must be private and owner-only')
    finally:
        os.close(fd)
    db = sqlite3.connect(path, timeout=30)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS order_previews (token TEXT PRIMARY KEY, snapshot TEXT NOT NULL, created REAL NOT NULL, used INTEGER NOT NULL)')
    return db


def _session(http):
    values = [c.value for c in http.cookies.jar if c.name == 'Session_id' and c.domain in ('.yandex.uz', 'yandex.uz') and c.path == '/' and not c.is_expired()]
    if len(values) != 1 or not values[0]:
        raise ValueError('An unambiguous Uzbekistan authenticated session is required')
    return hashlib.sha256(values[0].encode()).hexdigest()


def _token(token):
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,80}', token):
        raise ValueError('Invalid preview token')
    return hashlib.sha256(token.encode()).hexdigest()


async def addresses(http):
    client = Lavka(http)
    props = await client.bootstrap()
    if props.get('pageData', {}).get('passportInfo', {}).get('status') != 'VALID':
        raise UpstreamError('Authenticated Uzbekistan session required')
    data = await client.call('/api/v1/providers/address/v1/get-favorite-addresses', {})
    if not isinstance(data, list) or any(not isinstance(x, dict) or not isinstance(x.get('address'), dict) for x in data):
        raise UpstreamError('Unexpected favorite addresses')
    return client, data


async def snapshot(http, address_index, suffix, expected_address_id=None, prepare=False):
    client, entries = await addresses(http)
    if type(address_index) is not int or not 0 <= address_index < len(entries) or not isinstance(suffix, str) or not re.fullmatch(r'[0-9]{4}', suffix):
        raise ValueError('Select an exact saved address index and four-digit card suffix')
    entry = entries[address_index]
    if expected_address_id is not None and entry.get('addressId') != expected_address_id:
        raise ValueError('The saved address moved or vanished; call use_address again')
    address = entry['address']
    loc = address.get('location')
    if not isinstance(loc, list) or len(loc) != 2:
        raise ValueError('Saved address has no usable location')
    props = client.props or {}
    headers = api_headers(props)
    response = await http.get(BASE + '/api/v1/providers/v2/service-info', headers=headers,
        params={'position[location][0]':loc[0], 'position[location][1]':loc[1], 'fallbackCurrencySign':'сум', 'depotType':'regular'})
    if response.status_code != 200:
        raise UpstreamError('Service-info lookup failed')
    info = response.json()
    if not isinstance(info, dict) or info.get('currency') != 'UZS' or not info.get('currencySign') or not info.get('depotId'):
        raise UpstreamError('Service-info location/currency is unverified')
    cart_client = CartClient(http, props, loc, info['currencySign'])
    cards = await PaymentClient(http, props, loc).lookup()
    indexes = [i for i, mask in enumerate(cards.masked_cards) if mask == '**** ' + suffix]
    if len(indexes) != 1:
        raise ValueError('Card suffix is missing or ambiguous')
    card = cards.select(indexes[0])
    # Only the preview prepares the cart; the check before submit must see it unchanged.
    cart = await (cart_client.prepare_checkout(card) if prepare else cart_client.retrieve())
    if cart.get('availableForCheckout') is not True or not cart.get('items') or not isinstance(cart.get('totalPriceValue'), str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', cart['totalPriceValue']):
        raise CartNotReady('Nonempty available cart and exact total required')
    if cart.get('statuses', {}).get('isRover') is not False:
        raise ValueError('Non-rover delivery is not confirmed')
    if cart.get('timeslot') is not None:
        raise ValueError('Scheduled delivery is not supported')
    position = {k: address[k] for k in POSITION_FIELDS if k in address and address[k] is not None}
    position['depotId'] = info['depotId']
    if not all(position.get(k) for k in ('placeId','country','city','street','house')):
        raise ValueError('Saved address is incomplete')
    # Bind all raw item fields, fees and delivery metadata, not only displayed fields.
    binding = {'entry': entry, 'position':position, 'info':info, 'cart':cart, 'card':card,
               'index':address_index, 'suffix':suffix, 'expected_address_id':expected_address_id}
    try:
        return json.loads(json.dumps(binding, sort_keys=True, allow_nan=False)), props
    except (TypeError, ValueError):
        raise UpstreamError('Non-JSON checkout data') from None


def _guard_db():
    _ensure_default_state_dir(GUARD)
    db = AttemptGuard(GUARD)._connect()
    db.execute("CREATE TABLE IF NOT EXISTS mcp_attempt (slot INTEGER PRIMARY KEY, nonce TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS mcp_attempt_audit (id INTEGER PRIMARY KEY, recorded_at REAL NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL)")
    db.commit()
    return db


def _release_known_rejection(db):
    state = db.execute("SELECT state FROM attempt_state WHERE slot=1").fetchone()
    if state == ('rejected',):
        db.execute("INSERT INTO mcp_attempt_audit (recorded_at,event,detail) VALUES (?,?,?)", (time.time(), 'rejected_release', 'Previously recorded upstream rejection'))
        db.execute("DELETE FROM mcp_attempt WHERE slot=1")
        db.execute("DELETE FROM attempt_state WHERE slot=1")
        db.execute("DELETE FROM attempts WHERE slot=1")


def _check_guard():
    with _guard_db() as db:
        db.execute('BEGIN IMMEDIATE')
        _release_known_rejection(db)
        if db.execute('SELECT 1 FROM attempts WHERE slot=1').fetchone():
            raise ValueError('Previous checkout outcome is quarantined and unknown; server reason cannot be known. Check official order history and payment, then explicitly reconcile; delayed processing remains possible.')


def _reserve():
    nonce = secrets.token_hex(32)
    with _guard_db() as db:
        db.execute('BEGIN IMMEDIATE')
        _release_known_rejection(db)
        if db.execute('SELECT 1 FROM attempts WHERE slot=1').fetchone():
            raise AttemptBlocked('Previous checkout outcome is quarantined and unknown; reconcile before another attempt')
        db.execute('INSERT INTO attempts VALUES (1)')
        db.execute("INSERT INTO attempt_state VALUES (1, 'unknown', NULL)")
        db.execute('INSERT INTO mcp_attempt VALUES (1, ?)', (nonce,))
        db.execute('INSERT INTO mcp_attempt_audit (recorded_at,event,detail) VALUES (?,?,?)', (time.time(), 'reserved', 'Outcome unknown until response classified'))
    return nonce


def _finish(nonce, outcome, detail):
    with _guard_db() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT nonce FROM mcp_attempt WHERE slot=1').fetchone() != (nonce,):
            return
        db.execute('INSERT INTO mcp_attempt_audit (recorded_at,event,detail) VALUES (?,?,?)', (time.time(), outcome, detail))
        if outcome in ('submitted', 'rejected'):
            db.execute('DELETE FROM mcp_attempt WHERE slot=1')
            db.execute('DELETE FROM attempt_state WHERE slot=1')
            db.execute('DELETE FROM attempts WHERE slot=1')


def _release(nonce, detail):
    """Release our own reservation when no checkout request was sent.

    The reserved slot was never exposed to the network, so it can be freed
    without reconciliation. The preview stays consumed: a submit attempt that
    fails preflight requires a fresh preview.
    """
    with _guard_db() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT nonce FROM mcp_attempt WHERE slot=1').fetchone() != (nonce,):
            return
        db.execute('INSERT INTO mcp_attempt_audit (recorded_at,event,detail) VALUES (?,?,?)', (time.time(), 'preflight_released', detail))
        db.execute('DELETE FROM mcp_attempt WHERE slot=1')
        db.execute('DELETE FROM attempt_state WHERE slot=1')
        db.execute('DELETE FROM attempts WHERE slot=1')


def reconcile_checkout(*, history_checked, payment_checked, acknowledge_uncertainty, no_request_in_progress, explicit_confirmation):
    """Record user attestation, not verified evidence, and release an unknown slot."""
    if any(x is not True for x in (history_checked, payment_checked, acknowledge_uncertainty, no_request_in_progress, explicit_confirmation)):
        raise ValueError('Explicitly confirm history and payment checked, no request in progress, and acknowledge delayed processing and duplicate-order risk')
    with _guard_db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM attempts WHERE slot=1').fetchone():
            raise ValueError('No checkout attempt to reconcile')
        db.execute('INSERT INTO mcp_attempt_audit (recorded_at,event,detail) VALUES (?,?,?)', (time.time(), 'user_reconciled', 'User attests history and payment checked, no request in progress, and accepts delayed processing and duplicate risk; not independently verified'))
        db.execute('DELETE FROM mcp_attempt WHERE slot=1')
        db.execute('DELETE FROM attempt_state WHERE slot=1')
        db.execute('DELETE FROM attempts WHERE slot=1')
    return {'released': True, 'warning': 'User attestation is not proof; delayed processing and duplicate orders remain possible. Obtain a fresh preview and explicit confirmation.'}


async def preview(http, address_index, card_suffix, expected_address_id=None):
    binding, _ = await snapshot(http, address_index, card_suffix, expected_address_id, prepare=True)
    _check_guard()
    binding = {'data': binding, 'session': _session(http)}
    token = secrets.token_urlsafe(32)
    with _db() as db:
        db.execute('INSERT INTO order_previews VALUES (?, ?, ?, 0)', (_token(token), json.dumps(binding, sort_keys=True, ensure_ascii=False), time.time()))
    binding = binding['data']
    cart = binding['cart']
    return {'preview_token':token, 'expires_in_seconds':300, 'address':binding['position'],
            'items':cart['items'], 'total_uzs':cart['totalPriceValue'], 'currency':'UZS',
            'card':'**** '+card_suffix, 'delivery':'non-rover', 'warning':WARNING}


# Cart fields that change on every read without changing the order: per-call ETA stamps, the
# one-time submit token and the live stock limit per item (seen live moving between preview and
# submit). Items, quantities, prices, availability, total, address and card must match the preview;
# a stock limit below the ordered quantity shows up in those.
_VOLATILE = frozenset({'nextIdempotencyToken', 'minEtaTimestamp', 'maxEtaTimestamp', 'maxAmount', 'quantityLimit'})


def _terms(binding):
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in _VOLATILE}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    return strip(binding)


def _changed(old, new, path=''):
    """Dotted names of the fields that differ (names only, never values), at most five."""
    if isinstance(old, dict) and isinstance(new, dict):
        names = []
        for key in sorted(set(old) | set(new), key=str):
            names += _changed(old.get(key), new.get(key), f'{path}.{key}' if path else str(key))
        return names[:5]
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [name for i, (a, b) in enumerate(zip(old, new, strict=True)) for name in _changed(a, b, f'{path}[{i}]')][:5]
    return [] if old == new else [path or 'checkout data']


def _site_options(cart):
    """The optional submit fields the Lavka site sends for this cart (see its checkout page)."""
    options = {'useRover': False}
    paid = cart.get('paidServices') if isinstance(cart.get('paidServices'), dict) else {}
    for key in ('serviceFee', 'serviceFeeV2'):
        amount = (paid.get(key) or {}).get('amount') if isinstance(paid.get(key), dict) else None
        if isinstance(amount, str) and amount:
            options[key] = amount
    cashback = cart.get('cashback') if isinstance(cart.get('cashback'), dict) else {}
    # The site links the Plus wallet so the order earns cashback; it never spends cashback unless the user asks.
    if cashback.get('flow') == 'gain' and isinstance(cashback.get('walletId'), str) and cashback['walletId']:
        options['cashback'] = {'walletId': cashback['walletId']}
    return options


SUBMIT_PATH = '/api/v2/lavka/v1/orders/v2/submit'


def _snake(value):
    """camelCase keys to the snake_case the site's gateway takes (cartId -> cart_id), values untouched."""
    if isinstance(value, dict):
        return {re.sub(r'(?<!^)(?=[A-Z])', '_', str(key)).lower(): _snake(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_snake(item) for item in value]
    return value


async def submit(http, token, confirmed_total_uzs, explicit_confirmation, approve=None):
    """Send the one checkout request for a fresh, user-confirmed preview.

    The account-wide guard is reserved before the cart is refreshed and compared,
    so two processes holding different valid previews can never both reach the
    network. A submit attempt consumes its preview even when preflight then fails;
    if no request was sent, the reservation is released for a later attempt.

    `approve(preview_data)` asks the person directly (MCP elicitation) when the client can;
    a refusal sends nothing and leaves the preview unused.
    """
    if explicit_confirmation is not True or not isinstance(confirmed_total_uzs, str):
        raise ValueError('Explicit confirmation and exact total string required')
    digest = _token(token)
    with _db() as db:
        row = db.execute('SELECT snapshot, created, used FROM order_previews WHERE token=?', (digest,)).fetchone()
    if not row or row[2] or not 0 <= time.time()-row[1] < 300:
        raise ValueError('Preview expired, consumed, or unknown')
    stored = json.loads(row[0])
    if stored['session'] != _session(http):
        raise ValueError('Authenticated session changed; obtain a new preview')
    old = stored['data']
    if confirmed_total_uzs != old['cart']['totalPriceValue']:
        raise ValueError('Confirmed total differs from preview')
    if approve is not None and not await approve(old):
        return {'outcome': 'declined'}
    nonce = _reserve()
    try:
        with _db() as db:
            db.execute('BEGIN IMMEDIATE')
            cur = db.execute('UPDATE order_previews SET used=1 WHERE token=? AND used=0', (digest,))
            if cur.rowcount != 1:
                raise ValueError('Preview already consumed')
        fresh, props = await snapshot(http, old['index'], old['suffix'], old.get('expected_address_id'))
        if not 0 <= time.time()-row[1] < 300:
            raise ValueError('Preview expired during preflight')
        if stored['session'] != _session(http):
            raise ValueError('Authenticated session changed during preflight')
        if _terms(fresh) != _terms(old):
            raise ValueError('Checkout data changed (' + ', '.join(_changed(_terms(old), _terms(fresh))) + '); obtain and confirm a new preview')
        cart, pos, card = fresh['cart'], fresh['position'], fresh['card']
        payload = build_provider_payload(cart_id=cart['cartId'], cart_version=cart['cartVersion'], flow_version=cart['orderFlowVersion'], position=pos, depot_type='regular', payment_type=card['type'], payment_id=card['id'], options=_site_options(cart))
    except BaseException:
        _release(nonce, 'Preflight failed before any checkout write')
        raise
    headers = api_headers(props)
    # The Lavka site sends the cart's one-time token with every submit, so Lavka itself drops a duplicate.
    if isinstance(cart.get('nextIdempotencyToken'), str) and cart['nextIdempotencyToken']:
        headers['X-Idempotency-Token'] = cart['nextIdempotencyToken']
    try:
        # The site's own checkout path and body format (snake_case), captured from its "Оплатить" button.
        response = await http.post(BASE+SUBMIT_PATH, headers=headers, json=_snake(payload))
    except Exception as exc:
        _finish(nonce, 'unknown', 'Transport failure; server reason unavailable')
        return {'outcome':'unknown', 'transport_error': type(exc).__name__, 'warning':WARNING + ' No server response was received; reconcile before a fresh attempt.'}
    try:
        data = response.json()
    except ValueError:
        data = None
    result = {'outcome':'unknown', 'http_status':response.status_code, 'warning':WARNING}
    order_id = (data.get('orderId') or data.get('order_id')) if isinstance(data, dict) else None
    if response.status_code == 200 and isinstance(data, dict) and isinstance(order_id, str) and order_id:
        data = {**data, 'orderId': order_id}
        _finish(nonce, 'submitted', 'Order ID returned')
        result.update(outcome='submitted', order_id=data['orderId'])
        # Lavka's own answer, minus ids: which fields it sent and how the site should track payment.
        result['lavka_response'] = {'fields': sorted(str(k) for k in data)[:30],
                                    **({'track_payment': data['trackPayment']} if isinstance(data.get('trackPayment'), dict) else {})}
        sent_at = time.monotonic()
        payment = await _await_payment(http, props, data['orderId'], sent_at)
        trace = payment.pop('trace')
        result.update(payment)
        if result.get('payment_status') not in PAYMENT_OK:
            from better_lavka_mcp.orders.tracking import tracking_state
            try:
                result['tracking'] = (await tracking_state(http, props, [data['orderId']])).get(data['orderId'])
            except UpstreamError:
                result['tracking'] = {'state': 'unknown'}
        from better_lavka_mcp.orders import diagnostics
        saved = diagnostics.record({'order_id': data['orderId'], 'submit_response': diagnostics.redact(data),
                                    'request_fields': sorted(_snake(payload)), 'payment_trace': trace, 'tracking': result.get('tracking')})
        if saved:
            result['diagnostics_file'] = saved
    elif 400 <= response.status_code <= 599:
        projection = sanitize_rejection(response.status_code, data)
        result.update(projection)
        result['upstream_error'] = sanitize_upstream_error(response.status_code, data)
        if response.status_code == 400 and 'grocery_unavailable_for_checkout' in projection['codes']:
            _finish(nonce, 'rejected', json.dumps(projection, sort_keys=True))
            result['outcome'] = 'rejected'
            if {'bad_service_fee', 'bad_service_fee_v2'} & set(projection['codes']):
                result['recovery'] = {
                    'kind': 'checkout_price_or_fee_changed',
                    'old_total_uzs': old['cart']['totalPriceValue'],
                    'automatic_retry': False,
                    'fresh_preview_required': True,
                    'explicit_confirmation_required': True,
                    'instructions': [
                        'Yandex rejected checkout with a fee validation error; its storefront displays a generic price-changed alert. The cause and any product price change are not established.',
                        'Obtain a fresh cart and preview_order for the same address and card; do not reuse the consumed preview.',
                        'Compare and inform the user of old and new totals and item, fee, delivery, and availability changes; do not invent unavailable details.',
                        'Respect the user’s price budget and constraints; if the new total exceeds them, do not submit unless the user explicitly changes the budget.',
                        'Ask for explicit confirmation of the new preview and total before a new submit; never submit automatically.',
                    ],
                }
        else:
            _finish(nonce, 'unknown', 'Unclassified upstream response')
            result['note'] = 'Order outcome cannot be determined from this response; reconcile before a fresh attempt.'
    else:
        _finish(nonce, 'unknown', 'Unclassified upstream response')
        result['note'] = 'Server reason and order outcome cannot be determined; reconcile before a fresh attempt.'
    return result


PAYMENT_POLL_ATTEMPTS = 6
# Statuses the Lavka site treats as a finished, good payment.
PAYMENT_OK = frozenset({'success', 'paid', 'hold', 'need_to_skip'})
PAYMENT_POLL_INTERVAL = 2.0


async def _await_payment(http, props, order_id, started=None):
    """Poll payment status briefly after submit; never resubmits and never raises.

    'trace' lists every poll (seconds since submit, status, payload type, whether a 3-D Secure link came) for diagnostics."""
    import asyncio

    from better_lavka_mcp.payment import TERMINAL_PAYMENT_STATUSES, payment_status
    started = time.monotonic() if started is None else started
    last = {'payment_status': 'unknown', 'redirect_url': None}
    trace = []
    for attempt in range(PAYMENT_POLL_ATTEMPTS):
        if attempt:
            await asyncio.sleep(PAYMENT_POLL_INTERVAL)
        try:
            last = await payment_status(http, props, order_id)
        except Exception as error:
            trace.append({'after_s': round(time.monotonic() - started, 1), 'error': type(error).__name__})
            continue
        trace.append({'after_s': round(time.monotonic() - started, 1), 'status': last['payment_status'],
                      'payload_type': last.get('payload_type'), 'redirect': bool(last['redirect_url']),
                      **({'error': last['payment_error']} if last.get('payment_error') else {})})
        if last['payment_status'] in TERMINAL_PAYMENT_STATUSES or last['redirect_url']:
            break
    last = {**last, 'trace': trace}
    if last['redirect_url']:
        last['note'] = 'The bank asks for 3-D Secure: open redirect_url to confirm the payment, then check get_payment_status.'
    elif last['payment_status'] not in TERMINAL_PAYMENT_STATUSES:
        last['note'] = 'Payment is still processing; check get_payment_status in a moment.'
    return last


async def cancel(http, order_id, explicit_confirmation, history):
    client = Lavka(http)
    props = await client.bootstrap()
    _ensure_default_state_dir(CANCEL_STATE)
    result = await cancel_once(http=http, props=props, order_id=order_id, confirmed=explicit_confirmation, state_path=CANCEL_STATE, history=history)
    result['warning'] = WARNING
    return result
