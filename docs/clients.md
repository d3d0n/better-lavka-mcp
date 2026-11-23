# Connect an MCP client

The server speaks MCP over **stdio**: the client starts a local process and communicates through its standard input and output. Install dependencies first as described in the [README](../README.md). Use a trusted local client: it can request access to account data and consequential tools.

## Configuration

Replace `/absolute/path/to/repository` in the examples with the absolute path to your checkout. The installed `better-lavka-mcp` console script must exist in that virtualenv; the examples target Unix-like systems. Merge the server entry into any existing configuration rather than overwriting other settings. Review project-level configurations before sharing them: absolute paths are machine-specific, and MCP servers can execute local programs.

Choose the initial country by adding `"env": {"YANDEX_LAVKA_REGION": "KZ"}` to the server entry (Oh My Pi, Claude Code and Claude Desktop), or `"environment": {"YANDEX_LAVKA_REGION": "KZ"}` for OpenCode. Use exactly `UZ`, `RU`, or `KZ`; omission defaults to `UZ`, and invalid values prevent startup. After connecting, call `select_country` to change the country for this server process. A restart restores the configured startup country. RU/KZ support anonymous status and search only; UZ account, login start/status, cart and order tools are blocked when those regions are selected. `cancel_login` remains available in every region to clean up a pending UZ login. An in-flight UZ operation must finish before switching. See [country and delivery location](../README.md#country-and-delivery-location).

### Oh My Pi

Add this to `.omp/mcp.json` in the project where you run Oh My Pi, or to `~/.omp/agent/mcp.json` for the default user profile. Other profiles and configuration directory overrides use different paths; see the [Oh My Pi MCP configuration guide](https://github.com/can1357/oh-my-pi/blob/main/docs/mcp-config.md).

```json
{
  "mcpServers": {
    "lavka-uz": {
      "type": "stdio",
      "command": "/absolute/path/to/repository/.venv/bin/better-lavka-mcp"
    }
  }
}
```

After editing, use `/mcp reload` and `/mcp list` in Oh My Pi.

### OpenCode

Add this to `opencode.json` in your project, or `~/.config/opencode/opencode.json` for user-wide configuration. OpenCode uses `mcp`, a `local` type and a command array—not `mcpServers`; see the [MCP server guide](https://opencode.ai/docs/mcp-servers/) and [configuration locations](https://opencode.ai/docs/config/).

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "lavka-uz": {
      "type": "local",
      "command": [
        "/absolute/path/to/repository/.venv/bin/better-lavka-mcp"
      ],
      "enabled": true
    }
  }
}
```

### Claude Code

Add this to `.mcp.json` in the project root. Project-scoped configuration can be shared with the project, so avoid committing personal paths or secrets. For user or local scope, see the [Claude Code MCP quickstart](https://code.claude.com/docs/en/mcp-quickstart) and its scope table.

```json
{
  "mcpServers": {
    "lavka-uz": {
      "type": "stdio",
      "command": "/absolute/path/to/repository/.venv/bin/better-lavka-mcp"
    }
  }
}
```

Restart the Claude Code session after editing and use `/mcp` to inspect the connection.

### Claude Desktop

Use the following `mcpServers` entry:

```json
{
  "mcpServers": {
    "lavka-uz": {
      "command": "/absolute/path/to/repository/.venv/bin/better-lavka-mcp"
    }
  }
}
```

For Claude Desktop, open **Settings → Developer → Edit Config**, merge the entry with existing servers, and restart the client. Other clients use their own configuration locations and keys; consult their documentation.

To use a non-default session file, add `YANDEX_SESSION_FILE` with an absolute, private file path to the server entry's `env` object (Oh My Pi, Claude Code and Claude Desktop) or `environment` object (OpenCode). This controls the cookie jar, not all order state; see [authentication](auth.md).

## Verify the connection

1. Restart or reconnect the client and inspect its MCP server/tool list. A connection alone does not verify live API access.
2. Ask it to call `get_storefront_status`. This is a homepage status, not a coverage check for your address; network or upstream changes can still cause an error.
3. For account tools, request `start_login` in a private channel and complete the [QR flow](auth.md). A client may not render images; the tool also returns a link. Treat both as credentials.
4. Ask it to `use_address` with the name of a saved address ("Дом", "Work"); search, cart and ordering then need no coordinates. Without it, pass longitude first, latitude second.

If the server fails to start, check the absolute console script path, virtualenv dependencies, and the client's stderr/logs. Never paste raw logs containing QR links, cookies or account information into an issue. Client-specific rendering and live connectivity are not guaranteed by a successful local test.
