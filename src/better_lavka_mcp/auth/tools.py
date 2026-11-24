"""Local-only QR login tools; returns an image and short-lived link."""
import asyncio
import os
import time
from pathlib import Path

from better_lavka_mcp import paths
from better_lavka_mcp.auth import qr as qr_auth


class QRLogin:
    def __init__(self, session_path, manager, verify=None, ttl=180, account_changed=None):
        self.session = Path(session_path)
        self.manager = manager
        self.verify = verify
        self.ttl = ttl
        self.account_changed = account_changed
        self.lock = asyncio.Lock()
        self.started = None
        self.original = None

    async def _verify(self, path):
        if self.verify is None:
            raise RuntimeError('No session verifier configured')
        return await self.verify(path)

    def _identity(self):
        """Account identity of the saved session; an unsafe file is refused, not read."""
        return qr_auth.session_identity(self.session)

    def _finish(self):
        if self.manager.auth is None:
            self.started = None
            self.manager.session_path.unlink(missing_ok=True)

    def _forget_previous_account(self):
        """Drop choices that belonged to the previous account; False when that failed."""
        if self.account_changed is None:
            return True
        try:
            self.account_changed()
        except Exception:
            return False
        return True

    def _unchanged_since_start(self):
        """True while the saved session is still the one this login set out to replace."""
        current = self._identity()
        if self.original is None:
            return current is None
        return qr_auth.same_account(self.original, current)

    def _promote(self):
        """Replace the saved session with the verified one, comparing under the cross-process lock.

        Compares the account identity, not the file bytes: a normal tool call saves a rotated
        service cookie without changing the account, and that must not fail a finished login."""
        staged = self.manager.session_path
        staged_identity = qr_auth.session_identity(staged)
        with paths.session_lock(self.session):
            if not self._unchanged_since_start():
                return {'status': 'session_changed', 'note': 'Existing session was not replaced.'}
            paths.prepare_parent(self.session)
            os.replace(staged, self.session)
        if not qr_auth.same_account(self.original, staged_identity):
            if not self._forget_previous_account():
                return {'status': 'authenticated',
                        'note': 'Signed in, but the remembered address and card could not be cleared; ask the user to choose them again.'}
        return {'status': 'authenticated'}

    async def start(self, replace_existing=False):
        async with self.lock:
            if self.started is not None:
                if time.monotonic() - self.started < self.ttl:
                    return {'status': 'already_pending'}
                await self.manager.close()
                self.started = None
            original = self._identity()
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
                if self._identity() is None:
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
            except Exception:
                return {'status': 'error', 'note': 'Login failed; existing session was not replaced.'}
            if result['status'] == 'pending':
                return {'status': 'pending'}
            try:
                verified = await self._verify(self.manager.session_path)
            except Exception:
                verified = None
            if verified is None:
                self._finish()
                return {'status': 'error', 'note': 'Login verification failed; existing session was not replaced.'}
            if not verified:
                self._finish()
                return {'status': 'verification_failed'}
            try:
                return self._promote()
            finally:
                self._finish()

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
    paths.prepare_parent(session)
    # Per-process paths avoid clobbering another server's pending login.
    import secrets
    suffix = secrets.token_hex(12)
    manager = QRManager(session.parent / ('.qr-session-' + suffix), session.parent / ('.qr-image-' + suffix + '.png'))

    async def verify(path):
        from better_lavka_mcp.client import Lavka
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}) as http:
            data = json.loads(paths.read_private_bytes(path, 'Saved session file'))
            for item in data['cookies']:
                params = dict(item)
                params['rest'] = params.pop('_rest', {})
                http.cookies.jar.set_cookie(Cookie(**params))
            props = await Lavka(http).bootstrap()
            return props.get('pageData', {}).get('passportInfo', {}).get('status') == 'VALID'

    def forget_previous_account():
        """The remembered address and card were chosen for the previous Yandex account."""
        from better_lavka_mcp import app as lavka_app
        lavka_app._forget_account_choices()

    login = QRLogin(session, manager, verify, account_changed=forget_previous_account)

    from better_lavka_mcp.app import ANNOTATIONS
    from better_lavka_mcp.render import json_output, render

    def as_text(tool, result):
        return json.dumps(result) if json_output() else render(tool, result)

    @mcp.tool(structured_output=False, annotations=ANNOTATIONS['start_login'])
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

    @mcp.tool(structured_output=False, annotations=ANNOTATIONS['get_login_status'])
    @guard
    async def get_login_status() -> str:
        """Check the QR login: pending, signed in, expired or failed. Saves the session once Lavka confirms it. Never returns credentials."""
        return as_text('get_login_status', await login.status())

    @mcp.tool(structured_output=False, annotations=ANNOTATIONS['cancel_login'])
    async def cancel_login() -> str:
        """Stop a pending QR login and delete its QR image. Does not sign out an existing session."""
        return as_text('cancel_login', await login.cancel())

    return login
