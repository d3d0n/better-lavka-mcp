"""Checkout tools: preview, confirmed submit, reconciliation and cancellation."""
import weakref

from mcp.server.fastmcp import Context
from mcp.types import ClientCapabilities, ElicitationCapability
from pydantic import BaseModel, Field

from better_lavka_mcp import client
from better_lavka_mcp.aliases import resolve
from better_lavka_mcp.app import _current_delivery, tool, uz_only
from better_lavka_mcp.errors import UpstreamError
from better_lavka_mcp.orders import submit as orders


class _Approval(BaseModel):
    approve: bool = Field(default=False, title='Yes, do it')


def _can_ask(ctx):
    try:
        return ctx is not None and ctx.session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability()))
    except (AttributeError, ValueError):  # no active request (direct calls, tests)
        return False


async def _ask(ctx, message):
    """Ask the person in the client's own dialog: the model can't answer it for them.

    True only for an explicit yes. A client that offers the dialog but fails to show it counts as no."""
    try:
        answer = await ctx.elicit(message, schema=_Approval)
    except Exception:
        return False
    return answer.action == 'accept' and answer.data.approve is True


def _order_question(data):
    from better_lavka_mcp.render import _delivery_terms, money, text
    cart = data['cart']
    items = '\n'.join(f"- {text(i.get('title'))} ×{i.get('quantity')}" for i in cart['items'])
    return (f"Place this Lavka order and charge {money(cart['totalPriceValue'])} to card **** {data['suffix']}?\n"
            f"{_delivery_terms(data['position'])}.\n{items}")


async def _address_cart(http, saved_address_index, expected_address_id=None):
    """The cart client for a saved address, or None when the address has no location.

    With expected_address_id, refuses when the list was reordered and that index is now another address."""
    from better_lavka_mcp.tools.cart import _cart_client
    _, entries = await orders.addresses(http)
    if expected_address_id is not None and not (0 <= saved_address_index < len(entries)
                                                and entries[saved_address_index].get('addressId') == expected_address_id):
        raise ValueError('The saved address moved or vanished; call use_address again')
    location = entries[saved_address_index]['address'].get('location') if 0 <= saved_address_index < len(entries) else None
    if not isinstance(location, list):
        return None
    return (await _cart_client(http, list(location)))[1]


async def _accept_price_changes(http, saved_address_index):
    from better_lavka_mcp.client import clean_text
    cart_client = await _address_cart(http, saved_address_index)
    if cart_client is None:
        return []
    return [{**change, 'title': clean_text(change['title'])} for change in await cart_client.accept_price_changes()]


async def _apply_promo(http, saved_address_index, code, expected_address_id):
    """Put the user's promo code on the cart of exactly the address being previewed."""
    cart_client = await _address_cart(http, saved_address_index, expected_address_id)
    if cart_client is None:
        raise ValueError('This address has no location, so a promo code cannot be applied')
    await cart_client.apply_promocode(code)


async def _promo_codes(http, saved_address_index, expected_address_id, applied):
    """The user's other valid codes, or None when Lavka didn't answer (unknown is not "none")."""
    try:
        cart_client = await _address_cart(http, saved_address_index, expected_address_id)
        offered = await cart_client.promocodes() if cart_client is not None else []
    except (UpstreamError, ValueError, KeyError, TypeError):
        return None
    on_cart = str((applied or {}).get('code') or '').casefold()
    return [p for p in offered if p['valid'] and p['code'].casefold() != on_cart]


# The latest preview of each MCP connection, so the model never sees or copies a token. A connection
# is usually one chat, but a host may run one connection for several chats: there, submit_order places
# the latest preview made in any of them. The confirmed total, the pre-submit check of cart, address
# and card, and the client's confirmation dialog (when offered) still guard what is placed.
_PREVIEWS = weakref.WeakKeyDictionary()
_NO_SESSION = object()  # direct calls without an MCP request (tests, scripts)
_DIRECT_PREVIEW = {}


def _connection(ctx):
    try:
        return ctx.session if ctx is not None else _NO_SESSION
    except (AttributeError, ValueError):  # no active request
        return _NO_SESSION


def _remember_preview(ctx, token):
    key = _connection(ctx)
    if key is _NO_SESSION:
        _DIRECT_PREVIEW['token'] = token
    else:
        _PREVIEWS[key] = token


def _forget_preview(ctx):
    key = _connection(ctx)
    if key is _NO_SESSION:
        _DIRECT_PREVIEW.pop('token', None)
    else:
        _PREVIEWS.pop(key, None)


def _take_preview(ctx):
    key = _connection(ctx)
    token = _DIRECT_PREVIEW.pop('token', None) if key is _NO_SESSION else _PREVIEWS.pop(key, None)
    if token is None:
        raise ValueError('No order preview on this connection: call preview_order, show it to the user, then submit_order')
    return token


@tool()
@uz_only
async def preview_order(saved_address_index: int | None = None, card_last4: str | None = None, spend_plus_points: bool | None = None, promo_code: str | None = None, ctx: Context | None = None) -> dict:
    """Show the order before paying: address, items, card, total; submit_order places exactly this preview within 5 minutes. Uses the chosen address and remembered card. spend_plus_points=true pays part with Plus points; promo_code applies one of the user's codes (the preview lists them). Charges nothing."""
    from better_lavka_mcp import preferences
    from better_lavka_mcp.payment import PaymentClient
    # A new preview replaces the old one even when it fails or asks for a card: the user is
    # no longer looking at the old terms, so submit_order must not place them.
    _forget_preview(ctx)
    flow = None if spend_plus_points is None else ('charge' if spend_plus_points else 'gain')
    chosen_card = card_last4
    expected_address_id = None
    async with client.create_http() as http:
        if saved_address_index is None or card_last4 is None:
            _, entries = await orders.addresses(http)
            if saved_address_index is None:
                delivery = _current_delivery(http)
                if delivery is None:
                    raise ValueError('Choose the delivery address first with use_address, or pass saved_address_index')
                ids = [i for i, entry in enumerate(entries) if entry.get('addressId') == delivery['address_id']]
                if len(ids) != 1:
                    raise ValueError('The address chosen with use_address is gone; call use_address again')
                saved_address_index = ids[0]
                # snapshot() refetches the list; bind the id so a reordering cannot swap the address.
                expected_address_id = entries[ids[0]].get('addressId')
            if card_last4 is None:
                location = entries[saved_address_index]['address'].get('location') if 0 <= saved_address_index < len(entries) else None
                props = await client.Lavka(http).bootstrap()
                cards = [mask[-4:] for mask in (await PaymentClient(http, props, location).lookup()).masked_cards]
                if not cards:
                    return {'preview': False, 'cards': []}
                remembered = preferences.load().get('card_last4')
                if remembered in cards:
                    card_last4 = remembered
                elif len(cards) == 1:
                    card_last4 = cards[0]
                else:
                    return {'preview': False, 'cards': ['**** ' + c for c in cards], 'note': 'Ask which card to use and pass card_last4; it is remembered for next time.'}
        if promo_code:  # a refused code stops the preview: the user asked for it
            await _apply_promo(http, saved_address_index, promo_code, expected_address_id)
        try:
            result = await orders.preview(http, saved_address_index, card_last4, expected_address_id, flow)
        except orders.CartNotReady:
            # Lavka repriced items since they were added: re-save them at the current price, as its site does.
            # The new prices end up in the preview total, which the user approves anyway.
            changes = await _accept_price_changes(http, saved_address_index)
            if not changes:
                raise
            result = await orders.preview(http, saved_address_index, card_last4, expected_address_id, flow)
            result['price_changes'] = changes
        codes = await _promo_codes(http, saved_address_index, expected_address_id, result.get('promo_applied'))
        if codes is None:
            result['promo_codes'] = 'unknown'
        elif codes:
            result['promo_codes'] = codes
    if chosen_card is not None:
        preferences.update(card_last4=chosen_card)
    _remember_preview(ctx, result.pop('preview_token'))
    return result


@tool()
@uz_only
async def submit_order(confirmed_total_uzs: str, explicit_confirmation: bool, ctx: Context | None = None) -> dict:
    """Charges money. Places the latest preview_order on this connection (normally this chat). Only after the user agreed: confirmed_total_uzs = the preview's exact total, explicit_confirmation=true. Sent once; returns the payment status (3-D Secure link if needed). Any attempt uses up the preview."""
    token = _take_preview(ctx)
    approve = (lambda data: _ask(ctx, _order_question(data))) if _can_ask(ctx) else None
    async with client.create_http() as http:
        return await orders.submit(http, token, confirmed_total_uzs, explicit_confirmation, approve)


@tool()
@uz_only
async def reconcile_order_attempt(checked_order_history: bool, checked_payment: bool, accepts_duplicate_risk: bool, no_request_in_progress: bool, explicit_confirmation: bool) -> dict:
    """Only after a checkout ended with an unknown result: record that order history and payment were checked, nothing is in flight and duplicate risk is accepted, to unblock checkout. Places nothing."""
    return orders.reconcile_checkout(history_checked=checked_order_history, payment_checked=checked_payment,
                                     acknowledge_uncertainty=accepts_duplicate_risk,
                                     no_request_in_progress=no_request_in_progress, explicit_confirmation=explicit_confirmation)


@tool()
@uz_only
async def cancel_order(order_id: str, explicit_confirmation: bool, ctx: Context | None = None) -> dict:
    """Cancel an order, once, with explicit_confirmation=true after the user agreed. Refund timing is up to the bank."""
    if explicit_confirmation is True and _can_ask(ctx) and not await _ask(ctx, f'Cancel Lavka order {order_id}?'):
        return {'outcome': 'declined'}
    async with client.create_http() as http:
        async def history():
            return await client.Lavka(http).order_history()
        return await orders.cancel(http, resolve(order_id), explicit_confirmation, history)
