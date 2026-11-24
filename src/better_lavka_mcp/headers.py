"""Request headers for Lavka's web API, as the Lavka site sends them."""
from better_lavka_mcp.constants import BASE


def api_headers(props, base=BASE):
    """Headers the site puts on every API call: CSRF tokens from the page props, plus its locale and city."""
    env = props.get('pageEnv') or {}
    locale = env.get('locale', 'ru-RU')
    if isinstance(locale, dict):
        locale = f"{locale.get('lang', 'ru')}-{locale.get('region', 'RU')}"
    return {'Origin': base, 'Referer': base + '/', 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json',
            'X-Csrf-Token': props.get('authProxyCsrfToken') or '', 'X-Csrf-Token-Bff': props.get('csrfToken') or '',
            'X-Lavka-Web-Locale': locale, 'X-Lavka-Web-City': str(env.get('cityId', ''))}
