import json
import os

import httpx
import pytest

from better_lavka_mcp.client import Lavka
from better_lavka_mcp.errors import UpstreamError


@pytest.mark.asyncio
async def test_search_uses_uz_currency_and_surfaces_csrf_failure():
    def handle(request):
        if request.url.host == "yandex.uz":
            return httpx.Response(200, headers={"set-cookie": "yandexuid=anonymous; Domain=.yandex.uz; Path=/"})
        if request.method == "GET":
            assert "yandexuid=anonymous" in request.headers.get("cookie", "")
            return httpx.Response(200, text='<script id="__page_props__-data">{ "csrfToken":"bff", "authProxyCsrfToken":"proxy", "pageEnv":{"cityId":10335,"locale":{"lang":"ru","region":"RU"}}, "pageData":{"serviceInfo":{"currency":"UZS","currencySign":"сум"}} }</script>')
        assert request.url.host == "lavka.yandex.uz"
        assert request.headers["X-Csrf-Token"] == "proxy"
        assert request.headers["X-Csrf-Token-Bff"] == "bff"
        assert json.loads(request.content)["currencySign"] == "сум"
        return httpx.Response(401, json={"data":{"message":"[Csrf] Invalid token"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = Lavka(http)
        with pytest.raises(UpstreamError, match="CSRF"):
            await client.search("молоко", 3, (65.0, 40.0))


def test_existing_cookie_jar_is_loaded_without_widening_domain(tmp_path, monkeypatch):
    import http.cookiejar

    from better_lavka_mcp import client as session
    cookie = http.cookiejar.Cookie(0, "Session_id", "secret", None, False, ".yandex.ru", True, True, "/", True, True, None, True, None, None, {}, False)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies": [cookie.__dict__]}))
    state.chmod(0o600)
    monkeypatch.setattr(session, "STATE", str(state))
    client = session.create_http()
    assert next(iter(client.cookies.jar)).domain == ".yandex.ru"
    request = client.build_request("GET", "https://lavka.yandex.uz/")
    assert "cookie" not in request.headers


@pytest.mark.asyncio
async def test_authenticated_search_uses_independent_anonymous_region_confirmation():
    def handle(request):
        if request.url.host == "yandex.uz":
            return httpx.Response(200, headers={"set-cookie": "yandexuid=anon; Domain=.yandex.uz; Path=/"})
        if request.method == "GET":
            auth = "Session_id=valid" in request.headers.get("cookie", "")
            page = {"csrfToken": "bff", "authProxyCsrfToken": "proxy", "pageData": {"passportInfo": {"status": "VALID"}, "needInitStartupRequest": False} if auth else {"serviceInfo": {"currency": "UZS", "currencySign": "сум"}}}
            return httpx.Response(200, text='<script id="__page_props__-data">' + json.dumps(page) + '</script>')
        assert "Session_id=valid" in request.headers.get("cookie", "")
        assert json.loads(request.content)["currencySign"] == "сум"
        return httpx.Response(200, json={"cacheProducts": [{"id": "x", "title": "Milk", "currentPrice": 100}]})
    transport = httpx.MockTransport(handle)
    async with httpx.AsyncClient(transport=transport) as http, httpx.AsyncClient(transport=transport) as region_http:
        http.cookies.set("Session_id", "valid", domain=".yandex.uz", path="/")
        client = Lavka(http, region_http=region_http)
        result = await client.search("milk", 3, (65.0, 40.0))
        assert result["products"][0]["currency"] == "UZS"


def _bootstrap_handle(service_info, calls):
    def handle(request):
        if request.url.host == "yandex.uz":
            return httpx.Response(200, headers={"set-cookie": "yandexuid=anon; Domain=.yandex.uz; Path=/"})
        if request.method == "GET":
            calls.append("bootstrap")
            page = {"csrfToken": "bff", "authProxyCsrfToken": "proxy",
                    "pageData": {"passportInfo": {"status": "VALID"}} if "Session_id=valid" in request.headers.get("cookie", "") else service_info}
            return httpx.Response(200, text='<script id="__page_props__-data">' + json.dumps(page) + '</script>')
        assert json.loads(request.content)["currencySign"] == "сум"
        if request.url.path == "/api/v1/providers/v1/product":
            return httpx.Response(200, json={"product": {"id": "milk", "title": "Milk", "currentPrice": 100}})
        if request.url.path == "/api/v1/providers/v1/layout":
            return httpx.Response(200, json={"sections": [{"categoryGroup": {"categoryGroupInfo": {"id": "g", "title": "Dairy"}},
                                                           "categories": [{"categoryInfo": {"id": "c", "title": "Milk", "available": True}}]}]})
        return httpx.Response(200, json={"categories": [], "products": []})
    return handle


@pytest.mark.asyncio
async def test_product_and_catalog_recover_the_currency_sign_from_an_anonymous_bootstrap():
    calls = []
    transport = httpx.MockTransport(_bootstrap_handle({"serviceInfo": {"currency": "UZS", "currencySign": "сум"}}, calls))
    async with httpx.AsyncClient(transport=transport) as http, httpx.AsyncClient(transport=transport) as region_http:
        http.cookies.set("Session_id", "valid", domain=".yandex.uz", path="/")
        lavka = Lavka(http, region_http=region_http)
        assert (await lavka.product("milk", (65.0, 40.0)))["id"] == "milk"
        assert (await lavka.categories((65.0, 40.0)))[0]["categories"][0]["category_id"] == "c"
        assert (await lavka.category("c", (65.0, 40.0)))["total"] == 0
    # One signed-in homepage plus one independent anonymous confirmation, cached across the catalog page.
    assert calls == ["bootstrap", "bootstrap"]


@pytest.mark.asyncio
async def test_catalog_refuses_when_the_currency_sign_cannot_be_confirmed():
    from better_lavka_mcp.errors import UpstreamError
    calls = []
    transport = httpx.MockTransport(_bootstrap_handle({}, calls))
    async with httpx.AsyncClient(transport=transport) as http, httpx.AsyncClient(transport=transport) as region_http:
        http.cookies.set("Session_id", "valid", domain=".yandex.uz", path="/")
        with pytest.raises(UpstreamError, match="currency"):
            await Lavka(http, region_http=region_http).product("milk", (65.0, 40.0))


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_shared_saved_session_is_refused_with_a_fix_instruction(tmp_path, monkeypatch):
    from better_lavka_mcp import client as session
    state = tmp_path / "session.json"
    state.write_text(json.dumps({"cookies": []}))
    state.chmod(0o644)
    monkeypatch.setattr(session, "STATE", str(state))
    with pytest.raises(ValueError, match="chmod 600"):
        session.create_http()


def test_symlinked_saved_session_is_refused(tmp_path, monkeypatch):
    from better_lavka_mcp import client as session
    target = tmp_path / "real.json"
    target.write_text(json.dumps({"cookies": []}))
    target.chmod(0o600)
    link = tmp_path / "session.json"
    link.symlink_to(target)
    monkeypatch.setattr(session, "STATE", str(link))
    with pytest.raises(ValueError, match="symlink"):
        session.create_http()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_custom_yandex_session_file_gets_the_same_checks(tmp_path):
    import os
    import subprocess
    import sys
    custom = tmp_path / "custom" / "session.json"
    custom.parent.mkdir()
    custom.write_text(json.dumps({"cookies": []}))
    custom.chmod(0o644)
    script = (
        "from better_lavka_mcp import client\n"
        "try:\n"
        "    client.create_http()\n"
        "except ValueError as error:\n"
        "    assert 'chmod 600' in str(error), error\n"
        "else:\n"
        "    raise AssertionError('shared custom session accepted')\n"
    )
    env = os.environ.copy()
    env.update(YANDEX_SESSION_FILE=str(custom), HOME=str(tmp_path / "home"))
    subprocess.run([sys.executable, "-c", script], env=env, check=True)

