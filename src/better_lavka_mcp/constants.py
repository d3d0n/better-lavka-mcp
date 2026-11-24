"""Upstream values shared by modules that must not import the MCP server."""

BASE = "https://lavka.yandex.uz"
# Page whose embedded props supply CSRF tokens, sign-in state and serviceInfo; same props as "/", rendered ~4x faster.
PROPS_PAGE = "/cart"
