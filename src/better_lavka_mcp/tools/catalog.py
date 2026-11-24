"""Catalog tools: product cards and category browsing."""
from better_lavka_mcp import client
from better_lavka_mcp.aliases import resolve
from better_lavka_mcp.app import _location, tool, uz_only


@tool()
@uz_only
async def get_product(product_id: str, longitude: float | None = None, latitude: float | None = None) -> dict:
    """Product card: price, pack, stock, description, nutrition per 100 g, ingredients, shelf life."""
    async with client.create_http() as http:
        location = _location(http, longitude, latitude)
        return await client.Lavka(http).product(resolve(product_id), location)


@tool()
@uz_only
async def list_categories(longitude: float | None = None, latitude: float | None = None) -> dict:
    """The catalog: groups and categories with category_id."""
    async with client.create_http() as http:
        location = _location(http, longitude, latitude)
        return {'groups': await client.Lavka(http).categories(location)}


@tool()
@uz_only
async def get_category_products(category_id: str, subcategory: str | None = None, limit: int = 30,
                                longitude: float | None = None, latitude: float | None = None) -> dict:
    """Products of one category, by shelf; subcategory narrows to one shelf. limit 1–100."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    async with client.create_http() as http:
        location = _location(http, longitude, latitude)
        return await client.Lavka(http).category(resolve(category_id), location, subcategory, limit)
