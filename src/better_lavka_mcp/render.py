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
    return f"- {p.get('title')} — {', '.join(parts)} · {ref(p.get('id') or p.get('product_id'))}"


def _cart_lines(cart):
    items = cart.get('items') or []
    if not items:
        return ['**Cart is empty.**']
    lines = [f"**Cart** ({len(items)} item{'s' if len(items) != 1 else ''}):"]
    for item in items:
        note = '' if item.get('available', True) else ' — not available now'
        lines.append(f"- {item['title']} ×{item['quantity']} — {money(item.get('price_uzs'))} each{note} · {ref(item['id'])}")
    total = f"Total **{money(cart.get('total_uzs'))}** incl. delivery {money(cart.get('delivery_uzs'))}"
    if cart.get('eta'):
        total += f" · arrives in {cart['eta']}"
    lines.append(total)
    if cart.get('available_for_checkout'):
        lines.append('Ready to order: preview_order shows address, card and the final total.')
    else:
        lines.append(f"Can't order yet ({cart.get('checkout_unavailable_reason') or 'unknown reason'}): "
                     'remove items marked not available, or add more if the order is below the minimum.')
    return lines


def _additions(result):
    lines = []
    if result.get('added'):
        lines.append('**Added:** ' + '; '.join(f"{a['title']} ×{a['quantity']}" for a in result['added']))
    for choice in result.get('needs_choice') or []:
        lines.append(f"**Pick one for \"{choice['query']}\"** ×{choice['quantity']} — not bought before, so nothing was added. "
                     'Ask the user which one, then call add_to_cart with its product_id:')
        lines += [_product(o) for o in choice['options']]
    if result.get('not_found'):
        lines.append('**Not found:** ' + ', '.join(result['not_found']) + ' — try other words or search_products.')
    if result.get('unavailable'):
        lines.append('**Sold out, left out:** ' + ', '.join(result['unavailable']) + ' — offer the user a replacement.')
    for cap in result.get('limited') or []:
        lines.append(f"**Limited:** {cap['title']} — store allows {cap['max']}, asked for {cap['requested']}.")
    return lines


def _search(r):
    currency = {'RUB': '₽', 'KZT': '₸'}.get((r['products'][0].get('currency') if r['products'] else 'UZS'), 'сум')
    if not r['products']:
        return 'Nothing found. Try other words, or browse with list_categories.'
    lines = [f"Found {len(r['products'])}:"]
    for p in r['products']:
        lines.append(_product({**p, 'pack': p.get('quantity')}, 'price', currency))
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
        return (f"Delivering to **{name}**: {r.get('short_address')}. Remembered for next time; "
                'search, cart and ordering now use it.')
    lines = ['Several saved addresses match — ask which one, then call use_address with its name or number:'
             if r.get('reason') == 'ambiguous' else 'No saved address matches. Saved addresses:']
    lines += [f"{c['index']}. {c.get('label') or '(no name)'} — {c.get('short_address')}" for c in r['candidates']]
    return '\n'.join(lines)


def _addresses(r):
    lines = ['Saved addresses (pick one with use_address by name or number):']
    for a in r['addresses']:
        addr = a['address']
        extra = ', '.join(f'{k} {addr[k]}' for k in ('entrance', 'floor', 'flat') if addr.get(k))
        lines.append(f"{a['index']}. {addr.get('label') or '(no name)'} — {addr.get('shortAddress') or addr.get('fullAddress')}"
                     + (f' ({extra})' if extra else ''))
    return '\n'.join(lines)


def _found_addresses(r):
    if not r['addresses']:
        return 'No address found. Try the street and house number.'
    lines = ['Addresses found (pass longitude, latitude to shop there; to deliver, the user saves it in the Lavka app):']
    lines += [f"- {a['title']}, {a['area']} — longitude {a['longitude']}, latitude {a['latitude']}" for a in r['addresses']]
    return '\n'.join(lines)


def _product_card(r):
    lines = [f"**{r['title']}**" + (f" ({r['brand']})" if r.get('brand') else ''),
             f"{money(r.get('price_uzs'))}, {r.get('pack') or ''}{'' if r.get('available') else ' — not available now'}".rstrip(', ')]
    n = r.get('nutrition_per_100g') or {}
    if n:
        lines.append('Per 100 g: ' + ', '.join(template.format(n[key]) for key, template in
                                               (('kcal', '{:g} kcal'), ('protein', 'protein {:g} g'), ('fat', 'fat {:g} g'), ('carbs', 'carbs {:g} g'))
                                               if n.get(key) is not None))
    if r.get('description'):
        lines.append(r['description'])
    lines += [f'{k}: {v}' for k, v in (r.get('details') or {}).items()]
    lines.append(f"Next: add_to_cart with product_id {ref(r['id'])}.")
    return '\n'.join(lines)


def _categories(r):
    lines = ['Catalog (call get_category_products with a category id):']
    for g in r['groups']:
        lines.append(f"**{g['title']}**")
        lines += [f"- {c['title']} · {ref(c['category_id'])}" for c in g['categories']]
    return '\n'.join(lines)


def _category(r):
    lines = ['Shelves: ' + '; '.join(f"{s['title']} ({s['count']})" for s in r['subcategories'])]
    shelf = None
    for p in r['products']:
        if p['subcategory'] != shelf:
            shelf = p['subcategory']
            lines.append(f'**{shelf}**')
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
        lines.append(f"**{(o.get('created_at') or '')[:10]}** — {o.get('status_text') or o.get('status')}, {money(o.get('total_uzs'))}"
                     f" · {o.get('address') or ''} · {ref(o['order_id'])}")
        lines += [f"  - {i['name']} ×{i['quantity']} — {money(i.get('price_uzs'))}" for i in o['items']]
    lines.append('Next: repeat_order with an order id puts it back in the cart; get_order shows fees.')
    if r.get('next_older_than'):
        lines.append(f"More: list_order_history(older_than=\"{aliases.short(r['next_older_than'])}\").")
    return '\n'.join(lines)


def _order(r):
    lines = [f"Order {ref(r['order_id'])} · {(r.get('created_at') or '')[:16].replace('T', ' ')} · {r.get('status_text') or r.get('status')}",
             f"Delivered to: {r.get('address')}"]
    lines += [f"- {i['name']} ×{i['quantity']} — {money(i.get('price_uzs'))} · {ref(i['product_id'])}" for i in r['items']]
    lines.append(f"Products {money(r.get('products_uzs'))} + delivery {money(r.get('delivery_uzs'))} + service fee {money(r.get('service_fee_uzs'))}"
                 f" − discount {money(r.get('discount_uzs'))} = **{money(r.get('total_uzs'))}**")
    lines.append('Next: repeat_order with this id to order it again.')
    return '\n'.join(lines)


def _payment(r):
    status = r.get('payment_status')
    if r.get('redirect_url'):
        return f"Payment waits for 3-D Secure. Give the user this link to confirm: {r['redirect_url']}"
    meaning = {'success': 'paid', 'paid': 'paid', 'hold': 'money is held on the card; it is charged when the order is delivered',
               'failed': 'payment failed', 'rejected': 'payment rejected', 'canceled': 'payment canceled', 'cancelled': 'payment canceled'}
    return f"Payment status: {status} — {meaning.get(status, 'still processing; check again in a minute')}."


def _preview(r):
    if r.get('preview') is False:
        return 'Which card? Saved: ' + ', '.join(r['cards']) + '. Call preview_order(card_last4="1234"); it is remembered.'
    a = r['address']
    where = ', '.join(str(a[k]) for k in ('street', 'house') if a.get(k)) or a.get('city') or 'saved address'
    extra = ', '.join(f'{k} {a[k]}' for k in ('entrance', 'floor', 'flat') if a.get(k))
    lines = [f"**Order preview** — nothing is charged yet. Delivery to {where}" + (f' ({extra})' if extra else '') + f", card {r['card']}."]
    for item in r['items']:
        lines.append(f"- {_clean(item.get('title'))} ×{item.get('quantity')} — {money(item.get('price'))}")
    lines.append(f"Total **{money(r['total_uzs'])}**.")
    lines.append(f"Show this to the user. Only after they say yes, call submit_order(order_preview_token=\"{r['preview_token']}\", "
                 f"confirmed_total_uzs=\"{r['total_uzs']}\", explicit_confirmation=true) within 5 minutes.")
    return '\n'.join(lines)


def _clean(text):
    from better_lavka_mcp.server import clean_text
    return clean_text(text)


def _submit(r):
    outcome = r.get('outcome')
    if outcome == 'submitted':
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
    if r.get('active_order_count') == 0:
        return 'No orders on the way right now.'
    return f"{r.get('feed_entry_count')} order(s) are being tracked. Details aren't decoded yet; the Lavka app shows the courier."


def _status(r):
    store = 'open' if r.get('is_open') and not r.get('is_disabled') else 'closed'
    lines = [f"Lavka{(' ' + r['country']) if r.get('country') else ''} is {store}; delivery {r.get('advertised_delivery_time') or 'time unknown'}."]
    if r.get('passport_status') is not None:
        lines.append('Signed in.' if r['passport_status'] == 'VALID' else 'Not signed in: call start_login.')
    return '\n'.join(lines)


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
    text = RENDERERS[tool](result)
    aliases.save()
    return text
