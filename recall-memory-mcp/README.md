# recall-memory-mcp

Cross-client shared memory for ChatGPT, Claude and Kiro over the Model Context
Protocol, backed by a single local SQLite authority.

> **Status: pre-release.**
> Nothing here should be read as a claim that installing this package makes
> every conversation sync automatically — installing an MCP server only means
> the host *can see* the tools. Whether a model calls them is decided by each
> host.

## What exists today

| Layer | Module | State |
|---|---|---|
| Request/result contracts | `models.py` | implemented + tested |
| Settings & allowlists | `settings.py` | implemented + tested |
| Log/error redaction | `redaction.py` | implemented + tested |
| Repository/embedder protocols | `repository.py` | implemented |
| Transport-free service | `service.py` | implemented + tested |
| MCP SDK v2 server, seven tools | `server.py` | implemented + tested |
| Streamable HTTP app (`/mcp`, `/health`) | `app.py` | implemented + tested |
| Bearer-token validation, scopes, revocation, grants | `auth.py`, `grants.py` | implemented + tested |
| Owner-scoped online admin and offline break-glass | `admin.py`, `adminapi.py`, `admincli.py`, `breakglass.py` | implemented + tested |
| Single-owner service lifecycle | `servicectl.py` | implemented + tested |
| CLI (`init`, `doctor`, `client-config`, `serve`, `db`, `service`, `memory`) | `cli.py` | implemented + tested |
| Host templates (ChatGPT / Claude / Kiro) | `client_configs.py`, `clients/` | implemented + tested |

## Cross-client setup

- **Kiro** is the only host here with an on-disk `mcpServers` file.
  `recall-memory-mcp client-config kiro --write <path>` installs it with a
  byte-for-byte backup and a JSON read-back. See `clients/kiro/README.md`.
- **ChatGPT** and **Claude** are configured through their own connector UI.
  `client-config chatgpt` and `client-config claude` print guidance and refuse
  `--write` instead of inventing a file format.

For a public endpoint, run remote mode with authentication configured; a
loopback URL is reachable only from this machine. Verify the endpoint with MCP
Inspector before blaming the host.

## Design invariants

- **Identity is never taken from the payload.** `owner_id`, `actor_id`,
  `grant_id` and `source_client` come from the authenticated connection; every
  request model rejects unknown fields so a host cannot smuggle them in.
- **Authorization before storage.** OAuth scope *and* exact memory scope are
  checked before the repository is touched; the repository additionally
  enforces `owner_id` + scope inside every SQL predicate.
- **Read tools are pure.** Search/get/recent never mutate content, revision,
  tier or indexes.
- **Stored memories are untrusted data.** Every result carries
  `data_trust: untrusted_memory_content` and is never wrapped as an
  instruction.
- **Errors are content-free.** No absolute paths, no tracebacks, no tokens and
  no idempotency-key existence disclosure ever reach the wire.

## Development

```bash
python -m pytest -q recall-memory-mcp/tests
```

The service layer requires no network, no MCP transport and no real database:
it depends only on the protocols in `recall_memory_mcp.repository`.

## Licence

Apache-2.0
