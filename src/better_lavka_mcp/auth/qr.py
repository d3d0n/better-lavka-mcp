"""Browser-free Yandex Passport Uzbekistan QR flow; opaque state stays local."""
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from better_lavka_mcp import paths
from better_lavka_mcp.errors import UpstreamError

PASSPORT = 'https://passport.yandex.uz'
BFF = PASSPORT + '/pwl-yandex/api/passport'

# Cookies that name the account rather than the current page session. Lavka rotates service
# cookies (lavka__session, yandexuid, ...) on ordinary calls, so those never identify an account.
LOGIN_COOKIES = ('yandex_login', 'Session_id')
LOGIN_DOMAIN = 'yandex.uz'

# A session file that cannot be parsed: it is still something, so a replacement needs approval,
# but its account cannot be compared with anything.
UNKNOWN_IDENTITY = object()


def cookie_identity(cookies):
    """Account identity of a cookie jar or saved cookie list: the Uzbekistan login cookies only.

    Accepts http.cookiejar.Cookie objects and their saved ``__dict__``/JSON form. Russian-domain
    copies and rotating service cookies are excluded: only lavka.yandex.uz credentials matter."""
    found = set()
    for cookie in cookies:
        if isinstance(cookie, dict):
            name, value, domain = cookie.get('name'), cookie.get('value'), cookie.get('domain')
        else:
            name, value, domain = getattr(cookie, 'name', None), getattr(cookie, 'value', None), getattr(cookie, 'domain', None)
        if name in LOGIN_COOKIES and isinstance(value, str) and value and isinstance(domain, str) and domain.lstrip('.').endswith(LOGIN_DOMAIN):
            found.add((name, domain, value))
    return tuple(sorted(found)) or None


def session_identity(target):
    """Account identity of the saved session; None when there is none.

    Refuses a symlinked or group/world-readable session file instead of reading it."""
    target = Path(target).expanduser()
    paths.prepare_parent(target, create=False)
    try:
        raw = paths.read_private_bytes(target, 'Saved session file')
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return UNKNOWN_IDENTITY
    cookies = data.get('cookies') if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        return UNKNOWN_IDENTITY
    return cookie_identity(cookies)


def same_account(before, after):
    """True only when both identities share a login cookie with the same value.

    yandex_login is stable between logins; Session_id is reissued by Yandex on every login, so a
    re-login of the same user also compares as different. Dropping remembered choices is safe;
    carrying the previous account's delivery address into a new account is not."""
    if not before or not after or before is UNKNOWN_IDENTITY or after is UNKNOWN_IDENTITY:
        return False
    before_map = {(name, domain): value for name, domain, value in before}
    after_map = {(name, domain): value for name, domain, value in after}
    for name in LOGIN_COOKIES:
        shared = [key for key in before_map if key[0] == name and key in after_map]
        if shared:
            return all(before_map[key] == after_map[key] for key in shared)
    return False


def write_qr_image(link, target):
    """Create a private PNG; the image is a temporary login capability."""
    import qrcode
    target = Path(target).expanduser()
    paths.prepare_parent(target)
    fd, temporary = tempfile.mkstemp(prefix='.lavka-qr-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            qrcode.make(link).save(stream, format='PNG')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_session(http, target):
    """Atomically save scoped cookies with owner-only permissions.

    Callers that replace a live session (the rotation save-back and the QR promotion) hold
    paths.session_lock around their compare and this call."""
    target = Path(target).expanduser()
    paths.prepare_parent(target)
    fd, temporary = tempfile.mkstemp(prefix='.lavka-session-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'cookies': [cookie.__dict__ for cookie in http.cookies.jar]}, stream)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class PassportQR:
    def __init__(self, http):
        self.http = http
        self._csrf = None
        self._state = None
        self._completed = False

    async def _post(self, path, **kwargs):
        response = await self.http.post(BFF + path, headers={
            'X-CSRF-Token': self._csrf,
            'Origin': PASSPORT,
            'Referer': PASSPORT + '/pwl-yandex',
        }, follow_redirects=False, **kwargs)
        if response.status_code != 200:
            raise UpstreamError(f'Passport request failed (HTTP {response.status_code})')
        try:
            data = response.json()
        except ValueError:
            raise UpstreamError('Passport returned non-JSON') from None
        if not isinstance(data, dict):
            raise UpstreamError('Unexpected Passport response')
        if data.get('errors') or data.get('error') or data.get('type') == 'captcha':
            raise UpstreamError('Passport rejected the request or requires verification')
        return data

    async def start(self):
        response = await self.http.get(PASSPORT + '/pwl-yandex', follow_redirects=False)
        if response.status_code != 200:
            raise UpstreamError(f'Passport bootstrap failed (HTTP {response.status_code})')
        match = re.search(r'__CSRF__\s*=\s*"([^\"]+)"', response.text)
        if not match:
            raise UpstreamError('Passport bootstrap CSRF missing or verification required')
        self._csrf = match.group(1)
        state = await self._post('/auth/password/submit', json={'retpath': 'https://lavka.yandex.uz/'})
        if not isinstance(state.get('track_id'), str) or not state['track_id']:
            raise UpstreamError('Passport track missing')
        result = await self._post('/auth/magic/code', data={
            'location_id': '0', 'magic_track_id': state['track_id'], 'track_id': ''})
        link = result.get('link')
        try:
            parsed = urlsplit(link)
            safe = (parsed.scheme == 'https' and parsed.hostname == 'passport.yandex.uz'
                    and parsed.username is None and parsed.password is None
                    and parsed.port in (None, 443))
        except (ValueError, TypeError):
            safe = False
        if not safe:
            raise UpstreamError('Passport returned an unsafe QR link')
        self._state = state
        return link

    async def poll(self):
        if self._state is None or self._completed:
            raise UpstreamError('No active QR session')
        result = await self._post('/auth/magic/code/status', json=self._state)
        if result.get('state') != 'otp_auth_finished':
            return False
        track = result.get('trackId')
        if not isinstance(track, str) or not track:
            raise UpstreamError('Confirmed Passport session missing track')
        await self._post('/sessions/get_session', data={'track_id': track})
        if not any(c.name == 'Session_id' and c.domain in ('yandex.uz', '.yandex.uz') and c.value for c in self.http.cookies.jar):
            raise UpstreamError('Passport did not issue an Uzbekistan session cookie')
        self._completed = True
        self._state = None
        return True
