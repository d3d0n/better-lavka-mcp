"""Turn tool results into short Markdown for the model: what happened, the data, and what to do next.

Set BETTER_LAVKA_MCP_OUTPUT=json to get the raw dictionaries instead.
"""
import json
import os
from decimal import Decimal, InvalidOperation

from better_lavka_mcp import aliases


def json_output():
    return os.environ.get('BETTER_LAVKA_MCP_OUTPUT', 'text').strip().lower() == 'json'


def ref(real_id):
    """A short id in backticks; tools accept it wherever the real id goes."""
    return f'`{aliases.short(real_id)}`'


def text(value, limit=160):
    """Upstream text (names, descriptions, addresses) as one plain line.

    It comes from Lavka, not from this server: newlines, Markdown and backticks are flattened so it
    can't pose as a heading, an id or a "Next:" instruction, and long text is cut.
    """
    from better_lavka_mcp.client import clean_text
    flat = ' '.join(clean_text(value).replace('`', "'").split())
    flat = flat.lstrip('#>*-+ ').replace('**', '')
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + '…'


def quoted(value, limit=400):
    """Longer upstream prose, quoted so it reads as data."""
    flat = text(value, limit)
    return f'“{flat}”' if flat else ''


def money(value, currency='сум'):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return '—'
    text = f'{amount:,.2f}'.rstrip('0').rstrip('.').replace(',', ' ')
    return f'{text} {currency}'


def _product(p, price_key='price_uzs', currency='сум'):
    parts = [money(p.get(price_key), currency)]
    if p.get('pack'):
        parts.append(str(p['pack']))
    if p.get('available') is False:
        parts.append('not available now')
    return f"- {text(p.get('title'))} — {', '.join(parts)} · {ref(p.get('id') or p.get('product_id'))}"


def _cart_lines(cart):
    items = cart.get('items') or []
    if not items:
        return ['**Cart is empty.**']
    lines = [f"**Cart** ({len(items)} item{'s' if len(items) != 1 else ''}):"]
    for item in items:
        note = '' if item.get('available', True) else ' — not available now'
        lines.append(f"- {text(item['title'])} ×{item['quantity']} — {money(item.get('price_uzs'))} each{note} · {ref(item['id'])}")
    total = f"Total **{money(cart.get('total_uzs'))}** incl. delivery {money(cart.get('delivery_uzs'))}"
    if cart.get('eta'):
        total += f" · arrives in {cart['eta']}"
    lines.append(total)
    if cart.get('available_for_checkout'):
        lines.append('Ready to order: preview_order shows address, card and the final total.')
    elif cart.get('checkout_unavailable_reason') == 'price-mismatch' and cart.get('price_changes'):
        lines.append(_price_changes(cart['price_changes'], accepted=False))
    else:
        lines.append(f"Can't order yet ({text(cart.get('checkout_unavailable_reason'), 60) or 'unknown reason'}): "
                     'remove items marked not available, or add more if the order is below the minimum.')
    return lines


def _additions(result):
    lines = []
    if result.get('added'):
        lines.append('**Added:** ' + '; '.join(f"{text(a['title'])} +{a['added']} (now {a['in_cart']} in cart)" for a in result['added']))
    for choice in result.get('needs_choice') or []:
        lines.append(f"**Pick one for \"{text(choice['query'], 60)}\"** ×{choice['quantity']} — not bought before, so nothing was added. "
                     'Ask the user which one, then call add_to_cart with its product_id:')
        lines += [_product(o) for o in choice['options']]
    if result.get('not_found'):
        lines.append('**Not found:** ' + ', '.join(text(q, 60) for q in result['not_found']) + ' — try other words or search_products.')
    if result.get('unavailable'):
        lines.append('**Sold out, left out:** ' + ', '.join(text(u) for u in result['unavailable']) + ' — offer the user a replacement.')
    for skip in result.get('skipped') or []:
        lines.append(f"**Skipped:** {text(skip['title'])} — {text(skip['reason'], 120)}.")
    for cap in result.get('limited') or []:
        lines.append(f"**Limited:** {text(cap['title'])} — at most {cap['max']} per order, asked for {cap['requested']}.")
    return lines


def _search(r):
    regional = r.get('country') in ('RU', 'KZ')
    currency = {'RUB': '₽', 'KZT': '₸'}.get((r['products'][0].get('currency') if r['products'] else 'UZS'), 'сум')
    if not r['products']:
        return 'Nothing found. Try other words.' if regional else 'Nothing found. Try other words, or browse with list_categories.'
    lines = [f"Found {len(r['products'])}" + (f" in Lavka {r['country']}:" if regional else ':')]
    for p in r['products']:
        lines.append(_product({**p, 'pack': p.get('quantity')}, 'price', currency))
    if regional:
        lines.append(f"{r['country']} prices are for reference: the cart, product cards and ordering work in Uzbekistan only (select_country UZ).")
    else:
        lines.append('Next: add_to_cart with a product_id, or get_product for calories and ingredients.')
    return '\n'.join(lines)


def _add(r):
    lines = []
    if r.get('order'):
        o = r['order']
        lines.append(f"Repeating order {ref(o['order_id'])} from {(o.get('created_at') or '')[:10]} (was {money(o.get('total_uzs'))}). Prices are today's.")
    lines += _additions(r)
    lines.append('')
    lines += _cart_lines(r['cart'])
    return '\n'.join(lines)


def _cart(r):
    return '\n'.join(_cart_lines(r))


def _use_address(r):
    if r.get('selected'):
        name = r.get('label') or 'Address'
        return (f"Delivering to **{text(name, 60)}**: {text(r.get('short_address'))}. Remembered for next time; "
                'search, cart and ordering now use it.')
    heading = {'ambiguous': 'Several saved addresses match — ask which one, then call use_address with its name or number:',
               'invalid_index': 'There is no saved address with that number. Saved addresses:'}
    lines = [heading.get(r.get('reason'), 'No saved address matches. Saved addresses:')]
    lines += [f"{c['index']}. {text(c.get('label'), 60) or '(no name)'} — {text(c.get('short_address'))}" for c in r['candidates']]
    return '\n'.join(lines)


def _addresses(r):
    lines = ['Saved addresses (pick one with use_address by name or number):']
    for a in r['addresses']:
        addr = a['address']
        extra = ', '.join(f'{k} {text(addr[k], 20)}' for k in ('entrance', 'floor', 'flat') if addr.get(k))
        lines.append(f"{a['index']}. {text(addr.get('label'), 60) or '(no name)'} — {text(addr.get('shortAddress') or addr.get('fullAddress'))}"
                     + (f' ({extra})' if extra else ''))
    return '\n'.join(lines)


def _found_addresses(r):
    if not r['addresses']:
        return 'No address found. Try the street and house number.'
    lines = ['Addresses found (pass longitude, latitude to shop there; to deliver, the user saves it in the Lavka app):']
    lines += [f"- {text(a['title'])}, {text(a['area'])} — longitude {a['longitude']}, latitude {a['latitude']}" for a in r['addresses']]
    return '\n'.join(lines)


def _product_card(r):
    lines = [f"**{text(r['title'])}**" + (f" ({text(r['brand'], 60)})" if r.get('brand') else ''),
             f"{money(r.get('price_uzs'))}, {r.get('pack') or ''}{'' if r.get('available') else ' — not available now'}".rstrip(', ')]
    n = r.get('nutrition_per_100g') or {}
    if n:
        lines.append('Per 100 g: ' + ', '.join(template.format(n[key]) for key, template in
                                               (('kcal', '{:g} kcal'), ('protein', 'protein {:g} g'), ('fat', 'fat {:g} g'), ('carbs', 'carbs {:g} g'))
                                               if n.get(key) is not None))
    if r.get('description'):
        lines.append(f"Description: {quoted(r['description'])}")
    details = r.get('details') or {}
    if r.get('composition') and r['composition'] not in details.values():
        lines.append(f"Ingredients: {quoted(r['composition'])}")
    lines += [f'{text(k, 60)}: {quoted(v, 300)}' for k, v in details.items()]
    if r.get('max_quantity'):
        lines.append(f"Up to {r['max_quantity']:g} per order.")
    lines.append(f"Next: add_to_cart with product_id {ref(r['id'])}.")
    return '\n'.join(lines)


def _categories(r):
    lines = ['Catalog (call get_category_products with a category id):']
    for g in r['groups']:
        lines.append(f"**{text(g['title'], 80)}**")
        lines += [f"- {text(c['title'], 80)} · {ref(c['category_id'])}" for c in g['categories']]
    return '\n'.join(lines)


def _category(r):
    lines = ['Shelves: ' + '; '.join(f"{text(s['title'], 80)} ({s['count']})" for s in r['subcategories'])]
    shelf = None
    for p in r['products']:
        if p['subcategory'] != shelf:
            shelf = p['subcategory']
            lines.append(f'**{text(shelf, 80)}**')
        lines.append(_product(p))
    if r['total'] > len(r['products']):
        lines.append(f"Showing {len(r['products'])} of {r['total']}. Pass subcategory to see one shelf, or a bigger limit.")
    return '\n'.join(lines)


def _cards(r):
    if not r['cards']:
        return 'No saved cards. The user adds one in the Lavka app.'
    return 'Saved cards: ' + ', '.join(r['cards']) + '. Pass the last four digits as card_last4 to preview_order.'


def _history(r):
    if not r['orders']:
        return 'No orders yet.'
    lines = []
    for o in r['orders']:
        lines.append(f"**{(o.get('created_at') or '')[:10]}** — {text(o.get('status_text') or o.get('status'), 40)}, {money(o.get('total_uzs'))}"
                     f" · {text(o.get('address'))} · {ref(o['order_id'])}")
        lines += [f"  - {text(i['name'])} ×{i['quantity']} — {money(i.get('price_uzs'))}" for i in o['items']]
    lines.append('Next: repeat_order with an order id puts it back in the cart; get_order shows fees.')
    if r.get('next_older_than'):
        lines.append(f"More: list_order_history(older_than=\"{aliases.short(r['next_older_than'])}\").")
    return '\n'.join(lines)


def _order(r):
    lines = [f"Order {ref(r['order_id'])} · {(r.get('created_at') or '')[:16].replace('T', ' ')} · {text(r.get('status_text') or r.get('status'), 40)}",
             f"Delivered to: {text(r.get('address'))}"]
    lines += [f"- {text(i['name'])} ×{i['quantity']} — {money(i.get('price_uzs'))} · {ref(i['product_id'])}" for i in r['items']]
    lines.append(f"Products {money(r.get('products_uzs'))} + delivery {money(r.get('delivery_uzs'))} + service fee {money(r.get('service_fee_uzs'))}"
                 f" = **{money(r.get('total_uzs'))}** (discount of {money(r.get('discount_uzs'))} already included).")
    if 'tracking' in r:
        lines.append(_tracking(r['tracking']))
    lines.append('Next: repeat_order with this id to order it again.')
    return '\n'.join(lines)


def _payment(r):
    status = r.get('payment_status')
    if r.get('redirect_url'):
        return f"Payment waits for 3-D Secure. Give the user this link to confirm: {r['redirect_url']}"
    meaning = {'success': 'paid', 'paid': 'paid', 'need_to_skip': 'accepted, no card step needed',
               'hold': 'money is held on the card; it is charged when the order is delivered',
               'fail': 'payment failed', 'failed': 'payment failed', 'rejected': 'payment rejected',
               'cancel': 'payment canceled', 'canceled': 'payment canceled', 'cancelled': 'payment canceled'}
    line = f"Payment status: {status} — {meaning.get(status, 'still processing; check again in a minute')}."
    error = r.get('payment_error') or {}
    if error:
        line += ' Lavka says: ' + quoted(' — '.join(v for v in (error.get('title'), error.get('text')) if v), 300)
    return line


def _tracking(t):
    """What Lavka's tracking screen tells the user, in its words."""
    if not t:
        return 'Lavka no longer tracks this order.'
    if t.get('state') == 'unknown':
        return "Tracking: unknown (Lavka didn't answer)."
    said = ' — '.join(x for x in (t.get('title'), t.get('subtitle')) if x)
    outcome = '/'.join(x for x in (t.get('status'), t.get('resolution')) if x)
    return f"Lavka says: {quoted(said, 300) or '(no text)'}" + (f" ({text(outcome, 40)})" if outcome else '') + '.'


def _price_changes(changes, accepted):
    listed = '; '.join(f"{text(c.get('title'))} {money(c.get('old_uzs'))} → {money(c.get('new_uzs'))}" for c in changes)
    if accepted:
        return f"**Prices changed since these were added:** {listed}. The total below uses the new prices."
    return f"**Prices changed since these were added:** {listed}. preview_order accepts the new prices; tell the user."


def _delivery_terms(a):
    """Everything the order sends about the address, so the user approves all of it."""
    where = ', '.join(text(a[k], 80) for k in ('city', 'street', 'house', 'buildingName') if a.get(k))
    unit = ', '.join(f'{label} {text(a[k], 20)}' for k, label in
                     (('entrance', 'entrance'), ('floor', 'floor'), ('flat', 'flat'), ('doorcode', 'door code'), ('doorbellName', 'doorbell')) if a.get(k))
    instructions = [label for k, label in (('leftAtDoor', 'leave at the door'), ('meetOutside', 'meet outside'), ('noDoorCall', "don't ring"))
                    if a.get(k) is True]
    line = f"Delivery to {where or 'saved address'}" + (f' ({unit})' if unit else '')
    if instructions:
        line += '; ' + ', '.join(instructions)
    if a.get('comment'):
        line += f"; courier note {quoted(a['comment'], 200)}"
    return line


def _preview(r):
    if r.get('preview') is False:
        if not r['cards']:
            return 'No saved card can pay for this order. The user adds or enables a card in the Lavka app, then call preview_order again.'
        return 'Which card? Saved: ' + ', '.join(r['cards']) + '. Call preview_order(card_last4="1234"); it is remembered.'
    lines = [f"**Order preview** — nothing is charged yet. {_delivery_terms(r['address'])}. Card {r['card']}."]
    if r.get('price_changes'):
        lines.append(_price_changes(r['price_changes'], accepted=True))
    for item in r['items']:
        lines.append(f"- {text(item.get('title'))} ×{item.get('quantity')} — {money(item.get('price'))}")
    lines.append(f"Total **{money(r['total_uzs'])}**.")
    lines.append(f"Show this to the user. Only after they say yes, call submit_order(order_preview_token=\"{r['preview_token']}\", "
                 f"confirmed_total_uzs=\"{r['total_uzs']}\", explicit_confirmation=true) within 5 minutes.")
    return '\n'.join(lines)


def _submit(r):
    outcome = r.get('outcome')
    if outcome == 'declined':
        return 'The user said no in the confirmation dialog. Nothing was sent or charged; the preview is still valid if they change their mind.'
    if outcome == 'submitted':
        if r.get('payment_status') in ('cancel', 'canceled', 'cancelled', 'fail', 'failed', 'rejected'):
            # Seen live: the order is created, the payment ends as "cancel" and Lavka cancels the order.
            lines = [f"**Order {ref(r['order_id'])} was created, but its payment ended as \"{text(r['payment_status'], 20)}\".**"]
            if r.get('payment_error'):
                lines.append(_payment(r))
            if 'tracking' in r:
                lines.append(_tracking(r['tracking']))
            lines.append('If the card was charged, Lavka returns the money when it cancels the order; the user can check their bank app. '
                         'Do not submit again on your own: ask the user first, and they may want another card.')
            if r.get('diagnostics_file'):
                lines.append(f"Lavka's answers to this checkout are saved, without ids or links, in {r['diagnostics_file']}.")
            return '\n'.join(lines)
        lines = [f"**Order placed** · {ref(r['order_id'])}."]
        lines.append(_payment(r))
        lines.append('Next: list_active_orders while it is on the way; cancel_order if the user changes their mind.')
        return '\n'.join(lines)
    if outcome == 'rejected':
        lines = ['**Lavka rejected the order.** Nothing was charged.']
        if r.get('recovery'):
            lines.append('Prices or fees changed. Read the cart again, run a new preview_order, show the new total and ask again.')
        else:
            lines.append(f"Codes: {', '.join(r.get('codes') or []) or 'none'}. Check the cart (get_cart) and try a new preview.")
        return '\n'.join(lines)
    return ('**Result unknown** — the order may or may not have gone through. Do NOT submit again. '
            'Ask the user to check the Lavka app; if there is no order, call reconcile_order_attempt with their confirmation, '
            'then start over with preview_order.')


def _active(r):
    if not r.get('orders'):
        return 'Lavka is tracking no orders. A just-placed order can take a moment to show up; list_order_history has the full list.'
    lines = ['Orders Lavka is tracking:']
    for o in r['orders']:
        lines.append(f"- {ref(o['order_id'])}: {_tracking(o)}" + (' Can still be canceled.' if o.get('cancelable') else ''))
    return '\n'.join(lines)


def _status(r):
    if r.get('is_open') is None and r.get('is_disabled') is None:
        store = 'status unknown'
    else:
        store = 'open' if r.get('is_open') and not r.get('is_disabled') else 'closed'
    name = f"Lavka{(' ' + r['country']) if r.get('country') else ''}"
    lines = [f"{name}: {store}; delivery {text(r.get('advertised_delivery_time'), 40) or 'time unknown'}."]
    if r.get('passport_status') is not None:
        lines.append('Signed in.' if r['passport_status'] == 'VALID' else 'Not signed in: call start_login.')
    if 'delivery_address' in r:
        a = r['delivery_address']
        lines.append(f"Delivering to **{text(a.get('label'), 60) or 'saved address'}**: {text(a.get('short_address'))}." if a
                     else 'No delivery address chosen yet: use_address.')
    if 'cart' in r:
        lines.append(_cart_summary(r['cart']))
    if 'latest_order' in r:
        lines.append(_latest_order(r['latest_order']))
    return '\n'.join(lines)


def _cart_summary(c):
    state = c.get('state')
    if state == 'no_address':
        return 'Cart: choose an address first (use_address).'
    if state != 'known':
        return "Cart: unknown (Lavka didn't answer) — get_cart to check."
    if not c.get('item_count'):
        return 'Cart: empty.'
    line = f"Cart: {c['item_count']} item{'s' if c['item_count'] != 1 else ''}, {money(c.get('total_uzs'))}"
    if c.get('available_for_checkout'):
        return line + ' — ready to order (preview_order).'
    return line + f" — can't order yet ({text(c.get('checkout_unavailable_reason'), 60) or 'unknown reason'}); get_cart shows why."


def _latest_order(o):
    state = o.get('state')
    if state == 'none':
        return 'No orders yet.'
    if state != 'known':
        return "Latest order: unknown (Lavka didn't answer) — list_order_history to check."
    # Lavka's own status word, as is: the latest known one, not a claim that the order is active or finished.
    when = (o.get('created_at') or '')[:16].replace('T', ' ')
    status = text(o.get('status_text') or o.get('status'), 40) or 'no status'
    return f"Latest order: {when} — «{status}», {money(o.get('total_uzs'))} · {ref(o['order_id'])}."


def _country(r):
    if r['country'] == 'UZ':
        return 'Country: Uzbekistan. All tools are available.'
    return f"Country: {r['country']}. Only get_storefront_status and search_products work here (anonymously); ordering is Uzbekistan-only."


def _login_status(r):
    meaning = {
        'pending': 'Waiting for the QR scan. Call get_login_status again in a few seconds.',
        'authenticated': 'Signed in. Next: use_address to pick where to deliver.',
        'login_required': 'Not signed in. Call start_login.',
        'expired': 'The QR code expired. Call start_login for a new one.',
        'already_pending': 'A login is already waiting for the scan. Call get_login_status.',
        'replacement_requires_approval': 'Already signed in. To switch accounts, ask the user and call start_login(replace_existing_session=true).',
        'cancelled': 'Login cancelled.',
        'session_changed': 'Signed in elsewhere meanwhile; the existing session was kept.',
    }
    return meaning.get(r.get('status'), f"Login: {r.get('status')}. {r.get('note') or ''}").strip()


def _start_login(r):
    if r.get('status') != 'pending':
        return _login_status(r)
    return ('Scan the QR code below in the Yandex app (or open this link on the phone where the Yandex app is signed in): '
            f"{r['qr_url']}\nThe link is a login key: show it only to the user. Then call get_login_status every few seconds.")


def _reconcile(r):
    return 'Checkout is unlocked. Start over with preview_order and get a fresh yes from the user.'


def _cancel(r):
    if r.get('outcome') == 'declined':
        return 'The user said no in the confirmation dialog. Nothing was sent; the order goes on.'
    if r.get('outcome') == 'canceled':
        return 'Order canceled. Refund timing is up to the bank.'
    return 'Cancellation sent, but not confirmed yet. Check list_order_history in a minute; do not send it again.'


def _count(r):
    return f"{r['count']} saved address(es). list_saved_addresses shows them."


RENDERERS = {
    'search_products': _search, 'add_to_cart': _add, 'repeat_order': _add, 'get_cart': _cart, 'set_cart_quantity': _cart,
    'use_address': _use_address, 'list_saved_addresses': _addresses, 'search_addresses': _found_addresses,
    'get_product': _product_card, 'list_categories': _categories, 'get_category_products': _category,
    'list_saved_cards': _cards, 'list_order_history': _history, 'get_order': _order, 'get_payment_status': _payment,
    'preview_order': _preview, 'submit_order': _submit, 'list_active_orders': _active, 'get_storefront_status': _status,
    'select_country': _country, 'get_login_status': _login_status, 'cancel_login': _login_status, 'start_login': _start_login,
    'reconcile_order_attempt': _reconcile, 'cancel_order': _cancel, 'count_saved_addresses': _count,
}


def render(tool, result):
    """Text for the model, or JSON when BETTER_LAVKA_MCP_OUTPUT=json."""
    if json_output():
        return json.dumps(result, ensure_ascii=False, indent=2)
    return RENDERERS[tool](result)
