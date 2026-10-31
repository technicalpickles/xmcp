# OAuth2 read-only mode + Docker (fork additions)

This fork adds an **OAuth2 user-context** auth path and a minimal Docker setup on
top of upstream `xdevplatform/xmcp`. Upstream only does OAuth1 user-context
signing, which **cannot read the bookmarks endpoints** (they require an OAuth2
token with `bookmark.read`). It also re-runs an interactive browser consent on
every startup. This fork fixes both for a headless, restartable, localhost-only
server.

## What changed

- **`xauth.py`** — OAuth2 token store (rotating refresh token), an `httpx.Auth`
  that bearer-signs requests and refreshes once on a `401`, and PKCE helpers.
- **`generate_authtoken.py`** — the OAuth2 Authorization-Code-+-PKCE generator
  the upstream README references but never shipped. Read-only scopes only.
- **`server.py`** — new `XMCP_AUTH_MODE` (`oauth2` default, `oauth1` = upstream).
  In `oauth2` mode it never opens a browser at startup.
- **`Dockerfile` / `compose.yaml`** — slim image, token store on a named volume,
  host bind to `127.0.0.1` only, secrets injected via `varlock run` from
  Automic Vault (`.env.schema`).

## Auth modes

| | `oauth2` (default) | `oauth1` (upstream) |
|---|---|---|
| Browser at startup | Never (authorize once) | Every startup |
| Reads bookmarks | ✅ | ❌ |
| Reads home timeline / mentions / likes | ✅ | ✅ |
| Token refresh | Automatic | n/a (no expiry) |

## Scopes (minimal, read-only by design)

`tweet.read users.read bookmark.read like.read follows.read offline.access`

No write or DM scopes are ever requested, so the token physically cannot post,
like, or message. `offline.access` is required to get a refresh token.

## One-time setup

### 1. X Developer Portal (your app's *User authentication settings*)

- App type: **Web App, Automated App or Bot** (confidential client).
- App permissions: **Read**.
- Callback / Redirect URI: `http://127.0.0.1:8976/callback`
- Copy the **OAuth2 Client ID** and **Client Secret** (these are *not* the
  API/consumer keys) into your 1Password item as fields `client_id` and
  `client_secret`. 1Password stays the long-term store.

### 2. Local config

Secrets are declared in `.env.schema` and resolved by [varlock](https://varlock.dev)
from [Automic Vault](https://github.com/automic-vault/automic-vault/blob/main/docs/varlock.md).
Needs Automic Vault 3.9.0 or newer.

```bash
mise install                 # node + varlock, pinned in mise.toml
mise run setup               # confirm (once) and cache the pinned Automic Vault varlock plugin
av save --project-directory=. X_OAUTH2_CLIENT_ID
av save --project-directory=. X_OAUTH2_CLIENT_SECRET
```

Paste each value from the 1Password item at the hidden prompt.

Varlock must run under node (the `npm:varlock` pin), not the standalone binary:
Automic Vault rejects the standalone binary as the requesting process. `varlock run`
has to be launched by a real shell: from fish, a `mise run` task or a script,
Automic Vault answers "Varlock application process is unavailable". The mise
tasks go through `zsh -fc '...; exit $?'` for that reason (the trailing command
keeps zsh from exec-ing away). From fish, wrap any other call the same way.

### 3. Authorize once

```bash
mise run authorize
```

This prints a URL (and tries to open your browser). Approve, and the tokens land
in the `xmcp-tokens` Docker volume. Every varlock run needs one Automic Vault
approval. The client ID is deliberately not `@sensitive`, so it stays visible in
the printed authorize URL.

## Run

```bash
mise run up
```

MCP endpoint: `http://127.0.0.1:8000/mcp` (localhost only).

## Connect a client

```bash
claude mcp add --transport http xmcp http://127.0.0.1:8000/mcp
```

## Running on the host instead of Docker

```bash
varlock run -- uv run --with-requirements requirements.docker.txt python generate_authtoken.py
varlock run -- uv run --with-requirements requirements.docker.txt python server.py
```

The host token store defaults to `./.tokens/oauth2.json` (gitignored).
