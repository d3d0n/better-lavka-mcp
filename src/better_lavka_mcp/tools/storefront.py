"""Storefront status and product search for every supported region."""
import httpx

from better_lavka_mcp import client
from better_lavka_mcp.app import _location, selected_region, tool, uz_only


@tool()
async def get_storefront_status(country: str | None = None) -> dict:
    """Read-only: return homepage storefront info (currency, open/disabled, delivery time; UZ also reports login status) for the selected country or an explicit country. Not a quote and not proof of delivery coverage at an address."""
    country = selected_region() if country is None else country
    if country not in ('UZ', 'RU', 'KZ'):
        raise ValueError('Supported countries: RU, KZ, UZ')
    if country != 'UZ':
        from better_lavka_mcp.region import RegionalStorefront
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}) as http:
            return await RegionalStorefront(country, http).status()
    async with client.create_http() as http:
        p = await client.Lavka(http).bootstrap()
        data = p.get("pageData", {})
        service = data.get("serviceInfo", {})
        return {"currency": service.get("currency"), "currency_sign": service.get("currencySign"), "advertised_delivery_time": service.get("deliveryTimeText"), "is_open": service.get("isOpen"), "is_disabled": service.get("isDisabled"), "passport_status": data.get("passportInfo", {}).get("status"), "address_coverage_verified": False, "note": "Homepage default storefront only; not a quote for your address."}


@tool()
async def search_products(query: str, limit: int = 20, longitude: float | None = None, latitude: float | None = None, country: str | None = None) -> dict:
    """Find products and prices. In Uzbekistan it searches at the address chosen with use_address (or at longitude, latitude); RU and KZ need coordinates and search anonymously. limit is 1–50."""
    if (longitude is None) != (latitude is None):
        raise ValueError("Both longitude and latitude are required")
    country = selected_region() if country is None else country
    if country not in ('UZ', 'RU', 'KZ'):
        raise ValueError('Supported countries: RU, KZ, UZ')
    if country != 'UZ':
        if longitude is None:
            raise ValueError('Both longitude and latitude are required')
        from better_lavka_mcp.region import RegionalStorefront
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}) as http:
            return await RegionalStorefront(country, http).search(query, limit, longitude, latitude)
    location = _location(longitude, latitude)
    async with client.create_http() as http:
        return await client.Lavka(http).search(query, limit, tuple(location))


@tool()
@uz_only
async def search_addresses(query: str) -> dict:
    """Find any address by text, e.g. "Amir Temur 1": street, area and coordinates. Pass the coordinates as longitude, latitude to search or cart tools to shop there."""
    async with client.create_http() as http:
        return {"addresses": await client.Lavka(http).suggest(query)}
