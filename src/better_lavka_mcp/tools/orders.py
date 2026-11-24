"""Checkout tools: preview, confirmed submit, reconciliation and cancellation."""
from better_lavka_mcp import client
from better_lavka_mcp.aliases import resolve
from better_lavka_mcp.app import _current_delivery, tool, uz_only
from better_lavka_mcp.orders import submit as orders


@tool()
@uz_only
async def preview_order(saved_address_index: int | None = None, card_last4: str | None = None) -> dict:
    """Show the order before paying: address, items, total UZS and card, plus a five-minute token for submit_order. Defaults to the address chosen with use_address and to the card used last time (or your only card). Charges nothing."""
    from better_lavka_mcp import preferences
    from better_lavka_mcp.payment import PaymentClient
    chosen_card = card_last4
    async with client.create_http() as http:
        if saved_address_index is None or card_last4 is None:
            _, entries = await orders.addresses(http)
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
                props = await client.Lavka(http).bootstrap()
                cards = [mask[-4:] for mask in (await PaymentClient(http, props, location).lookup()).masked_cards]
                remembered = preferences.load().get('card_last4')
                if remembered in cards:
                    card_last4 = remembered
                elif len(cards) == 1:
                    card_last4 = cards[0]
                else:
                    return {'preview': False, 'cards': ['**** ' + c for c in cards], 'note': 'Ask which card to use and pass card_last4; it is remembered for next time.'}
        result = await orders.preview(http, saved_address_index, card_last4)
    if chosen_card is not None:
        preferences.update(card_last4=chosen_card)
    return result


@tool()
@uz_only
async def submit_order(order_preview_token: str, confirmed_total_uzs: str, explicit_confirmation: bool) -> dict:
    """Charges money: place the order from preview_order's token. Pass the exact total from the preview as confirmed_total_uzs and explicit_confirmation=true once the user agreed. Sent once; then waits briefly for the payment status and returns redirect_url if the bank asks for 3-D Secure."""
    async with client.create_http() as http:
        return await orders.submit(http, order_preview_token, confirmed_total_uzs, explicit_confirmation)


@tool()
@uz_only
async def reconcile_order_attempt(checked_order_history: bool, checked_payment: bool, accepts_duplicate_risk: bool, no_request_in_progress: bool, explicit_confirmation: bool) -> dict:
    """Local state only: record that you checked official order history and payment, that no request is in progress, and that you accept duplicate-order risk, then release the unknown checkout block. Verifies nothing, places no order, and cannot rule out delayed processing."""
    return orders.reconcile_checkout(history_checked=checked_order_history, payment_checked=checked_payment,
                                     acknowledge_uncertainty=accepts_duplicate_risk,
                                     no_request_in_progress=no_request_in_progress, explicit_confirmation=explicit_confirmation)


@tool()
@uz_only
async def cancel_order(order_id: str, explicit_confirmation: bool) -> dict:
    """Cancels an order: send at most one cancellation request for the exact order_id after explicit_confirmation=true. A response does not prove cancellation, refund status is unknown, and repeat cancellation attempts are blocked."""
    async with client.create_http() as http:
        async def history():
            return await client.Lavka(http).order_history()
        return await orders.cancel(http, resolve(order_id), explicit_confirmation, history)
