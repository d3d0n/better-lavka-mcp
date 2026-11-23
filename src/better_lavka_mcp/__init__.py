"""Unofficial Yandex Lavka Uzbekistan MCP server; import submodules explicitly."""


def main():
    """`better-lavka-mcp` runs the stdio server; `better-lavka-mcp uninstall` deletes local data."""
    import sys
    if sys.argv[1:2] == ['uninstall']:
        # Before importing the server, which creates its data folder.
        from better_lavka_mcp.uninstall import run
        sys.exit(run(sys.argv[2:]))
    from better_lavka_mcp.server import mcp
    mcp.run(transport='stdio')
