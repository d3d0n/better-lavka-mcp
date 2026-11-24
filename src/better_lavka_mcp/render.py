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


def _stock_note(p):
    """Stock worth saying: sold out, or Lavka's own "running low" flag with the per-order maximum."""
    if p.get('available') is False or p.get('orderable') == 0:
        return 'sold out'
    if p.get('low_stock'):
        return 'running low' + (f", up to {p['orderable']:g} per order" if p.get('orderable') else '')
    return None


def _was(p, currency='сум'):
    """" (was 54 990 сум, −25%)" for a crossed-out price, else ""."""
    if not p.get('old_price'):
        return ''
    return f" (was {money(p['old_price'], currency)}" + (f", −{p['discount_percent']}%" if p.get('discount_percent') else '') + ')'


def _product(p, price_key='price_uzs', currency='сум'):
    parts = [money(p.get(price_key), currency) + _was(p, currency)]
    if p.get('pack'):
        parts.append(str(p['pack']))
    if note := _stock_note(p):
        parts.append(note)
    if p.get('favorite'):
        parts.append('favorite')
    if p.get('bought_before'):
        parts.append('bought before')
    return f"- {text(p.get('title'))} — {', '.join(parts)} · {ref(p.get('id') or p.get('product_id'))}"


def _option(o):
    return f"{text(o.get('title'))} — {money(o.get('price_uzs'))}" + (f", {text(o['pack'], 20)}" if o.get('pack') else '') + f" · {ref(o['id'])}"


def _cart_stock(item):
    """(suffix for the cart line, whether the depot can't fill this line)."""
    have, want = item.get('orderable'), _qty(item.get('quantity'))
    if item.get('available') is False or have == 0:
        return ' — **sold out**', True
    if have is not None and want is not None and want > have:
        return f" — **only {have:g} can be ordered now**", True
    if item.get('low_stock'):
        return ' — running low', False
    return '', False


def _shortage(item):
    have = item.get('orderable')
    return f"{text(item['title'])} is sold out" if not have else f"{text(item['title'])} can be ordered only up to {have:g}"


def _qty(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _points(p):
    """Plus points worth telling the user about, or None."""
    if not p:
        return None
    if p.get('spending'):
        return f"Plus points: {p['spending']:,} go towards this order (already taken off the total).".replace(',', ' ')
    if p.get('available'):
        return (f"Plus points: up to {p['available']:,} can go towards this order — preview_order(spend_plus_points=true)."
                .replace(',', ' ') + (f" Earns {p['earning']:,} otherwise.".replace(',', ' ') if p.get('earning') else ''))
    if p.get('earning'):
        return f"Plus points: this order earns {p['earning']:,}.".replace(',', ' ')
    return None


def _cart_lines(cart):
    items = cart.get('items') or []
    if not items:
        return ['**Cart is empty.**']
    lines = [f"**Cart** ({len(items)} item{'s' if len(items) != 1 else ''}):"]
    short = []
    for item in items:
        note, problem = _cart_stock(item)
        if problem:
            short.append(item)
        lines.append(f"- {text(item['title'])} ×{item['quantity']} — {money(item.get('price_uzs'))} each{note} · {ref(item['id'])}")
        if item.get('replacements'):
            lines.append('  Lavka suggests instead: ' + '; '.join(_option(o) for o in item['replacements']))
    total = f"Total **{money(cart.get('total_uzs'))}** incl. delivery {money(cart.get('delivery_uzs'))}"
    if cart.get('eta'):
        total += f" · arrives in {cart['eta']}"
    lines.append(total)
    if points := _points(cart.get('plus_points')):
        lines.append(points)
    if cart.get('available_for_checkout'):
        lines.append('Ready to order: preview_order shows address, card and the final total.')
    elif short:
        lines.append("Can't order yet: " + '; '.join(_shortage(i) for i in short) + '. Ask the user: remove it '
                     '(set_cart_quantity 0), lower the quantity, or pick a replacement with search_products.')
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
        why = (f"Lavka found nothing for these exact words, only for «{text(choice['searched_for'], 60)}», so nothing was added. "
               if choice.get('searched_for') else 'not bought before, so nothing was added. ')
        lines.append(f"**Pick one for \"{text(choice['query'], 60)}\"** ×{choice['quantity']} — {why}"
                     'Ask the user which one, then call add_to_cart with its product_id:')
        lines += [_product(o) for o in choice['options']]
    if result.get('not_found'):
        lines.append('**Not found:** ' + ', '.join(text(q, 60) for q in result['not_found']) + ' — try other words or search_products.')
    if result.get('unavailable'):
        lines.append('**Sold out, left out:** ' + ', '.join(text(u) for u in result['unavailable']) + ' — offer the user a replacement.')
    for offer in result.get('replacements') or []:
        lines.append(f"Lavka suggests instead of {text(offer['for'])}: " + '; '.join(_option(o) for o in offer['options']) + '.')
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
    if r.get('usual'):
        lines = ['What the user usually buys here (favorites first, then most recently bought):']
        lines += [_product({**p, 'pack': p.get('quantity'), 'bought_before': False}, 'price', currency) for p in r['products']]
        lines.append('Next: add_to_cart with the product_ids the user wants.')
        return '\n'.join(lines)
    lines = [f"Found {len(r['products'])}" + (f" in Lavka {r['country']}:" if regional else ':')]
    if r.get('searched_for'):
        lines[0] = f"Lavka found nothing for the exact words; results for «{text(r['searched_for'], 60)}»:"
    for p in r['products']:
        lines.append(_product({**p, 'pack': p.get('quantity')}, 'price', currency))
    if regional:
        lines.append(f"{r['country']} prices are for reference: the cart, product cards and ordering work in Uzbekistan only.")
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


def _account(r):
    if not r.get('signed_in'):
        return 'Not signed in: call login.'
    lines = ['Signed in' + (f" as {text(r['login'], 60)}" if r.get('login') else '') + '.']
    lines.append('Saved addresses (choose one with use_address by name or number):' if r['addresses']
                 else 'No saved addresses: the user adds one in the Lavka app.')
    for a in r['addresses']:
        addr = a['address']
        extra = ', '.join(f'{k} {text(addr[k], 20)}' for k in ('entrance', 'floor', 'flat') if addr.get(k))
        lines.append(f"{a['index']}. {text(addr.get('label'), 60) or '(no name)'} — {text(addr.get('shortAddress') or addr.get('fullAddress'))}"
                     + (f' ({extra})' if extra else '') + (' · **chosen**' if a.get('chosen') else ''))
    if r['cards']:
        lines.append('Cards: ' + ', '.join(c['card'] + (' (remembered for paying)' if c['remembered'] else '') for c in r['cards'])
                     + '. Another card: preview_order(card_last4="1234").')
    else:
        lines.append('No saved cards: the user adds one in the Lavka app.')
    return '\n'.join(lines)


def _found_addresses(r):
    if not r['addresses']:
        return 'No address found. Try the street and house number.'
    lines = ['Addresses found (pass longitude, latitude to shop there; to deliver, the user saves it in the Lavka app):']
    lines += [f"- {text(a['title'])}, {text(a['area'])} — longitude {a['longitude']}, latitude {a['latitude']}" for a in r['addresses']]
    return '\n'.join(lines)


def _product_card(r):
    lines = [f"**{text(r['title'])}**" + (f" ({text(r['brand'], 60)})" if r.get('brand') else ''),
             f"{money(r.get('price_uzs'))}{_was(r)}, {r.get('pack') or ''}" + (f" — {note}" if (note := _stock_note(r)) else '')]
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
    if r.get('max_quantity') and not (r.get('low_stock') or r.get('available') is False):
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


def _history(r):
    if not r['orders']:
        return 'No orders yet.'
    lines = []
    for o in r['orders']:
        lines.append(f"**{(o.get('created_at') or '')[:10]}** — {text(o.get('status_text') or o.get('status'), 40)}, {money(o.get('total_uzs'))}"
                     f" · {text(o.get('address'))} · {ref(o['order_id'])}")
        if o.get('tracking'):
            lines.append(f"  {_tracking(o['tracking'])}{_still_possible(o['tracking'])}")
        lines += [f"  - {text(i['name'])} ×{i['quantity']} — {money(i.get('price_uzs'))}" for i in o['items']]
    if r.get('tracking') == 'unknown':
        lines.append("Live status: unknown (Lavka's tracking didn't answer) — get_order shows it for one order.")
    for t in r.get('active_elsewhere') or []:
        lines.append(f"Also in progress, not on this page: {ref(t['order_id'])}: {_tracking(t)}{_still_possible(t)}")
    lines.append('Next: repeat_order with an order id puts it back in the cart; get_order shows fees and payment.')
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
        lines.append(_tracking(r['tracking']) + _still_possible(r['tracking'] or {}))
    if 'payment' in r:
        lines.append("Payment status: unknown (Lavka didn't answer)." if r['payment'].get('state') == 'unknown' else _payment(r['payment']))
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


def _still_possible(t):
    can = [label for key, label in (('cancelable', 'canceled'), ('can_edit_address', 'given another address')) if t.get(key)]
    return f" Can still be {' or '.join(can)}." if can else ''


def _tracking(t):
    """What Lavka's tracking screen tells the user, in its words."""
    if not t:
        return 'Lavka no longer tracks this order.'
    if t.get('state') == 'unknown':
        return "Tracking: unknown (Lavka didn't answer)."
    said = ' — '.join(x for x in (t.get('title'), t.get('subtitle')) if x)
    outcome = '/'.join(x for x in (t.get('status'), t.get('resolution')) if x)
    line = f"Lavka says: {quoted(said, 300) or '(no text)'}" + (f" ({text(outcome, 40)})" if outcome else '') + '.'
    facts = []
    if t.get('eta_minutes') is not None and t.get('resolution') is None and t.get('status') != 'closed':
        facts.append(f"about {t['eta_minutes']} min to go")
    courier = t.get('courier') or {}
    if courier.get('distance_m') is not None:
        km = courier['distance_m'] / 1000
        facts.append(f"courier{' on ' + text(courier['transport'], 20) if courier.get('transport') else ''} "
                     + (f"{km:.1f} km away" if km >= 1 else f"{courier['distance_m']} m away"))
    if t.get('heavy') and t.get('status') != 'closed':
        facts.append('heavy order')
    if t.get('number'):
        facts.append(f"order № {text(t['number'], 30)}")
    if t.get('resolution') == 'canceled' and t.get('price_uzs') == '0' and t.get('original_price_uzs'):
        # The order's price, not a bank record: a hold or refund on the card is not visible here.
        facts.append(f"Lavka's price for it is now 0 (was {money(t['original_price_uzs'])}); whether the card was refunded isn't shown here")
    if facts:
        joined = '; '.join(facts)
        line += ' ' + joined[0].upper() + joined[1:] + '.'
    return line


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
    if points := _points(r.get('plus_points')):
        lines.append(points)
    if r.get('promo_applied'):
        lines.append(f"Promo code {text(r['promo_applied']['code'], 40)} applied: {text(r['promo_applied']['discount'], 30)} (already in the total).")
    if r.get('promo_codes') == 'unknown':
        lines.append("Promo codes: unknown (Lavka didn't answer); the user may have some.")
    if isinstance(r.get('promo_codes'), list) and r['promo_codes']:
        lines.append('The user also has: ' + '; '.join(
            f"«{text(p['title'], 60)}»" + (f" {text(p['subtitle'], 60)}" if p.get('subtitle') else '')
            + (f" until {str(p['expires'])[:10]}" if p.get('expires') else '') + f" — promo_code=\"{text(p['code'], 40)}\""
            for p in r['promo_codes']) + '. Ask before using one: preview_order(promo_code=...).')
    lines.append(f"Show this to the user. Only after they say yes, call submit_order(confirmed_total_uzs=\"{r['total_uzs']}\", "
                 'explicit_confirmation=true) within 5 minutes; it places exactly this preview.')
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
        lines.append('Next: get_order or list_order_history show where it is; cancel_order if the user changes their mind.')
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


def _status(r):
    if r.get('is_open') is None and r.get('is_disabled') is None:
        store = 'status unknown'
    else:
        store = 'open' if r.get('is_open') and not r.get('is_disabled') else 'closed'
    name = f"Lavka{(' ' + r['country']) if r.get('country') else ''}"
    lines = [f"{name}: {store}; delivery {text(r.get('advertised_delivery_time'), 40) or 'time unknown'}."]
    if r.get('passport_status') is not None:
        lines.append('Signed in.' if r['passport_status'] == 'VALID' else 'Not signed in: call login.')
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


def _login_status(r):
    meaning = {
        'pending': 'Waiting for the QR scan. Call login again in a few seconds.',
        'authenticated': 'Signed in. Next: use_address to pick where to deliver (get_account lists the addresses).',
        'login_required': 'Not signed in. Call login.',
        'expired': 'The QR code expired. Call login for a new one.',
        'already_pending': 'A login is already waiting for the scan. Call login again to check it.',
        'replacement_requires_approval': 'Already signed in. To switch accounts, ask the user and call login(replace_existing_session=true).',
        'cancelled': 'Login cancelled.',
        'session_changed': 'Signed in elsewhere meanwhile; the existing session was kept.',
    }
    return meaning.get(r.get('status'), f"Login: {r.get('status')}. {r.get('note') or ''}").strip()


def _login(r):
    if r.get('status') != 'pending' or not r.get('qr_url'):
        return _login_status(r)
    return ('Scan the QR code below in the Yandex app (or open this link on the phone where the Yandex app is signed in): '
            f"{r['qr_url']}\nThe link is a login key: show it only to the user. Then call login again every few seconds.")


def _reconcile(r):
    return 'Checkout is unlocked. Start over with preview_order and get a fresh yes from the user.'


def _cancel(r):
    if r.get('outcome') == 'declined':
        return 'The user said no in the confirmation dialog. Nothing was sent; the order goes on.'
    if r.get('outcome') == 'canceled':
        return 'Order canceled. Refund timing is up to the bank.'
    return 'Cancellation sent, but not confirmed yet. Check list_order_history in a minute; do not send it again.'


RENDERERS = {
    'search_products': _search, 'add_to_cart': _add, 'repeat_order': _add, 'get_cart': _cart, 'set_cart_quantity': _cart,
    'use_address': _use_address, 'get_account': _account, 'search_addresses': _found_addresses,
    'get_product': _product_card, 'list_categories': _categories, 'get_category_products': _category,
    'list_order_history': _history, 'get_order': _order,
    'preview_order': _preview, 'submit_order': _submit, 'get_storefront_status': _status,
    'login': _login, 'reconcile_order_attempt': _reconcile, 'cancel_order': _cancel,
}


def render(tool, result):
    """Text for the model, or JSON when BETTER_LAVKA_MCP_OUTPUT=json."""
    if json_output():
        return json.dumps(result, ensure_ascii=False, indent=2)
    return RENDERERS[tool](result)
