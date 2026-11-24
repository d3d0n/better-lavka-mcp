"""Checkout tools: preview, confirmed submit, reconciliation and cancellation."""
from mcp.server.fastmcp import Context
from mcp.types import ClientCapabilities, ElicitationCapability
from pydantic import BaseModel, Field

from better_lavka_mcp import client
from better_lavka_mcp.aliases import resolve
from better_lavka_mcp.app import _current_delivery, tool, uz_only
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


async def _accept_price_changes(http, saved_address_index):
    from better_lavka_mcp.client import clean_text
    from better_lavka_mcp.tools.cart import _cart_client
    _, entries = await orders.addresses(http)
    location = entries[saved_address_index]['address'].get('location') if 0 <= saved_address_index < len(entries) else None
    if not isinstance(location, list):
        return []
    _, cart_client = await _cart_client(http, list(location))
    return [{**change, 'title': clean_text(change['title'])} for change in await cart_client.accept_price_changes()]


@tool()
@uz_only
async def preview_order(saved_address_index: int | None = None, card_last4: str | None = None) -> dict:
    """Show the order before paying: address, items, total UZS and card, plus a five-minute token for submit_order. Defaults to the address chosen with use_address and to the card used last time (or your only card). Charges nothing; if Lavka repriced items since they were added, it re-saves them at the new price and lists the changes."""
    from better_lavka_mcp import preferences
    from better_lavka_mcp.payment import PaymentClient
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
        try:
            result = await orders.preview(http, saved_address_index, card_last4, expected_address_id)
        except orders.CartNotReady:
            # Lavka repriced items since they were added: re-save them at the current price, as its site does.
            # The new prices end up in the preview total, which the user approves anyway.
            changes = await _accept_price_changes(http, saved_address_index)
            if not changes:
                raise
            result = await orders.preview(http, saved_address_index, card_last4, expected_address_id)
            result['price_changes'] = changes
    if chosen_card is not None:
        preferences.update(card_last4=chosen_card)
    return result


@tool()
@uz_only
async def submit_order(order_preview_token: str, confirmed_total_uzs: str, explicit_confirmation: bool, ctx: Context | None = None) -> dict:
    """Charges money: place the order from preview_order's token. Pass the exact total from the preview as confirmed_total_uzs and explicit_confirmation=true once the user agreed. If the client supports it, the user also confirms in a dialog. Sent once; then waits briefly for the payment status and returns redirect_url if the bank asks for 3-D Secure. A failed preflight spends the token: call preview_order again."""
    approve = (lambda data: _ask(ctx, _order_question(data))) if _can_ask(ctx) else None
    async with client.create_http() as http:
        return await orders.submit(http, order_preview_token, confirmed_total_uzs, explicit_confirmation, approve)


@tool()
@uz_only
async def reconcile_order_attempt(checked_order_history: bool, checked_payment: bool, accepts_duplicate_risk: bool, no_request_in_progress: bool, explicit_confirmation: bool) -> dict:
    """Local state only: record that you checked official order history and payment, that no request is in progress, and that you accept duplicate-order risk, then release the unknown checkout block. Verifies nothing, places no order, and cannot rule out delayed processing."""
    return orders.reconcile_checkout(history_checked=checked_order_history, payment_checked=checked_payment,
                                     acknowledge_uncertainty=accepts_duplicate_risk,
                                     no_request_in_progress=no_request_in_progress, explicit_confirmation=explicit_confirmation)


@tool()
@uz_only
async def cancel_order(order_id: str, explicit_confirmation: bool, ctx: Context | None = None) -> dict:
    """Cancels an order: send at most one cancellation request for the exact order_id after explicit_confirmation=true. If the client supports it, the user also confirms in a dialog. A response does not prove cancellation, refund status is unknown, and repeat cancellation attempts are blocked."""
    if explicit_confirmation is True and _can_ask(ctx) and not await _ask(ctx, f'Cancel Lavka order {order_id}?'):
        return {'outcome': 'declined'}
    async with client.create_http() as http:
        async def history():
            return await client.Lavka(http).order_history()
        return await orders.cancel(http, resolve(order_id), explicit_confirmation, history)
