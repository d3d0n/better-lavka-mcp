"""Request headers for Lavka's web API, as the Lavka site sends them."""
from better_lavka_mcp.constants import BASE

# A current desktop Chrome, as the site's own visitors send; a bare "Mozilla/5.0" stands out.
USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/150.0.0.0 Safari/537.36')
# The client hints that Chrome sends with that user agent. Yandex also derives its `bh` cookie from them.
CLIENT_HINTS = {'sec-ch-ua': '" Not A;Brand";v="99", "Chromium";v="150", "Google Chrome";v="150"',
                'sec-ch-ua-mobile': '?0', 'sec-ch-ua-platform': '"macOS"', 'Accept-Language': 'ru'}
# Sent by the Uzbekistan site on every API call (captured from its checkout page).
UZ_SITE_HEADERS = {'X-Platform': 'lavka_uzb_web', 'X-Grocery-Trusted-User': 'false',
                   'X-Captcha-Service': 'lavka', 'X-Captcha-Language': 'ru'}


def api_headers(props, base=BASE):
    """Headers the site puts on every API call: CSRF tokens from the page props, its locale and city, and its platform."""
    env = props.get('pageEnv') or {}
    locale = env.get('locale', 'ru-RU')
    if isinstance(locale, dict):
        locale = f"{locale.get('lang', 'ru')}-{locale.get('region', 'RU')}"
    headers = {'Origin': base, 'Referer': base + '/', 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json',
               'X-Csrf-Token': props.get('authProxyCsrfToken') or '', 'X-Csrf-Token-Bff': props.get('csrfToken') or '',
               'X-Lavka-Web-Locale': locale, 'X-Lavka-Web-City': str(env.get('cityId', ''))}
    if base == BASE:
        headers.update(UZ_SITE_HEADERS)
    return headers
