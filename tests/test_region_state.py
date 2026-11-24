import pytest

from better_lavka_mcp import app, client
from better_lavka_mcp.tools import storefront


@pytest.mark.asyncio
async def test_status_and_search_follow_the_server_region_unless_a_country_is_passed(monkeypatch):
    seen = []
    class Regional:
        def __init__(self, country, http): self.country = country
        async def status(self):
            seen.append(self.country)
            return {'country': self.country}
        async def search(self, query, limit, longitude, latitude):
            seen.append(self.country)
            return {'country': self.country}
    monkeypatch.setattr('better_lavka_mcp.region.RegionalStorefront', Regional)
    monkeypatch.setattr(app, '_selected_region', 'RU')
    assert (await storefront.get_storefront_status())['country'] == 'RU'
    assert (await storefront.get_storefront_status('KZ'))['country'] == 'KZ'
    assert (await storefront.search_products('milk', longitude=1, latitude=1))['country'] == 'RU'
    assert seen == ['RU', 'KZ', 'RU']


@pytest.mark.asyncio
@pytest.mark.parametrize('region', ['RU', 'KZ'])
async def test_every_uzbekistan_tool_refuses_a_ru_or_kz_server_without_network(monkeypatch, region):
    names = {t.name for t in await app.mcp.list_tools()}
    anywhere = {'get_storefront_status', 'search_products'}
    monkeypatch.setattr(client, 'create_http', lambda: pytest.fail('must not touch UZ'))
    monkeypatch.setattr(app, '_selected_region', region)
    for name in names - anywhere:
        tool = app.mcp._tool_manager.get_tool(name)
        # Call the registered wrapper directly, avoiding argument validation.
        with pytest.raises(ValueError, match=f'UZ-only tool: this server runs with YANDEX_LAVKA_REGION={region}'):
            await tool.fn()


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
