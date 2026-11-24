import base64

import pytest


@pytest.mark.asyncio
async def test_server_registers_qr_and_returns_image_and_opt_in_link(monkeypatch, tmp_path):
    from better_lavka_mcp import app as lavka_app
    from better_lavka_mcp.auth import runtime as qr_runtime
    class FakeManager:
        def __init__(self, session_path, image_path):
            self.session_path, self.image_path, self.auth = session_path, image_path, None
        async def start(self):
            self.auth = object()
            self.image_path.write_bytes(b'fake-png')
            return {'qr_image': str(self.image_path), 'qr_url': 'https://example.test/SECRET'}
        async def poll(self):
            return {'status': 'pending'}
        async def close(self):
            self.auth = None
            self.image_path.unlink(missing_ok=True)
    monkeypatch.setattr(qr_runtime, 'QRManager', FakeManager)
    from mcp.server.fastmcp import FastMCP

    from better_lavka_mcp.auth.tools import register_qr_tools
    assert 'login' in {t.name for t in await lavka_app.mcp.list_tools()}
    server = FastMCP('test')
    register_qr_tools(server, tmp_path / 'session')
    result = await server.call_tool('login', {})
    assert 'https://example.test/SECRET' in str(result)
    assert any(x.type == 'image' and base64.b64decode(x.data) == b'fake-png' for x in result)
    # Calling login again checks the scan instead of making a second QR code.
    again = await server.call_tool('login', {})
    assert 'Waiting for the QR scan' in str(again) and 'SECRET' not in str(again)
    cancelled = await server.call_tool('login', {'cancel': True})
    assert 'cancelled' in str(cancelled)
