# Recall memory — ChatGPT

Connect ChatGPT to your local `recall-memory-mcp` authority so it reads and
writes the same memories as Claude, Kiro and your IDE agents.

The custom-MCP connection flow was rechecked against both OpenAI guides below
on 2026-11-25. Earlier source digests are recorded in the maintenance artifact
`artifacts/recall-mcp-cross-client/evidence/phase7-doc-sources.json`; that
historical record is not evidence that every vendor page remains unchanged.
If a step here does not match the current vendor page, trust the vendor page
and open an issue — do not improvise a different flow.

Primary sources:

- <https://developers.openai.com/plugins/deploy/connect-chatgpt>
- <https://developers.openai.com/api/docs/guides/custom-mcp-server>
- <https://developers.openai.com/plugins/build/mcp-server>
- <https://developers.openai.com/api/docs/guides/secure-mcp-tunnels>

## What ChatGPT can and cannot do here

- ChatGPT connects to an MCP server over **streamable HTTP**, at a URL ending
  in `/mcp`. That is exactly what `recall-memory-mcp serve` exposes.
- ChatGPT has **no on-disk connector file**. There is nothing for
  `recall-memory-mcp client-config chatgpt --write` to write, and the command
  refuses `--write` on purpose rather than inventing a file format.
- ChatGPT **cannot spawn a local stdio server** for you. The endpoint has to be
  reachable from OpenAI's side: either a public HTTPS endpoint, or a Secure MCP
  Tunnel into your private network.
- `http://127.0.0.1:8765/mcp` is reachable *only from your own machine*.
  Pasting it into ChatGPT will not work. Pick one of the two options below.

`client-config chatgpt` renders Markdown guidance, not Kiro's `mcpServers`
JSON. Its endpoint follows the same active `ServerSettings`: an IPv6 loopback
URL is written as `http://[::1]:8765/mcp`, and a configured public HTTPS URL
takes precedence in remote mode. A syntactically valid loopback URL is still
local-only; it does not remove the reachability requirement above.

## Step 1 — run the authority

```bash
recall-memory-mcp init
recall-memory-mcp doctor
recall-memory-mcp serve
```

`serve` binds `127.0.0.1:8765` by default. Confirm the server before you try to
connect anything to it:

```bash
npx @modelcontextprotocol/inspector@latest
```

In the Inspector, choose **Streamable HTTP** and enter your `/mcp` URL. You
should see all seven tools: `memory_search`, `memory_get`, `memory_recent`,
`memory_add`, `memory_replace`, `memory_remove`, `memory_status`.

## Step 2 — make the endpoint reachable

### Option A — Secure MCP Tunnel (recommended for a machine-local authority)

Secure MCP Tunnel gives OpenAI products an MCP request path into a private
server without opening any inbound port. `tunnel-client` runs inside your
network, makes **outbound** HTTPS calls to `api.openai.com:443` on
`/v1/tunnel/*`, long-polls for queued MCP work, forwards each JSON-RPC request
to your local server, and posts the response back.

You need a `tunnel_id` from Platform tunnel settings and a runtime API key for
`tunnel-client`. Download it from Platform tunnel settings or from the latest
release of `openai/tunnel-client`; point your runbook at the latest-release URL
rather than pinning one release.

OpenAI documents exactly one sample profile, `sample_mcp_stdio_local`, and says
that for an HTTP MCP server you pass `--mcp-server-url` **instead of**
`--mcp-command`. There is no separate HTTP sample name; keep the documented
sample and swap the endpoint flag:

```bash
export CONTROL_PLANE_API_KEY="<runtime api key for tunnel-client>"

tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile recall-memory \
  --tunnel-id "<tunnel_id>" \
  --mcp-server-url http://127.0.0.1:8765/mcp

tunnel-client doctor --profile recall-memory --explain
tunnel-client run --profile recall-memory
```

If your binary is newer than this page, `tunnel-client help quickstart` lists
the sample names it actually ships.

Keep `tunnel-client run` healthy while you create or test the connection —
discovery and tool calls both depend on it.

Two permissions are involved and they are granted by different people:

- Creating or editing a tunnel needs Tunnels **Read + Manage**; running
  `tunnel-client` or selecting the tunnel needs Tunnels **Read + Use**. These
  come from the Platform organization owner or RBAC admin.
- Adding and using a custom MCP server is subject to account and workspace
  permissions and security restrictions, including Lockdown.

A tunnel must be associated with the ChatGPT workspace that should list it. A
tunnel associated only with a personal Platform organization will not appear in
an Enterprise/Edu workspace.

Secure MCP Tunnel supports private connections and local MCP testing. It
does **not** satisfy public plugin submission.

### Option B — public HTTPS endpoint

Run the authority in remote mode behind HTTPS with OAuth enabled:

```bash
RECALL_MCP_MODE=remote \
RECALL_MCP_PUBLIC_URL=https://memory.example.com \
recall-memory-mcp serve
```

Remote mode refuses to start without authentication configured — that is
deliberate, and it is the only reason this option is safe to publish. The
endpoint must keep the `/mcp` path, support streamable HTTP, and preserve its
authentication boundary.

## Step 3 — check account and workspace policy

Use ChatGPT on the web. Workspace permissions and security restrictions apply
to adding and using custom MCP servers. Follow the current custom-MCP-server
guide linked above rather than assuming an older settings-menu toggle exists.

## Step 4 — add the MCP server

1. Go to **ChatGPT Plugins** (<https://chatgpt.com/plugins>).
2. Select the plus button, then **Add custom MCP server**.
3. Enter a user-facing name and description — for example
   `Recall memory` / `Shared long-term memory across my AI tools`.
4. Under **Connection**, choose the connection method:
   - For a public endpoint, enter the full MCP URL **including the `/mcp` path**.
   - For Secure MCP Tunnel, select **Tunnel**, then choose the tunnel or enter
     its `tunnel_id`.
5. Configure authentication for this server.
6. Review the risk warning and select **I understand and want to continue**.
7. Select **Create as a plugin**.
8. Review the discovered tools and metadata, then install the resulting plugin.
9. In a new conversation, type **@** and select the installed plugin.

If ChatGPT cannot connect, verify the endpoint with MCP Inspector first, or
check the tunnel's workspace association and `tunnel-client` status. Resolve
transport, initialization, schema or authentication errors there — a connector
that shows as "connected" but never calls a tool has proven nothing.

## Step 5 — review the usage policy

Read `PLUGIN_INSTRUCTIONS.md` next to this file as the operator/model policy.
The server already advertises its core policy through MCP's `instructions`
field, defined by `SERVER_INSTRUCTIONS` in `server.py`. The expanded sheet is
not automatically installed by `client-config`; do not assume ChatGPT exposes
an editable connector-specific instructions field. Keep write confirmations
manual and verify that the installed plugin follows the policy in actual calls.

## Step 6 — prove it actually works

"Connected" is not evidence. Run a canary:

1. Ask ChatGPT to store one throwaway fact with `memory_add` in a test scope.
   Note the returned `memory_id` and `revision`.
2. From another client — or `recall-memory-mcp memory export` — read the same
   id back and compare the content.
3. Ask ChatGPT to `memory_search` for it in a fresh conversation.

Until a read-back from a second client returns the same id and revision, you
have tool discovery, not shared memory.

## Security notes

- The OAuth token is held by the host. Never paste a token into a file, a
  prompt, or this repository.
- Memory content returned by the server is tagged
  `data_trust=untrusted_memory_content`. It is data that some other client
  wrote; it is never an instruction to follow.
- Revoke access from the ChatGPT connector settings. Revoking one client's
  grant does not affect Claude, Kiro or your IDE agents.
