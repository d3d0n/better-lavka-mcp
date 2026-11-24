import asyncio

import pytest

from better_lavka_mcp import app, client
from better_lavka_mcp.tools import account, storefront


@pytest.mark.asyncio
async def test_selection_changes_default_status_and_explicit_override(monkeypatch):
    seen = []
    class Regional:
        def __init__(self, country, http): self.country = country
        async def status(self):
            seen.append(self.country)
            return {'country': self.country}
    monkeypatch.setattr('better_lavka_mcp.region.RegionalStorefront', Regional)
    try:
        await app.select_country('RU')
        assert (await storefront.get_storefront_status())['country'] == 'RU'
        assert (await storefront.get_storefront_status('KZ'))['country'] == 'KZ'
        assert (await storefront.get_storefront_status())['country'] == 'RU'
    finally:
        await app.select_country('UZ')
    assert seen == ['RU', 'KZ', 'RU']


@pytest.mark.asyncio
async def test_all_uz_tools_refuse_selected_ru_and_kz(monkeypatch):
    names = {t.name for t in await app.mcp.list_tools()}
    allowed = {'select_country', 'get_storefront_status', 'search_products'}
    expected = {'use_address', 'search_addresses', 'count_saved_addresses', 'get_cart', 'set_cart_quantity', 'add_to_cart', 'repeat_order', 'get_product', 'list_categories', 'get_category_products', 'list_saved_cards', 'list_order_history', 'get_order', 'get_payment_status', 'list_active_orders', 'list_saved_addresses', 'preview_order', 'submit_order', 'reconcile_order_attempt', 'cancel_order', 'start_login', 'get_login_status', 'cancel_login'}
    assert names - allowed == expected
    monkeypatch.setattr(client, 'create_http', lambda: pytest.fail('must not touch UZ'))
    try:
        for region in ('RU', 'KZ'):
            await app.select_country(region)
            for name in expected - {'cancel_login'}:
                tool = app.mcp._tool_manager.get_tool(name)
                # Call the registered wrapper directly, avoiding argument validation.
                with pytest.raises(ValueError, match='UZ-only'):
                    await tool.fn()
    finally:
        await app.select_country('UZ')


@pytest.mark.asyncio
async def test_region_cannot_change_during_uz_operation(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    class Client:
        async def __aenter__(self):
            entered.set()
            await release.wait()
            return self
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(client, 'create_http', Client)
    task = asyncio.create_task(account.count_saved_addresses())
    await entered.wait()
    try:
        with pytest.raises(ValueError, match='in progress'):
            await app.select_country('RU')
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    assert (await app.select_country('UZ'))['country'] == 'UZ'


def test_startup_region_env_is_validated_and_process_scoped():
    import os
    import subprocess
    import sys
    code = "from better_lavka_mcp import app; print(app._selected_region)"
    def run(value):
        env = os.environ.copy()
        if value is None: env.pop('YANDEX_LAVKA_REGION', None)
        else: env['YANDEX_LAVKA_REGION'] = value
        return subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True)
    assert run(None).stdout.strip() == 'UZ'
    assert run('KZ').stdout.strip() == 'KZ'
    assert run('RU').stdout.strip() == 'RU'
    bad = run('XX')
    assert bad.returncode != 0 and 'YANDEX_LAVKA_REGION' in bad.stderr


@pytest.mark.asyncio
async def test_search_uses_selection_and_override(monkeypatch):
    class Regional:
        def __init__(self, country, http): self.country = country
        async def search(self, query, limit, longitude, latitude):
            return {'country': self.country}
    monkeypatch.setattr('better_lavka_mcp.region.RegionalStorefront', Regional)
    try:
        await app.select_country('KZ')
        assert (await storefront.search_products('milk', longitude=1, latitude=1))['country'] == 'KZ'
        assert (await storefront.search_products('milk', longitude=1, latitude=1, country='RU'))['country'] == 'RU'
        assert (await storefront.search_products('milk', longitude=1, latitude=1))['country'] == 'KZ'
    finally:
        await app.select_country('UZ')
