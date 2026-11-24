# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org); install any of them with `uvx better-lavka-mcp@<version>`.

## [Unreleased]

## [0.7.1] — 2026-11-24

### Fixed
- Server instructions: the sign-in hint reads "if not signed in" again after the overview sentence.

## [0.7.0] — 2026-11-24

### Added
- `get_storefront_status` is now the overview to start with: besides open hours and sign-in it shows the chosen address, what is in the cart (items, total, whether it can be ordered) and the latest order with Lavka's own status word. A part Lavka didn't answer reads as unknown, never as an empty cart or no orders.

## [0.6.4] — 2026-11-24

### Fixed
- The remembered address is checked against the cookies of the request that uses it, not a separate read of the session file, so a login landing in between can no longer send the previous account's coordinates with the new account's cookies.

## [0.6.3] — 2026-11-24

### Fixed
- A delivery address remembers which Yandex account chose it. After any server process signs in to another account, other running processes stop using the old address, and a `use_address` that finishes after the switch is ignored. Only a hash of the login cookies is stored.

## [0.6.2] — 2026-11-24

### Added
- `submit_order` and `cancel_order` ask the user in the client's own confirmation dialog when the client supports MCP elicitation; the model can't answer it for them. A "no" sends nothing.
- Every tool carries MCP annotations (read-only, destructive, idempotent), so clients can ask before the tools that spend or cancel.
- `add_to_cart` and `repeat_order` say how many were added and how many are now in the cart, and list items they skipped.

### Fixed
- Two server processes holding different previews can no longer both send a checkout: the order lock is taken before the cart is re-read.
- A tool call made while a QR login waits for the scan no longer makes that login fail with "signed in elsewhere": a refreshed service cookie doesn't count as another account.
- Saving refreshed cookies and saving a new QR login share one cross-process lock, so an older session can't overwrite a newer login.
- Signing in to a different Yandex account forgets the previous account's remembered address and card.
- Product cards, the catalog and categories get the currency sign even when the signed-in homepage omits it.
- `preview_order` refuses when the remembered address moved in the saved list between two reads, instead of previewing another address.
- The order preview shows the city, door code, delivery instructions and courier note that the order sends.
- The payment status `cancel` reads as canceled, not as still processing.
- With no usable card, `preview_order` says to add one in the Lavka app instead of asking which card.
- The 50-per-line limit is reported under "Limited" instead of silently cutting the quantity.
- Fractional (weighed) quantities are no longer rounded to whole units when adding to the cart or repeating an order.
- `use_address("9")` with three saved addresses says there is no address 9 instead of picking the one on house 9.
- An unknown storefront state reads as unknown, not as closed.
- Searches in Russia and Kazakhstan no longer suggest tools that work only in Uzbekistan.
- Product, address and order text from Lavka is flattened and quoted, so it can't pose as the server's own instructions; the server instructions say such text is never the user's approval.

### Security
- The session file, preferences and short-id database are refused when they are symlinks or readable by other users; nothing is chmod-ed through a symlink.
- GitHub Actions are pinned to commit SHAs; the PyPI upload runs in a job without the repository checkout.

## [0.6.1] — 2026-11-24

### Fixed
- `add_to_cart` and `repeat_order` keep the cart lock across adding and capping at the store limit, so a removal made meanwhile isn't undone.
- Order previews and cancellation work on Windows: the owner-only file checks apply POSIX permissions only on macOS and Linux.

### Changed
- Type checking uses ty instead of pyright; CI also runs a Windows job.

## [0.6.0] — 2026-11-24

### Added
- Product cards show ingredients from the ingredients block and the per-order limit.
- Logo, issue forms, this changelog, and GitHub Releases created from it on every tag.
- Windows: data lives in `%LOCALAPPDATA%\better-lavka-mcp`.
- CI runs on Linux and macOS and checks the code with ruff and pyright (`mise run lint`).

### Changed
- The server code is split into `app.py`, `client.py` and `tools/` by area instead of one large module.

### Removed
- `requirements.txt`: dependencies live in `pyproject.toml`.
- An unused offline checkout prototype.

## [0.5.3] — 2026-11-24

### Fixed
- `uninstall` never deletes a directory or a symlink target outside the data folder: `YANDEX_SESSION_FILE` is removed only if it is a file (or just the link), and a symlinked data folder removes only the link.

## [0.5.2] — 2026-11-24

### Added
- `better-lavka-mcp uninstall` deletes the sign-in, remembered address and card, short ids, previews and guards, asks first (`--yes` skips), and prints how to remove the client entry and the package.

## [0.5.1] — 2026-11-24

### Fixed
- Short ids are stored in SQLite with unique alias and real id, allocated in one transaction and never reused, so several server processes agree and an old id never points to another product.
- `get_order` no longer subtracts the already-included discount a second time.
- `list_active_orders` counts tracking-feed entries instead of claiming a number of orders.

## [0.5.0] — 2026-11-24

### Added
- Short three-word ids (`loom paper grass`, ~5 tokens) instead of 44-character hex ids (~23 tokens). Stable per product; tools accept either form.

## [0.4.0] — 2026-11-24

### Changed
- Tools answer in short Markdown with what happened, the data and the next step, instead of JSON. `BETTER_LAVKA_MCP_OUTPUT=json` returns the raw data.
- Server instructions describe the usual shopping flow.

### Fixed
- `search_addresses` uses Lavka's current suggest endpoint (the old one returned 404) and returns coordinates.

## [0.3.1] — 2026-11-24

### Changed
- `add_to_cart` asks which product to take (`needs_choice`) when a query matches several products you never bought, instead of adding the first hit.
- README examples are in English.

## [0.3.0] — 2026-11-24

### Added
- The delivery address and the card you chose are remembered between sessions.
- `add_to_cart` prefers products from your past orders.
- Catalog browsing: `list_categories`, `get_category_products`.
- Cookies Lavka rotates during calls are saved back to the session file.

## [0.2.0] — 2026-11-24

First release on PyPI and in the MCP Registry.

### Added
- `use_address`: pick a saved address once; search, cart and ordering stop asking for coordinates.
- `add_to_cart` (a whole list in one update) and `repeat_order`; both drop sold-out items and cap quantities at the store limit.
- Order history with items, `get_order`, `get_product` with nutrition, `get_payment_status`; `submit_order` waits for the payment status and returns the 3-D Secure link.
- Install with `uvx better-lavka-mcp`.

### Changed
- Tools renamed to plain verb + noun names; `set_cart_quantity` no longer needs a price.
- Package renamed to `better-lavka-mcp` and moved to `src/`.

[Unreleased]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.7.1...HEAD
[0.7.1]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.6.4...v0.7.0
[0.6.4]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.6.3...v0.6.4
[0.6.3]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.6.2...v0.6.3
[0.6.2]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.5.3...v0.6.0
[0.5.3]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/d3d0n/better-lavka-mcp/releases/tag/v0.2.0
