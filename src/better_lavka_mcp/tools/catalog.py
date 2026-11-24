"""Catalog tools: product cards and category browsing."""
from better_lavka_mcp import client
from better_lavka_mcp.aliases import resolve
from better_lavka_mcp.app import _location, tool, uz_only


@tool()
@uz_only
async def get_product(product_id: str, longitude: float | None = None, latitude: float | None = None) -> dict:
    """Product card: price, pack size, availability, description, calories and protein/fat/carbs per 100 g, shelf life, brand and country."""
    async with client.create_http() as http:
        location = _location(http, longitude, latitude)
        return await client.Lavka(http).product(resolve(product_id), location)


@tool()
@uz_only
async def list_categories(longitude: float | None = None, latitude: float | None = None) -> dict:
    """The store's catalog: groups ("Молочный прилавок", "Готовая еда"…) with their categories and category_id, for the delivery address."""
    async with client.create_http() as http:
        location = _location(http, longitude, latitude)
        return {'groups': await client.Lavka(http).categories(location)}


@tool()
@uz_only
async def get_category_products(category_id: str, subcategory: str | None = None, limit: int = 30,
                                longitude: float | None = None, latitude: float | None = None) -> dict:
    """Browse one category from list_categories: its shelves (subcategories) with counts and products with prices. Pass subcategory (e.g. "Яйца") to see one shelf. limit is 1–100."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    async with client.create_http() as http:
        location = _location(http, longitude, latitude)
        return await client.Lavka(http).category(resolve(category_id), location, subcategory, limit)
