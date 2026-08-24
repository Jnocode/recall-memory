# Recall memory in IDE agents — Cursor, Windsurf, VS Code, Claude Code

The three desktop hosts (ChatGPT, Claude, Kiro) are covered in
`recall-memory-mcp/clients/`. This page covers the four IDE agents.

All four speak the same MCP streamable HTTP transport to the same authority, so
a memory written in Cursor is readable in Claude Code and vice versa. What
differs is the config file name, the root JSON key, and how each tool spells
"this is an HTTP server". Those differences are not cosmetic — get one wrong
and the server is silently skipped.

Every schema below is taken from the vendor's current documentation. The fetched
documents, with SHA-256 digests and the date they were read, are recorded in
`artifacts/recall-mcp-cross-client/evidence/phase7-doc-sources.json`.

## At a glance

| Agent | Config file | Root key | How HTTP is declared |
|---|---|---|---|
| Cursor | `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global) | `mcpServers` | presence of `url` |
| Windsurf (Devin Local agent) | `.devin/mcp_config.json`, `.devin/mcp_config.local.json`, or `~/.config/devin/mcp_config.json` (`%APPDATA%\devin\mcp_config.json` on Windows) | `mcpServers` | `"transport": "http"` |
| Windsurf (legacy Cascade agent) | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` | presence of `url` |
| VS Code | `.vscode/mcp.json` (workspace) or the user-profile `mcp.json` | **`servers`** | `"type": "http"` |
| Claude Code | `.mcp.json` / `~/.claude.json`, or `claude mcp add` | `mcpServers` | `"type": "http"` |

Two traps worth stating outright:

- **VS Code uses `servers`, not `mcpServers`.** A `mcpServers` block in
  `.vscode/mcp.json` does nothing.
- **Claude Code treats a `url` entry with no `type` as a stdio server.** It
  skips the server and reports `MCP server "<name>" has a "url" but no "type";
  add "type": "http" (or "sse" / "ws") to this entry`. Before v2.1.202 the same
  mistake surfaced as the far less helpful
  `command: expected string, received undefined`. Always include
  `"type": "http"`.

## Before you start

Run the authority and confirm it serves all seven tools:

```bash
recall-memory-mcp init
recall-memory-mcp doctor
recall-memory-mcp serve
```

```bash
npx @modelcontextprotocol/inspector@latest
```

Select **Streamable HTTP**, enter `http://127.0.0.1:8765/mcp`, and check that
`memory_search`, `memory_get`, `memory_recent`, `memory_add`, `memory_replace`,
`memory_remove` and `memory_status` are all listed. If they are not there, no
IDE config will help.

`http://127.0.0.1:8765/mcp` works for agents running on this machine. For a
remote authority, use `RECALL_MCP_MODE=remote` with an HTTPS public URL and
substitute it everywhere below.

## Cursor

Project-scoped `.cursor/mcp.json`, or `~/.cursor/mcp.json` for every project:

```json
{
  "mcpServers": {
    "recall-memory": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {
        "Authorization": "Bearer ${RECALL_MCP_TOKEN}"
      }
    }
  }
}
```

Cursor supports stdio, HTTP and SSE; Recall is HTTP. For a server that uses
OAuth, Cursor can either do dynamic client registration or take static OAuth
client credentials in `mcp.json` — use static credentials only when your
provider issues a fixed client ID, and keep the value in an environment
variable rather than in the committed file.

## Windsurf

Windsurf's MCP configuration moved. New tabs default to the **Devin Local
agent**, which reads the Devin CLI config files; `~/.codeium/windsurf/mcp_config.json`
applies to the **legacy Cascade agent** only. Configure the one you actually
use, or both.

### Devin Local agent (current default)

Easiest:

```bash
devin mcp add recall-memory http://127.0.0.1:8765/mcp
devin mcp add -s project recall-memory http://127.0.0.1:8765/mcp
devin mcp add -s user recall-memory http://127.0.0.1:8765/mcp
```

A URL positional argument implies HTTP (streamable HTTP). Default scope is
`local` (`.devin/mcp_config.local.json`, gitignored); `project` writes the
shared `.devin/mcp_config.json`; `user` writes the global file.

By hand, in `.devin/mcp_config.json`:

```json
{
  "mcpServers": {
    "recall-memory": {
      "url": "http://127.0.0.1:8765/mcp",
      "transport": "http"
    }
  }
}
```

If the authority uses OAuth, authenticate once with `devin mcp login
recall-memory`. Each MCP client keeps its own OAuth session, so signing in from
Windsurf does not sign you in from Claude Code.

Useful: `devin mcp list`, `devin mcp get recall-memory`,
`devin mcp disable recall-memory`, `devin mcp logout recall-memory`.

### Legacy Cascade agent

`~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "recall-memory": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

Cascade caps the agent at 100 tools total across all servers. Recall adds seven.

## VS Code

Workspace `.vscode/mcp.json` — note the `servers` key:

```json
{
  "servers": {
    "recall-memory": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

For a user-profile config that applies to every workspace, run
**MCP: Open User Configuration** from the Command Palette. **MCP: Add Server**
walks you through it and asks whether the target is Workspace or Global.

From the command line:

```bash
code --add-mcp "{\"name\":\"recall-memory\",\"type\":\"http\",\"url\":\"http://127.0.0.1:8765/mcp\"}"
```

Do not hardcode credentials in `.vscode/mcp.json` — it is a file people commit.
Use VS Code input variables or an environment file for anything secret.

Servers defined in your user profile run locally. If you work in a remote or
Dev Container and need the server to resolve from the remote machine, define it
in workspace settings or in **MCP: Open Remote User Configuration**.

## Claude Code

```bash
claude mcp add --transport http recall-memory http://127.0.0.1:8765/mcp
```

With a bearer token:

```bash
claude mcp add --transport http recall-memory http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer ${RECALL_MCP_TOKEN}"
```

By hand, in project `.mcp.json` or `~/.claude.json`:

```json
{
  "mcpServers": {
    "recall-memory": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

`streamable-http` is accepted as an alias for `http`, so a config copied from
MCP-spec documentation works unchanged. What is *not* accepted is omitting
`type` — see the trap noted above.

## Scope binding for IDE agents

An IDE agent should read and write the exact project scope for the repository it
has open, and use `global` only for genuinely user-wide preferences. For this
repository, start the local authority with an exact allowlist:

```bash
RECALL_MCP_MEMORY_SCOPES=global,project:recall recall-memory-mcp serve
```

The server does not infer a project slug from the MCP connection: MCP
Streamable HTTP has no standard workspace field that Recall can trust for that
purpose. Every IDE tool call for this repository must pass
`scope="project:recall"`; put that rule in the host's project instructions. The
verified grant and the authority allowlist must both authorize the same exact
scope. Naming another scope in a request cannot widen either boundary and fails
closed with a uniform not-authorized error.

Do not grant `project:*`. Add another exact `project:<slug>` value to
`RECALL_MCP_MEMORY_SCOPES` only when that workspace should share this authority.
If a host has no project rule, use `global` only when the caller is authorized
for it; absence of workspace context never silently selects or creates a
project scope.

## Steering the agents

`recall-memory-mcp/clients/kiro/recall-memory-steering.example.md` is written
for Kiro but the policy is host-independent: search before planning, add only
durable facts the user asked to keep, never bulk-save a session, and require an
explicit request for replace and remove. Adapt it to:

- Cursor — `.cursor/rules/`
- Windsurf — workspace rules / `AGENTS.md`
- VS Code — `.github/copilot-instructions.md`
- Claude Code — `CLAUDE.md`

## Credentials

Every example above uses `${RECALL_MCP_TOKEN}` as a placeholder. Never paste a
real token into a config file that lives in a repository. Prefer OAuth where the
agent supports it, so each client holds its own revocable grant and revoking one
does not disturb the others.

## Verifying, honestly

A server that appears in the agent's UI has proven nothing. The only evidence
that these tools share memory is a cross-client read-back:

1. In IDE agent A, `memory_add` a throwaway fact in a test scope. Record
   `memory_id` and `revision`.
2. In agent B — a different product, not another window of the same one — call
   `memory_get` with that id. Compare content and revision.
3. In agent B, `memory_replace` with `expected_revision` from step 1.
4. Back in agent A, confirm the new revision and that the old content is gone.

Anything short of that is tool discovery.
