# MCP Server Catalog (Design Reference)

Configuration reference for connecting MCP servers to Jack. Adding a server is
editing `~/.autobot/mcp/servers.json` — never Python code. See
`docs/plans/mcp-integration-design.md` §5 for the full field spec.

## Slack (stdio, bot token)

The `@modelcontextprotocol/server-slack` package runs locally over stdio. It
calls the Slack API using a bot token, so it is a **network-egress** server:
every tool call sends data to Slack. This is the disclosed exception — the
server is opt-in, enabled explicitly, and labelled with a ↗ badge in the UI.

### `servers.json` entry

```jsonc
{
  "servers": {
    "slack": {
      "label": "Slack",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-slack"],
      "env": { "SLACK_TEAM_ID": "T0123456" },
      "auth": { "type": "token" },
      "token_env": "SLACK_BOT_TOKEN",
      "secret_ref": "mcp.slack.token",
      "enabled": false,
      "egress": "network",
      "default_risk": "write",
      "tool_allow": ["slack_*"],
      "tool_risk_overrides": {
        "slack_send_message": "write",
        "slack_schedule_message": "write"
      }
    }
  }
}
```

**Fields:**
- `token_env` — the env-var name the MCP server reads for its bot token.
- `secret_ref` — the Keychain account name where Jack stores the token (never in `servers.json`).
- `egress: "network"` — marks every tool with `network=True`, triggering ↗ badges and gate confirms for writes.
- `enabled: false` — off by default; enable via the Settings view or by setting to `true`.

### Storing the bot token

Store the token in the macOS Keychain once (never on disk):

```bash
# Using autobot's secret helper (after `make run` brings the daemon up):
curl -s -X POST http://127.0.0.1:8765/secret \
  -H "Content-Type: application/json" \
  -d '{"name": "mcp.slack.token", "value": "xoxb-your-real-token"}'

# Or directly via the security CLI:
security add-generic-password -U -s autobot -a mcp.slack.token -w "xoxb-your-real-token"
```

### Manual smoke-test steps

These steps verify the full path: Keychain → token injection → subprocess env → Slack API.

1. **Prerequisites:** Node.js ≥ 18 installed (`node --version`); a Slack bot token with `channels:read`, `chat:write`, `search:read` scopes; `allow_mcp: true` in `~/.autobot/settings.json`.
2. **Write the token** to the Keychain using the `POST /secret` command above.
3. **Enable the server:** set `"enabled": true` in `~/.autobot/mcp/servers.json` and replace `T0123456` with your real workspace Team ID.
4. **Launch Jack:** `make run`. The `[mcp]` log line `mcp connected server=slack tools=N` confirms the connection.
5. **List channels** (read-only, no card): say or type "list my Slack channels". Expect a channel list; check `~/.autobot/logs/autobot.log` for `[mcp]` call logs.
6. **Send a test message** (write, confirm card): say "send 'hello from Jack' to #test-channel". A network confirm card should appear ("Sends data to Slack"); approve it. Verify the message appears in Slack.
7. **Revoke and re-test:** `security delete-generic-password -s autobot -a mcp.slack.token`. Restart Jack. Any Slack tool call should return a failed `ToolResult` (token missing → subprocess auth error), NOT a crash.

### Notes

- The `npx -y` invocation downloads and caches the package on first run; subsequent starts are fast.
- To use the remote Slack-hosted MCP server (OAuth 2.1) instead of the local stdio server, set `"transport": "http"`, `"url": "https://mcp.slack.com/..."`, and `"auth": {"type": "oauth2"}`. The OAuth flow is implemented in Phase 6.
- The `SLACK_TEAM_ID` env var is optional but recommended — it scopes searches to your workspace.
