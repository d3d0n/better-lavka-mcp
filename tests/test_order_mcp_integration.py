import asyncio
import json

import httpx
import pytest

from better_lavka_mcp import app, client
from better_lavka_mcp.orders import submit as orders
from better_lavka_mcp.orders.cancel import CancellationBlocked
from better_lavka_mcp.orders.guard import AttemptBlocked
from better_lavka_mcp.tools import orders as order_tools


@pytest.mark.asyncio
async def test_order_tools_are_exposed():
    names = {tool.name for tool in await app.mcp.list_tools()}
    assert {'preview_order', 'submit_order', 'cancel_order', 'list_saved_addresses'} <= names

@pytest.mark.asyncio
async def test_every_tool_tells_clients_whether_it_reads_or_spends():
    tools = {tool.name: tool for tool in await app.mcp.list_tools()}
    assert all(tool.annotations is not None for tool in tools.values())
    assert tools['search_products'].annotations.readOnlyHint is True
    # preview_order may re-save repriced cart lines, so a host must not auto-approve it as a read.
    assert tools['preview_order'].annotations.readOnlyHint is False and tools['preview_order'].annotations.destructiveHint is False
    for name in ('submit_order', 'cancel_order'):
        assert tools[name].annotations.destructiveHint is True and not tools[name].annotations.readOnlyHint
        assert 'ctx' not in tools[name].inputSchema['properties']  # the dialog context is not a model argument

@pytest.mark.asyncio
async def test_mock_checkout_single_submit_and_cancel(tmp_path, monkeypatch):
    calls = []
    props = {'pageData': {'passportInfo': {'status': 'VALID'}}}
    address = {'location':[69.2,41.3], 'placeId':'p', 'country':'Uzbekistan', 'city':'Tashkent', 'street':'Test', 'house':'1', 'flat':'5', 'floor':'2', 'entrance':'1', 'doorcode':'42'}
    cart = {'cartId':'cart', 'cartVersion':1, 'orderFlowVersion':'v', 'items':[{'id':'item', 'title':'Item', 'quantity':'1', 'price':'100'}], 'availableForCheckout':True, 'totalPriceValue':'100', 'statuses':{'isRover':False}}
    def handle(request):
        path = request.url.path
        calls.append(path)
        if path.endswith('get-favorite-addresses'): return httpx.Response(200,json=[{'address':address}])
        if path.endswith('service-info'):
            # Live service-info changes its delivery texts between preview and submit; checkout must not care.
            return httpx.Response(200,json={'currency':'UZS','currencySign':'sum','depotId':'depot','deliveryTimeText':f'{len(calls)} мин','l10n':{'deliveryCost':str(len(calls))}})
        if path.endswith('cart/v1/set-payment'):
            # The site's checkout-page call: snake_case body, one-time idempotency header.
            body = json.loads(request.content)
            assert request.headers['X-Idempotency-Token'] and body['cart_version'] == cart['cartVersion']
            cart.update(paymentMethod=body['payment_method'], cartVersion=cart['cartVersion'] + 1)
            return httpx.Response(200, json={})
        if path.endswith('retrieve'): return httpx.Response(200,json=cart)
        if path.endswith('methods'): return httpx.Response(200,json={'methods':[{'type':'card','id':'private','number':'**** 1234','availability':{'available':True}}]})
        if path.endswith('submit'):
            body=json.loads(request.content); assert body['position']['flat']=='5'; assert body['use_rover'] is False; assert body['payment_method_id']=='private'
            return httpx.Response(200,json={'orderId':'o1','newbie':False,'trackPayment':{'modalPlace':'none'}})
        if path.endswith('cancel'): return httpx.Response(200,json={})
        if path.endswith('payments/v1/status'):
            assert json.loads(request.content) == {'orderId': 'o1', 'paymentType': 'card'}
            return httpx.Response(200,json={'status':'wait_user_action','payload':{'redirectUrl':'https://trust.example/3ds'}})
        raise AssertionError(path)
    async def boot(self): self.props=props; return props
    monkeypatch.setattr(client.Lavka,'bootstrap',boot)
    monkeypatch.setattr(orders,'GUARD',tmp_path/'guard')
    monkeypatch.setattr(orders,'STATE',tmp_path/'previews')
    monkeypatch.setattr(orders,'CANCEL_STATE',tmp_path/'cancel')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        http.cookies.set('Session_id','test',domain='.yandex.uz',path='/')
        preview=await orders.preview(http,0,'1234')
        assert preview['address']['flat']=='5' and preview['total_uzs']=='100'
        result=await orders.submit(http,preview['preview_token'],'100',True)
        assert result['order_id']=='o1'
        # The preview put the card on the cart once; the check before submit wrote nothing.
        assert calls.count('/api/v2/lavka/v1/cart/v1/set-payment') == 1 and cart['paymentMethod'] == {'type': 'card', 'id': 'private'}
        assert result['payment_status']=='wait_user_action' and result['redirect_url']=='https://trust.example/3ds'
        with pytest.raises(ValueError, match='consumed'): await orders.submit(http,preview['preview_token'],'100',True)
        async def history(): return {'orders':[{'order_id':'o1','status':'cancelled'}]}
        result=await orders.cancel(http,'o1',True,history)
        assert result['refund_status']=='unknown'
    assert calls.count('/api/v2/lavka/v1/orders/v2/submit')==1
    assert calls.count('/api/v1/providers/orders/v1/cancel')==1

@pytest.mark.asyncio
async def test_checkout_timeout_blocks_new_preview_across_instances(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, 'GUARD', tmp_path/'guard')
    monkeypatch.setattr(orders, 'STATE', tmp_path/'previews')
    cart = {'cartId':'c','cartVersion':1,'orderFlowVersion':'v','totalPriceValue':'10','items':[{'id':'x'}]}
    binding={'cart':cart, 'position':{'location':[69.2,41.3]}, 'card':{'type':'card','id':'id'}, 'index':0,'suffix':'1234'}
    async def snap(*args, **kwargs): return binding, {'pageData':{'passportInfo':{'status':'VALID'}}}
    monkeypatch.setattr(orders, 'snapshot', snap)
    writes=[]
    def handle(request):
        writes.append(request.url.path)
        raise httpx.ReadTimeout('secret-token and private address')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        http.cookies.set('Session_id','test',domain='.yandex.uz',path='/')
        p=await orders.preview(http,0,'1234')
        result = await orders.submit(http,p['preview_token'],'10',True)
        assert result['outcome'] == 'unknown'
        assert result['transport_error'] == 'ReadTimeout'
        assert 'secret-token' not in str(result) and 'private address' not in str(result)
        with pytest.raises(ValueError,match='quarantined'): await orders.preview(http,0,'1234')
    assert writes==['/api/v2/lavka/v1/orders/v2/submit']

@pytest.mark.asyncio
async def test_expiry_during_fresh_preflight_prevents_write(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, 'GUARD', tmp_path/'guard')
    monkeypatch.setattr(orders, 'STATE', tmp_path/'previews')
    now=[1000.0]
    monkeypatch.setattr(orders.time,'time',lambda: now[0])
    binding={'cart':{'cartId':'c','cartVersion':1,'orderFlowVersion':'v','totalPriceValue':'10','items':[{'id':'x'}]},'position':{'location':[69.2,41.3]},'card':{'type':'card','id':'id'},'index':0,'suffix':'1234'}
    async def snap(*args, **kwargs):
        now[0]+=200
        return binding, {'pageData':{'passportInfo':{'status':'VALID'}}}
    monkeypatch.setattr(orders,'snapshot',snap)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: pytest.fail('expired preview wrote'))) as http:
        http.cookies.set('Session_id','test',domain='.yandex.uz',path='/')
        p=await orders.preview(http,0,'1234')
        now[0]+=150
        with pytest.raises(ValueError,match='expired'): await orders.submit(http,p['preview_token'],'10',True)
        # Preflight failed before any write, so the guard must be free for a new preview.
        assert (await orders.preview(http,0,'1234'))['preview_token']

@pytest.mark.asyncio
async def test_changed_data_during_preflight_releases_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, 'GUARD', tmp_path/'guard')
    monkeypatch.setattr(orders, 'STATE', tmp_path/'previews')
    old = {'cart': {'cartId':'c','cartVersion':1,'orderFlowVersion':'v','totalPriceValue':'10','items':[{'id':'x'}]},
           'position': {'location':[69.2,41.3]}, 'card': {'type':'card','id':'id'}, 'index':0, 'suffix':'1234'}
    new = {**old, 'cart': {**old['cart'], 'cartVersion': 2}}
    bindings = [old, new]
    async def snap(*args, **kwargs):
        return bindings.pop(0), {}
    monkeypatch.setattr(orders, 'snapshot', snap)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: pytest.fail('write attempted'))) as http:
        http.cookies.set('Session_id','test',domain='.yandex.uz',path='/')
        p = await orders.preview(http,0,'1234')
        with pytest.raises(ValueError, match='changed'): await orders.submit(http,p['preview_token'],'10',True)
        bindings.append(old)
        assert (await orders.preview(http,0,'1234'))['preview_token']


@pytest.mark.asyncio
async def test_two_valid_previews_cannot_both_submit(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, 'GUARD', tmp_path/'guard')
    monkeypatch.setattr(orders, 'STATE', tmp_path/'previews')
    binding = {'cart': {'cartId':'c','cartVersion':1,'orderFlowVersion':'v','totalPriceValue':'10','items':[{'id':'x'}]},
               'position': {'location':[69.2,41.3]}, 'card': {'type':'card','id':'id'}, 'index':0, 'suffix':'1234'}
    calls, paused, release = [], asyncio.Event(), asyncio.Event()
    async def snap(*args, **kwargs):
        calls.append(1)
        if len(calls) <= 2:
            return binding, {}
        paused.set()
        await release.wait()
        return binding, {}
    posts = []
    def handle(request):
        if request.url.path.endswith('/submit'):
            posts.append(request.url.path)
            return httpx.Response(200, json={'orderId':'one'})
        if request.url.path.endswith('payments/v1/status'):
            return httpx.Response(200, json={'status':'success'})
        raise AssertionError(request.url.path)
    monkeypatch.setattr(orders, 'snapshot', snap)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        http.cookies.set('Session_id','test',domain='.yandex.uz',path='/')
        first = await orders.preview(http, 0, '1234')
        second = await orders.preview(http, 0, '1234')
        sending = asyncio.create_task(orders.submit(http, first['preview_token'], '10', True))
        await paused.wait()
        refused = False
        try:
            await orders.submit(http, second['preview_token'], '10', True)
        except AttemptBlocked:
            refused = True
        finally:
            release.set()
        result = await sending
    assert refused and result['outcome'] == 'submitted'
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_preview_refuses_address_that_moved_between_fetches(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, 'STATE', tmp_path/'previews')
    monkeypatch.setattr(orders, 'GUARD', tmp_path/'guard')
    home = {'addressId':'a-home','address':{'label':'Home','location':[69.2,41.3]}}
    work = {'addressId':'a-work','address':{'label':'Work','location':[69.3,41.2]}}
    lists = [[home, work], [work, home]]
    async def addresses(http):
        return None, lists.pop(0)
    monkeypatch.setattr(orders, 'addresses', addresses)
    monkeypatch.setattr(app, '_delivery', {'address_id':'a-home','location':[69.2,41.3],'index':0,'label':'Home'})
    monkeypatch.setattr(client, 'create_http', lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail('network not expected'))))
    with pytest.raises(ValueError, match='moved'):
        await order_tools.preview_order(card_last4='1234')


@pytest.mark.asyncio
async def test_cancel_timeout_never_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, 'CANCEL_STATE', tmp_path/'cancel')
    async def boot(self):
        self.props={'pageData':{'passportInfo':{'status':'VALID'}}}; return self.props
    monkeypatch.setattr(client.Lavka,'bootstrap',boot)
    calls=[]
    def handle(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout('ambiguous')
    async def history(): raise RuntimeError('history unavailable')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        assert (await orders.cancel(http,'o1',True,history))['outcome']=='unknown'
        with pytest.raises(CancellationBlocked, match='already attempted'): await orders.cancel(http,'o1',True,history)
    assert calls==['/api/v1/providers/orders/v1/cancel']

@pytest.mark.asyncio
async def test_preview_bound_to_unchanged_authenticated_session(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, 'GUARD', tmp_path/'guard')
    monkeypatch.setattr(orders, 'STATE', tmp_path/'previews')
    binding={'cart':{'cartId':'c','cartVersion':1,'orderFlowVersion':'v','totalPriceValue':'10','items':[{'id':'x'}]},'position':{'location':[69.2,41.3]},'card':{'type':'card','id':'id'},'index':0,'suffix':'1234'}
    async def snap(*args, **kwargs): return binding, {'pageData':{'passportInfo':{'status':'VALID'}}}
    monkeypatch.setattr(orders,'snapshot',snap)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: pytest.fail('write attempted'))) as http:
        http.cookies.set('Session_id','first',domain='.yandex.uz',path='/')
        p=await orders.preview(http,0,'1234')
        http.cookies.set('Session_id','different',domain='.yandex.uz',path='/')
        with pytest.raises(ValueError,match='session'): await orders.submit(http,p['preview_token'],'10',True)


@pytest.mark.asyncio
async def test_success_allows_fresh_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "GUARD", tmp_path / "guard")
    monkeypatch.setattr(orders, "STATE", tmp_path / "previews")
    binding = {"cart":{"cartId":"c","cartVersion":1,"orderFlowVersion":"v","totalPriceValue":"10","items":[{"id":"x"}]},"position":{"location":[69.2,41.3]},"card":{"type":"card","id":"id"},"index":0,"suffix":"1234"}
    async def snap(*args, **kwargs): return binding, {}
    monkeypatch.setattr(orders, "snapshot", snap)
    def handle(request):
        if request.url.path.endswith("payments/v1/status"):
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(200, json={"orderId": "one"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        http.cookies.set("Session_id","test",domain=".yandex.uz",path="/")
        p = await orders.preview(http, 0, "1234")
        result = await orders.submit(http,p["preview_token"],"10",True)
        assert result["outcome"] == "submitted" and result["payment_status"] == "success"
        assert (await orders.preview(http, 0, "1234"))["preview_token"]


@pytest.mark.asyncio
async def test_fields_lavka_changes_on_every_read_do_not_block_the_order(tmp_path, monkeypatch):
    # Live carts carry per-call ETA stamps and a fresh one-time submit token on every read.
    monkeypatch.setattr(orders, "GUARD", tmp_path / "guard")
    monkeypatch.setattr(orders, "STATE", tmp_path / "previews")
    reads = iter(range(1, 10))
    def cart(total="10"):
        n = next(reads)
        return {"cartId": "c", "cartVersion": 1, "orderFlowVersion": "v", "totalPriceValue": total,
                "items": [{"id": "x", "quantity": "1", "maxAmount": str(20 - n), "quantityLimit": str(20 - n)}],
                "nextIdempotencyToken": f"token-{n}",
                "deliveryConditions": [{"templateVars": {"minEtaTimestamp": f"12:{n:02}", "maxEtaTimestamp": f"13:{n:02}"}}]}
    totals = iter(["10", "10", "10", "11"])
    async def snap(*args, **kwargs):
        return {"cart": cart(next(totals)), "position": {"location": [69.2, 41.3]}, "card": {"type": "card", "id": "id"},
                "index": 0, "suffix": "1234"}, {}
    monkeypatch.setattr(orders, "snapshot", snap)
    posts = []
    def handle(request):
        if request.url.path.endswith("payments/v1/status"):
            return httpx.Response(200, json={"status": "success"})
        posts.append(request.headers.get("X-Idempotency-Token"))
        return httpx.Response(200, json={"orderId": "one"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        http.cookies.set("Session_id", "test", domain=".yandex.uz", path="/")
        p = await orders.preview(http, 0, "1234")
        result = await orders.submit(http, p["preview_token"], "10", True)
        assert result["outcome"] == "submitted"
        # The token of the cart read right before sending, as the Lavka site does.
        assert posts == ["token-2"]
        # A real change still stops the order.
        p = await orders.preview(http, 0, "1234")
        with pytest.raises(ValueError, match="changed"):
            await orders.submit(http, p["preview_token"], "10", True)
        assert len(posts) == 1


@pytest.mark.asyncio
async def test_submit_request_matches_the_lavka_site(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "GUARD", tmp_path / "guard")
    monkeypatch.setattr(orders, "STATE", tmp_path / "previews")
    cart = {"cartId": "c", "cartVersion": 1, "orderFlowVersion": "v", "totalPriceValue": "10", "items": [{"id": "x"}],
            "nextIdempotencyToken": "once", "cashback": {"flow": "gain", "walletId": "w/1", "availableForPayment": 17960},
            "paidServices": {"serviceFeeV2": {"amount": "3990"}, "bagsSample": {"amount": "1950"}}}
    binding = {"cart": cart, "position": {"location": [69.2, 41.3]}, "card": {"type": "card", "id": "id"}, "index": 0, "suffix": "1234"}
    async def snap(*args, **kwargs): return binding, {"authProxyCsrfToken": "a", "csrfToken": "b", "pageEnv": {"cityId": 10335, "locale": {"lang": "ru", "region": "UZ"}}}
    monkeypatch.setattr(orders, "snapshot", snap)
    sent, sent_paths = [], []
    def handle(request):
        if request.url.path.endswith("payments/v1/status"):
            return httpx.Response(200, json={"status": "success"})
        sent.append((request.headers, json.loads(request.content)))
        sent_paths.append(request.url.path)
        return httpx.Response(200, json={"orderId": "one", "trackPayment": {"modalPlace": "none"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        http.cookies.set("Session_id", "test", domain=".yandex.uz", path="/")
        p = await orders.preview(http, 0, "1234")
        result = await orders.submit(http, p["preview_token"], "10", True)
    headers, body = sent[0]
    assert headers["X-Idempotency-Token"] == "once" and headers["X-Csrf-Token"] == "a"
    assert headers["X-Lavka-Web-City"] == "10335" and headers["X-Lavka-Web-Locale"] == "ru-UZ"
    assert body["depot_order_context"] == {"position": [69.2, 41.3], "is_supermarket": False}
    # Earns cashback like the site does, never spends it.
    assert body["cashback"] == {"wallet_id": "w/1"} and body["service_fee_v2"] == "3990" and "service_fee" not in body
    # The site's own checkout path, body format and platform headers (captured from its "Оплатить" button).
    assert sent_paths == ["/api/v2/lavka/v1/orders/v2/submit"] and headers["X-Platform"] == "lavka_uzb_web"
    assert headers["X-Grocery-Trusted-User"] == "false" and headers["X-Captcha-Service"] == "lavka"
    assert result["lavka_response"] == {"fields": ["orderId", "trackPayment"], "track_payment": {"modalPlace": "none"}}
    # A local record of Lavka's answer and every payment poll, without ids, tokens or links.
    from better_lavka_mcp.orders import diagnostics
    entry = json.loads(diagnostics.PATH.read_text().splitlines()[-1])
    assert entry["submit_response"] == {"orderId": "str", "trackPayment": {"modalPlace": "none"}}
    assert [(p["status"], p["redirect"]) for p in entry["payment_trace"]] == [("success", False)]
    assert "cashback" in entry["request_fields"] and "w/1" not in diagnostics.PATH.read_text()


async def _submit_through_mcp(tmp_path, monkeypatch, answer, *, ask=True):
    """submit_order over a real MCP session whose client answers the confirmation dialog with `answer`."""
    from mcp.shared.memory import create_connected_server_and_client_session
    monkeypatch.setattr(orders, "GUARD", tmp_path / "guard")
    monkeypatch.setattr(orders, "STATE", tmp_path / "previews")
    binding = {"cart": {"cartId": "c", "cartVersion": 1, "orderFlowVersion": "v", "totalPriceValue": "87630",
                        "items": [{"id": "x", "title": "Творог", "quantity": "2"}]},
               "position": {"location": [69.2, 41.3], "city": "Ташкент", "street": "Example street", "house": "1", "leftAtDoor": True},
               "card": {"type": "card", "id": "id"}, "index": 0, "suffix": "1234"}
    async def snap(*args, **kwargs): return binding, {}
    monkeypatch.setattr(orders, "snapshot", snap)
    posts, questions = [], []
    def handle(request):
        if request.url.path.endswith("payments/v1/status"):
            return httpx.Response(200, json={"status": "success"})
        posts.append(request.url.path)
        return httpx.Response(200, json={"orderId": "one"})
    def http():
        session = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        session.cookies.set("Session_id", "test", domain=".yandex.uz", path="/")
        return session
    monkeypatch.setattr(client, "create_http", http)
    async def dialog(context, params):
        questions.append(params.message)
        return answer
    async with create_connected_server_and_client_session(app.mcp, elicitation_callback=dialog if ask else None) as mcp_client:
        preview = await mcp_client.call_tool("preview_order", {"saved_address_index": 0, "card_last4": "1234"})
        # The model only ever sees the total; the preview itself stays with this MCP connection.
        assert 'submit_order(confirmed_total_uzs="87630", explicit_confirmation=true)' in preview.content[0].text
        assert "token" not in preview.content[0].text.lower()
        result = await mcp_client.call_tool("submit_order", {"confirmed_total_uzs": "87630", "explicit_confirmation": True})
    return result.content[0].text, posts, questions


@pytest.mark.asyncio
async def test_user_saying_no_in_the_client_dialog_sends_nothing(tmp_path, monkeypatch):
    from mcp.types import ElicitResult
    text, posts, questions = await _submit_through_mcp(tmp_path, monkeypatch, ElicitResult(action="decline"))
    assert posts == [] and 'said no' in text
    # The person sees what they approve: total, card, full address and delivery instructions, items.
    assert questions and '87 630 сум' in questions[0] and '**** 1234' in questions[0]
    assert 'Ташкент, Example street, 1' in questions[0] and 'leave at the door' in questions[0] and 'Творог ×2' in questions[0]


@pytest.mark.asyncio
async def test_user_saying_yes_in_the_client_dialog_places_the_order(tmp_path, monkeypatch):
    from mcp.types import ElicitResult
    text, posts, _ = await _submit_through_mcp(tmp_path, monkeypatch, ElicitResult(action="accept", content={"approve": True}))
    assert posts == ["/api/v2/lavka/v1/orders/v2/submit"] and 'Order placed' in text
    # Accepting the dialog without ticking the box is not a yes.
    text, posts, _ = await _submit_through_mcp(tmp_path, monkeypatch, ElicitResult(action="accept", content={"approve": False}))
    assert posts == [] and 'said no' in text


@pytest.mark.asyncio
async def test_clients_without_dialogs_still_order_on_explicit_confirmation(tmp_path, monkeypatch):
    text, posts, questions = await _submit_through_mcp(tmp_path, monkeypatch, None, ask=False)
    assert questions == [] and posts == ["/api/v2/lavka/v1/orders/v2/submit"] and 'Order placed' in text


@pytest.mark.asyncio
async def test_rejection_exposes_safe_codes_and_releases(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "GUARD", tmp_path / "guard")
    monkeypatch.setattr(orders, "STATE", tmp_path / "previews")
    binding = {"cart":{"cartId":"c","cartVersion":1,"orderFlowVersion":"v","totalPriceValue":"10","items":[{"id":"x"}]},"position":{"location":[69.2,41.3]},"card":{"type":"card","id":"id"},"index":0,"suffix":"1234"}
    async def snap(*args, **kwargs): return binding, {}
    monkeypatch.setattr(orders, "snapshot", snap)
    body = {"code":"grocery_unavailable_for_checkout", "detail":"bad_service_fee_v2", "secret":"private"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(400,json=body))) as http:
        http.cookies.set("Session_id","test",domain=".yandex.uz",path="/")
        p = await orders.preview(http,0,"1234")
        result = await orders.submit(http,p["preview_token"],"10",True)
        assert result["outcome"] == "rejected"
        assert set(result["codes"]) == {"grocery_unavailable_for_checkout", "bad_service_fee_v2"}
        assert "private" not in str(result)
        assert (await orders.preview(http,0,"1234"))["preview_token"]

def test_unknown_reconciliation_requires_acknowledgement_and_audit(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(orders, "GUARD", tmp_path / "guard")
    old = orders._reserve()
    args = dict(history_checked=True, payment_checked=True, acknowledge_uncertainty=True, no_request_in_progress=True, explicit_confirmation=True)
    with pytest.raises(ValueError): orders.reconcile_checkout(**(args | {"payment_checked":False}))
    assert orders.reconcile_checkout(**args)["released"]
    new = orders._reserve()
    orders._finish(old, "submitted", "late response")
    with pytest.raises(ValueError, match="quarantined"): orders._check_guard()
    with sqlite3.connect(orders.GUARD) as db:
        assert db.execute("select count(*) from mcp_attempt_audit where event='user_reconciled'").fetchone()[0] == 1
    orders._finish(new, "submitted", "test")

@pytest.mark.asyncio
async def test_reconciliation_tool_exposed():
    assert "reconcile_order_attempt" in {tool.name for tool in await app.mcp.list_tools()}


def test_prior_known_rejection_released_without_bank_claim(tmp_path, monkeypatch):
    import sqlite3

    from better_lavka_mcp.orders.guard import AttemptGuard
    monkeypatch.setattr(orders, "GUARD", tmp_path / "guard")
    guard = AttemptGuard(orders.GUARD)
    guard.reserve()
    guard.record_rejection()
    orders._check_guard()
    with sqlite3.connect(orders.GUARD) as db:
        assert db.execute("select count(*) from attempts").fetchone()[0] == 0
        assert db.execute("select detail from mcp_attempt_audit where event='rejected_release'").fetchone()[0] == "Previously recorded upstream rejection"

@pytest.mark.asyncio
async def test_unclassified_http_failure_stays_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "GUARD", tmp_path / "guard")
    monkeypatch.setattr(orders, "STATE", tmp_path / "previews")
    binding = {"cart":{"cartId":"c","cartVersion":1,"orderFlowVersion":"v","totalPriceValue":"10","items":[{"id":"x"}]},"position":{"location":[69.2,41.3]},"card":{"type":"card","id":"id"},"index":0,"suffix":"1234"}
    async def snap(*args, **kwargs): return binding, {}
    monkeypatch.setattr(orders, "snapshot", snap)
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(500, text="private error")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        http.cookies.set("Session_id","test",domain=".yandex.uz",path="/")
        p=await orders.preview(http,0,"1234")
        result=await orders.submit(http,p["preview_token"],"10",True)
        assert result["outcome"] == "unknown" and "cannot be determined" in result["note"]
        assert "private error" not in str(result)
        with pytest.raises(ValueError, match="quarantined"): await orders.preview(http,0,"1234")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_submit_places_only_this_connections_preview(tmp_path, monkeypatch):
    """Two MCP connections never place each other's preview."""
    from mcp.shared.memory import create_connected_server_and_client_session
    monkeypatch.setattr(orders, "GUARD", tmp_path / "guard")
    monkeypatch.setattr(orders, "STATE", tmp_path / "previews")
    binding = {"cart": {"cartId": "c", "cartVersion": 1, "orderFlowVersion": "v", "totalPriceValue": "10", "items": [{"id": "x"}]},
               "position": {"location": [69.2, 41.3]}, "card": {"type": "card", "id": "id"}, "index": 0, "suffix": "1234"}
    async def snap(*args, **kwargs): return binding, {}
    monkeypatch.setattr(orders, "snapshot", snap)
    posts = []
    def handle(request):
        if request.url.path.endswith("payments/v1/status"):
            return httpx.Response(200, json={"status": "success"})
        posts.append(request.url.path)
        return httpx.Response(200, json={"orderId": "one"})
    def http():
        session = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        session.cookies.set("Session_id", "test", domain=".yandex.uz", path="/")
        return session
    monkeypatch.setattr(client, "create_http", http)
    args = {"confirmed_total_uzs": "10", "explicit_confirmation": True}
    async with create_connected_server_and_client_session(app.mcp) as chat_a:
        await chat_a.call_tool("preview_order", {"saved_address_index": 0, "card_last4": "1234"})
        async with create_connected_server_and_client_session(app.mcp) as chat_b:
            other = await chat_b.call_tool("submit_order", args)
            assert other.isError and "No order preview on this connection" in other.content[0].text and posts == []
        placed = await chat_a.call_tool("submit_order", args)
        assert "Order placed" in placed.content[0].text and len(posts) == 1
        again = await chat_a.call_tool("submit_order", args)  # the preview is used up
        assert again.isError and len(posts) == 1
        # A newer preview that fails (here: no card chosen among two) drops the older one.
        await chat_a.call_tool("preview_order", {"saved_address_index": 0, "card_last4": "1234"})
        async def two_cards(*args, **kwargs):
            raise ValueError("Card suffix is missing or ambiguous")
        monkeypatch.setattr(orders, "snapshot", two_cards)
        failed = await chat_a.call_tool("preview_order", {"saved_address_index": 0, "card_last4": "9999"})
        assert failed.isError
        stale = await chat_a.call_tool("submit_order", args)
        assert stale.isError and "No order preview on this connection" in stale.content[0].text and len(posts) == 1
