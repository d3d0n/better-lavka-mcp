<!-- mcp-name: io.github.d3d0n/better-lavka-mcp -->
<h1 align="center">Better Lavka MCP 🛒</h1>

<p align="center">
  <strong>Groceries from Yandex Lavka Uzbekistan, ordered by your AI assistant.</strong>
</p>

<p align="center">
  <a href="docs/auth.md">Sign in</a> ·
  <a href="docs/checkout.md">Checkout</a> ·
  <a href="docs/clients.md">Clients</a> ·
  <a href="SECURITY.md">Security</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="LICENSE">GPL-3.0</a>
</p>

---

## What is this, really?

A local **stdio MCP server** that talks to the same undocumented web API as `lavka.yandex.uz`. Connect it to Claude, Codex, OpenCode or any MCP client, and the assistant can search the shelves, fill your cart and place a real order on your real account.

No browser automation, no cookie copy-paste: you sign in by scanning a QR code with the Yandex app. State lives in a private directory on your machine, nowhere else.

---

## Stuff you do with it

- **Say where once.** «Доставляй домой» — the assistant picks your saved address, and every search and cart call uses it from then on.
- **Ask what's on the shelf.** «Chilonzorda sut bormi?» — milk and its price at your Lavka.
- **Shopping list in one message.** «Молоко, хлеб, шесть яиц» — all of it lands in the cart in one go; whatever the store doesn't have, you hear about.
- **Same as last time.** «Повтори прошлый заказ» — everything from your last order goes back into the cart at today's prices, minus what's sold out.
- **Read the label.** «Сколько калорий в этом хлебе?» — calories, protein, fat, carbs, ingredients and shelf life from the product card.
- **Say yes.** The assistant shows address, items, total and card; you agree, the order goes out, and if the bank wants 3-D Secure you get the link.
- **Check on orders.** «Что я заказывал?», «отмени заказ».
- **Peek at Russia and Kazakhstan.** Browse the RU and KZ storefronts and search their products.

---

## Three little stories

**Sunday restock.** You type *«повтори прошлый заказ домой»*. Potatoes, chicken, cottage cheese and the rest go back in the cart; the flatbread is sold out today, and the assistant says so. It shows the total, you say «да», and breakfast is on its way.

**Recipe to cart.** You paste a plov recipe and say *«собери всё для этого на четверых»*. The assistant turns it into a list, and rice, carrots, lamb, oil and cumin go into the cart in one request. Anything out of stock comes back to you with a suggestion to swap.

**Label check.** *«Bu nonda nechta kaloriya bor?»* The assistant opens the product card and answers in Uzbek: 240 kcal per 100 g, 7.7 g protein, three days of shelf life.

---

## Works today · Not yet

| ✅ Works today | 🚧 Not yet |
|---|---|
| Sign in with a QR code from the Yandex app | Live order tracking (courier, ETA) |
| Pick a saved address once per session | Scheduled delivery slots |
| Product search, product cards with nutrition, shopping lists, cart editing | |
| Order history with items, repeat an order, order details | |
| Order with a chosen card, payment status and 3-D Secure link, cancel an order | |
| Browse the RU and KZ storefronts | Ordering in Russia and Kazakhstan |

---

## Getting started

You need [mise](https://mise.jdx.dev/getting-started.html) and a Unix-like system.

**Once:**
```sh
git clone https://github.com/d3d0n/better-lavka-mcp.git && cd better-lavka-mcp
mise trust && mise install
mise run install   # Python 3.12 + deps from uv.lock
mise run test      # offline suite, no network, no money
```

**Then plug it into your client:**
```json
{
  "mcpServers": {
    "lavka": {
      "type": "stdio",
      "command": "/absolute/path/to/better-lavka-mcp/.venv/bin/better-lavka-mcp"
    }
  }
}
```

Ask the assistant to call `start_login`, scan the QR in the Yandex app, and you're in. Per-client examples (Claude Code, Claude Desktop, OpenCode, Oh My Pi) are in [docs/clients.md](docs/clients.md).

Settings:

- `YANDEX_LAVKA_REGION` — startup country: `UZ` (default), `RU` or `KZ`. `select_country` changes it for this process only.
- `YANDEX_SESSION_FILE` — where the session cookies live.

---

## Tools

Twenty-four tools with plain verb + noun names, so the assistant picks the right one on the first try.

| Tool | What it does |
|---|---|
| `use_address` | Pick the delivery address for this session by name ("Дом", "Work") |
| `search_products` | Find products and prices |
| `get_product` | Product card: price, pack, nutrition, ingredients, shelf life |
| `add_to_cart` | Add a whole shopping list in one go |
| `repeat_order` | Put a past order back into the cart |
| `get_cart` | What's in the cart, delivery and total |
| `set_cart_quantity` | Change or remove one item |
| `preview_order` | Address, card, items and total before ordering |
| `submit_order` | Place the order |
| `get_payment_status` | Payment status and 3-D Secure link |
| `list_order_history` | Past orders with their items |
| `get_order` | One past order in full |
| `cancel_order` | Cancel an order |
| `list_active_orders` | Whether any orders are currently tracked |
| `list_saved_addresses` | Your saved addresses |
| `list_saved_cards` | Your saved cards |
| `start_login` · `get_login_status` · `cancel_login` | Sign in with a QR code |
| `search_addresses` | Find an address by text |
| `count_saved_addresses` | How many addresses are saved |
| `get_storefront_status` | Is Lavka open, delivery time |
| `select_country` | Switch between UZ, RU and KZ |
| `reconcile_order_attempt` | Unblock ordering after a dropped connection |

---

## Under the hood

```mermaid
flowchart LR
  C[MCP client] -- stdio --> S[server.py<br>tools · country gate]
  S --> A[auth/<br>QR login]
  S --> K[cart · payment]
  S --> R[region.py<br>RU/KZ anonymous]
  S --> O[orders/<br>preview · guard · submit · cancel]
  O --> DB[(SQLite<br>tokens · guard)]
  A --> F[(session file)]
  K & O & R --> L[Lavka web API]
```

Session and order state live under `~/.local/share/yandex-lavka-mcp/`.

<details>
<summary><strong>Code map</strong></summary>

`src/better_lavka_mcp/`

- `server.py` — the tools
- `auth/` — QR sign-in
- `cart.py`, `payment.py`, `region.py` — cart, saved cards, RU/KZ storefronts
- `orders/` — preview, submit, cancel, tracking
- `constants.py`, `errors.py` — shared bits

</details>

---

## What it is not

- Not official. Not affiliated with or endorsed by Yandex. The API is undocumented and may change.
- Not a payment processor. It never sees full card numbers.

---

<p align="center">
  <sub>Unofficial · runs on your machine · <a href="LICENSE">GPL-3.0-only</a></sub>
</p>
