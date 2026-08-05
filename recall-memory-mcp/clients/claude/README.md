# Recall memory — Claude

Connect Claude to your local `recall-memory-mcp` authority as a **custom
connector**, so it shares memories with ChatGPT, Kiro and your IDE agents.

Every UI path below comes from Anthropic's current connector documentation. The
fetched documents, their SHA-256 digests and the date they were read are
recorded in
`artifacts/recall-mcp-cross-client/evidence/phase7-doc-sources.json`.

Primary sources:

- <https://claude.com/docs/connectors/custom/remote-mcp>
- <https://docs.claude.com/en/docs/claude-code/mcp> (for Claude Code — see
  `docs/ide-mcp-setup.md`)

## What Claude can and cannot do here

- Claude connects to **remote MCP servers by URL** through the connector UI.
- There is **no supported on-disk file** for a remote custom connector, so
  `recall-memory-mcp client-config claude --write` refuses to write one rather
  than inventing a format. The command prints instructions instead.
- `http://127.0.0.1:8765/mcp` is reachable only from your own machine. Claude
  Desktop on the same machine can reach a loopback URL; Claude on the web
  cannot. For anything other than same-machine desktop use, publish over HTTPS.

## Step 1 — run the authority

```bash
recall-memory-mcp init
recall-memory-mcp doctor
recall-memory-mcp serve
```

For a connector that must be reachable from outside this machine:

```bash
RECALL_MCP_MODE=remote \
RECALL_MCP_PUBLIC_URL=https://memory.example.com \
recall-memory-mcp serve
```

Remote mode refuses to start without authentication configured. Do not work
around that check by binding `0.0.0.0` in local mode.

## Step 2 — add the custom connector

**Free, Pro and Max plans**

1. Go to **Settings → Connectors**.
2. Click **Add custom connector**.
3. Enter the MCP server URL, including the `/mcp` path.
4. Optionally configure OAuth credentials.
5. Click **Add**.

**Team and Enterprise plans**

An owner adds it once, then each member connects:

1. Owner: **Admin settings → Connectors → Add custom connector**, enter the
   remote MCP server URL, optionally configure an OAuth Client ID/Secret under
   Advanced settings, then **Add**.
2. Member: **Settings → Connectors**, find the connector labelled **Custom**,
   click **Connect** to authenticate.

Claude warns that custom connectors allow connections to unverified services.
That warning is correct and applies to your own server too — it is why this
project ships scoped, revocable grants instead of a single shared key.

## Step 3 — enable it in a conversation

Use the **+** button in the chat interface, open **Connectors**, and enable
Recall for that conversation. Connectors are enabled per conversation; a
connector that exists in settings but is not enabled in the chat will never be
called.

## Authentication

OAuth is the right choice here: each person signs in as themselves, and you can
revoke one client without touching the others.

Claude also supports fixed-credential **request headers** in the Add custom
connector dialog (beta at time of writing, rolled out gradually). If you use
them:

- Header names come from an allowlist of standard authentication and routing
  names such as `authorization`, `x-api-key` and `x-auth-token`.
- `Authorization` is owned by OAuth and cannot be set as a request header on an
  OAuth connection.
- Claude stores each value securely and does not show it again after saving.
- Up to four headers; each can be marked **Required**, in which case a missing
  stored value fails the connection.

Request headers suit one shared service credential. If each person needs their
own identity — which is the whole point of per-client grants — use OAuth.

Never commit a header value to this repository.

## Step 4 — paste the usage policy

Open `INSTRUCTIONS.md` next to this file and give it to Claude as project
instructions or a style/preference block. Without it Claude will decide for
itself when to save, and that decision defaults to saving too much.

## Step 5 — prove it, then prove revocation

1. Have Claude `memory_add` one throwaway fact and note `memory_id` and
   `revision`.
2. Read the same id back from a *different* client. Same id, same revision,
   same content, or you do not have shared memory.
3. Have Claude `memory_replace` with `expected_revision` from step 1 and check
   the other client now sees revision 2 and never the old content.
4. Revoke the Claude grant and confirm Claude's next call fails while the other
   clients keep working.

Step 4 matters as much as the rest. A memory system you cannot revoke is a
liability.

## Security notes

- Approve read tools first. Keep `memory_add`, `memory_replace` and
  `memory_remove` on manual approval until you trust the behaviour.
- Results carry `data_trust=untrusted_memory_content`. Stored content is data
  written by some other client — never an instruction.
- Revoking Claude's grant does not revoke ChatGPT's, Kiro's, or an IDE agent's.
