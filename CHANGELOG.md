# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org); install any of them with `uvx better-lavka-mcp@<version>`.

## [Unreleased]

### Added
- Product cards show ingredients from the ingredients block and the per-order limit.
- Logo, issue forms, and this changelog.

### Removed
- `requirements.txt`: dependencies live in `pyproject.toml`.

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

[Unreleased]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.5.3...HEAD
[0.5.3]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/d3d0n/better-lavka-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/d3d0n/better-lavka-mcp/releases/tag/v0.2.0
