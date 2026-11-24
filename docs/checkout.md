# Cart and checkout

> [!CAUTION]
> `set_cart_quantity`, `add_to_cart`, `repeat_order`, `submit_order` and `cancel_order` change real account state. Cart tools, `repeat_order` and `preview_order` were run against lavka.yandex.uz; `submit_order` and `cancel_order` are tested with mocks only.

## Address and cart

`use_address("Дом")` picks the delivery address by saved-address name, part of the street or index from `list_saved_addresses`; `home`/`дом`/`uy` and `work`/`работа`/`ish` are treated as the same name. If several addresses match, it returns the candidates instead of choosing. The choice is remembered in `~/.local/share/yandex-lavka-mcp/preferences.json` (address id, name, short address and map point only), so later sessions start with it. After that, `search_products`, `list_categories`, `get_category_products`, `get_cart`, `set_cart_quantity`, `add_to_cart`, `repeat_order`, `get_product` and `list_saved_cards` need no coordinates; passing `longitude, latitude` still overrides the address for one call.

`list_categories` returns the catalog groups and their categories; `get_category_products(category_id, subcategory=None, limit=30)` returns that category's shelves with counts and its products, optionally one shelf only.

`get_cart` returns items, quantities, prices, pack sizes, availability, delivery cost, ETA, total and whether the cart can be checked out. `set_cart_quantity(product_id, quantity)` sets an exact quantity; zero removes the item. Lavka requires a price on every cart line but reprices each line to the current catalog price, so the server fills it in. Every write is one request followed by a read-back that checks the requested quantities, the cart identity and that other items did not change; an ambiguous result is reported, never retried.

`add_to_cart([{"query": "молоко"}, {"product_id": "…", "quantity": 2}])` adds quantities on top of the cart in a single update. For a query it adds the product you ordered before if it is among the results, or the only match. If neither, nothing is added for that query: `needs_choice` returns up to five options with prices and pack sizes, and the assistant asks which one and calls again with its `product_id`. `repeat_order("last")` adds the products of a past order the same way. Both then remove items the store cannot sell right now, cap quantities at the store limit, and report `added`, `unavailable`, `limited` and (for `add_to_cart`) `needs_choice` and `not_found`.

## Preview and submit

1. Choose the address with `use_address`. If you have several cards, read `list_saved_cards` and pick the four-digit suffix.
2. Call `preview_order()` — or `preview_order(saved_address_index, card_last4)` to be explicit. It defaults to the remembered address and to the card passed last time (or the only saved card); with several cards and none remembered it returns them and asks for `card_last4`. Review the returned address, items, total UZS, card and delivery with the user. This is not an order. Only non-rover, on-demand delivery is supported.
3. Only after explicit approval of those exact terms, call `submit_order(order_preview_token, confirmed_total_uzs, explicit_confirmation=true)` using the token and exact total string returned by the preview. The token is single-use and expires after five minutes; preflight re-reads and compares the bound data. If anything changes or expires, obtain and confirm a new preview.
4. If the client supports MCP elicitation, `submit_order` also shows the user a confirmation dialog with the total, card, full delivery terms and items. Only a ticked "yes" places the order; "no", closing the dialog or a failed dialog sends nothing and leaves the preview unused. `cancel_order` asks the same way. Clients without elicitation rely on `explicit_confirmation` alone.

The preflight is local, not a server-side price guarantee. Each attempt is sent at most once. After an order ID comes back, `submit_order` polls the payment status for a few seconds and returns `payment_status`; when the bank asks for 3-D Secure it also returns `redirect_url` for the user to open. `get_payment_status(order_id)` checks it again later. The local durable guard is not account-wide across other applications or installations.

## Rejection and uncertainty

The server can return a redacted diagnostic projection, not a complete upstream response; even machine-like error codes can be sensitive. A recognized checkout rejection can release the guard. For a recognized `bad_service_fee_v2` or `bad_service_fee` rejection, refresh the cart and preview, compare old and new totals and available item, fee, delivery and availability changes, respect the budget, and obtain fresh explicit approval. The code alone does not prove product prices changed or explain the fee error. Never reuse a consumed token or retry automatically.

A timeout, transport error, or unrecognized response may mean the outcome is **unknown**. The guard quarantines the attempt. Check official order history and payment, confirm no request is in progress, and consider delayed processing and duplicate-order risk. `reconcile_order_attempt` requires explicit attestations of those checks and risks (`checked_order_history`, `checked_payment`, `accepts_duplicate_risk`) before releasing the local guard; it does not independently verify them or prove no order exists. Never bypass the guard by deleting or switching state files. Another application can bypass the local guard entirely.

## Orders, tracking and cancellation

`list_order_history` returns past orders newest first with their items (product id, name, quantity, price), total and short address; `limit` is at most 21, and the next page is requested with `older_than` set to the previous page's `next_older_than` (an order id, as the web app does; paging has not yet been exercised on an account with more than one page). `get_order(order_id)` adds products, delivery, service fee and discount. Contact details, receipts and account identifiers are not returned.

Every submit appends Lavka's redacted answer and each payment-status poll (seconds after submit, status, payload type) to `checkout-diagnostics.jsonl` in the data directory, owner-only; card and wallet ids, tokens, links and addresses are not kept. `list_active_orders` lists the orders Lavka still tracks, closed ones included, with Lavka's status and the text its tracking screen shows (for example "Заказ отменён — Вернём деньги на карту"); `get_order` adds the same line. An empty feed cannot rule out a delayed order.

`cancel_order(order_id, explicit_confirmation=true)` sends at most one cancellation request and quarantines further cancellation attempts. Confirm the exact order and review applicable terms first. A successful HTTP response, including an empty object, does not prove cancellation; verify the exact order in the official app. Refund status and fees are unknown. The cancellation guard has no release workflow: later cancellations require independent reconciliation and an operator-managed solution, not automatic retry or deletion of state.
