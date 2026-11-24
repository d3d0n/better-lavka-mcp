"""Local-only QR login tools; returns an image and short-lived link."""
import asyncio
import hashlib
import os
import time
from pathlib import Path


class QRLogin:
    def __init__(self, session_path, manager, verify=None, ttl=180):
        self.session = Path(session_path)
        self.manager = manager
        self.verify = verify
        self.ttl = ttl
        self.lock = asyncio.Lock()
        self.started = None
        self.original = None

    async def _verify(self, path):
        if self.verify is None:
            raise RuntimeError('No session verifier configured')
        return await self.verify(path)

    def _fingerprint(self):
        try:
            return hashlib.sha256(self.session.read_bytes()).digest()
        except FileNotFoundError:
            return None

    async def start(self, replace_existing=False):
        async with self.lock:
            if self.started is not None:
                if time.monotonic() - self.started < self.ttl:
                    return {'status': 'already_pending'}
                await self.manager.close()
                self.started = None
            original = self._fingerprint()
            if original is not None and not replace_existing:
                return {'status': 'replacement_requires_approval'}
            try:
                result = await self.manager.start()
            except Exception:
                return {'status': 'error', 'note': 'Could not start QR login.'}
            self.original = original
            self.started = time.monotonic()
            return {'status': 'pending', 'qr_image': result['qr_image'], 'qr_url': result['qr_url'], 'note': 'Private login capability: share only with the intended user.'}

    async def status(self):
        async with self.lock:
            if self.started is None:
                if not self.session.exists():
                    return {'status': 'login_required'}
                try:
                    valid = await self._verify(self.session)
                except Exception:
                    return {'status': 'verification_unavailable'}
                return {'status': 'authenticated' if valid else 'login_required'}
            if time.monotonic() - self.started >= self.ttl:
                await self.manager.close()
                self.started = None
                return {'status': 'expired'}
            try:
                result = await self.manager.poll()
                if result['status'] == 'pending':
                    return {'status': 'pending'}
                staged = self.manager.session_path
                if not await self._verify(staged):
                    return {'status': 'verification_failed'}
                if self._fingerprint() != self.original:
                    return {'status': 'session_changed', 'note': 'Existing session was not replaced.'}
                if self.original is None:
                    os.link(staged, self.session)
                    staged.unlink()
                else:
                    os.replace(staged, self.session)
                return {'status': 'authenticated'}
            except Exception:
                return {'status': 'error', 'note': 'Login or verification failed; existing session was not replaced.'}
            finally:
                if self.manager.auth is None:
                    self.started = None
                    self.manager.session_path.unlink(missing_ok=True)

    async def cancel(self):
        async with self.lock:
            await self.manager.close()
            self.manager.session_path.unlink(missing_ok=True)
            self.started = None
            return {'status': 'cancelled'}


def register_qr_tools(mcp, session_path, guard=None):
    """Register once on a trusted, local stdio server; QR file is owner-only."""
    import json
    from http.cookiejar import Cookie

    import httpx

    from better_lavka_mcp.auth.runtime import QRManager

    if guard is None:
        guard = lambda fn: fn

    session = Path(session_path).expanduser()
    session.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Per-process paths avoid clobbering another server's pending login.
    import secrets
    suffix = secrets.token_hex(12)
    manager = QRManager(session.parent / ('.qr-session-' + suffix), session.parent / ('.qr-image-' + suffix + '.png'))

    async def verify(path):
        from better_lavka_mcp.client import Lavka
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}) as http:
            data = json.loads(path.read_text())
            for item in data['cookies']:
                params = dict(item)
                params['rest'] = params.pop('_rest', {})
                http.cookies.jar.set_cookie(Cookie(**params))
            props = await Lavka(http).bootstrap()
            return props.get('pageData', {}).get('passportInfo', {}).get('status') == 'VALID'

    login = QRLogin(session, manager, verify)

    from better_lavka_mcp.render import json_output, render

    def as_text(tool, result):
        return json.dumps(result) if json_output() else render(tool, result)

    @mcp.tool(structured_output=False)
    @guard
    async def start_login(replace_existing_session: bool = False):
        """Sign in to Lavka: returns a QR code to scan in the Yandex app and a short-lived link to open on the phone instead. Both are login keys: show them only to the user. replace_existing_session=true switches accounts and needs the user's approval."""
        import base64

        from mcp.types import ImageContent, TextContent
        result = await login.start(replace_existing_session)
        image_path = result.pop('qr_image', None)
        content: list[TextContent | ImageContent] = [TextContent(type='text', text=as_text('start_login', result))]
        if image_path:
            try:
                image = Path(image_path).read_bytes()
            except OSError:
                await login.cancel()
                return [TextContent(type='text', text=as_text('start_login', {'status': 'error', 'note': 'QR image unavailable; login cancelled.'}))]
            content.append(ImageContent(type='image', data=base64.b64encode(image).decode('ascii'), mimeType='image/png'))
        return content

    @mcp.tool(structured_output=False)
    @guard
    async def get_login_status() -> str:
        """Check the QR login: pending, signed in, expired or failed. Saves the session once Lavka confirms it. Never returns credentials."""
        return as_text('get_login_status', await login.status())

    @mcp.tool(structured_output=False)
    async def cancel_login() -> str:
        """Stop a pending QR login and delete its QR image. Does not sign out an existing session."""
        return as_text('cancel_login', await login.cancel())

    return login
