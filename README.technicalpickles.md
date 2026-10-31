# technicalpickles fork

My personal version of `xdevplatform/xmcp`. This file summarizes how this branch
differs from upstream so the changes are easy to review and re-base.

- **Upstream base:** `63d3436` (Merge PR #1, fix/openapi-spec-url)
- **Full setup/runbook:** see [README.oauth2-docker.md](README.oauth2-docker.md)
- **Diffstat:** 10 files, +666 / -5. `server.py` is purely additive; the upstream
  OAuth1 path is untouched and still reachable.

## Why this fork exists

Upstream only implements OAuth1 user-context signing. Two problems for my use
(reading my own timeline, mentions, and bookmarks via a long-running local MCP):

1. **Bookmarks are unreadable.** The bookmarks endpoints require an OAuth2 user
   token with `bookmark.read`; OAuth1 cannot call them. Upstream's OAuth2 path is
   dead code (`get_auth_headers` is never wired in) and `generate_authtoken.py`
   is referenced by the README but was never committed.
2. **Not headless.** Upstream re-runs an interactive browser consent on *every*
   startup and keeps tokens in memory only, which is hostile to a container.

## Behavioral differences from upstream

| Behavior | Upstream | This fork |
|---|---|---|
| Default auth | OAuth1 user-context | **OAuth2 user-context** (`XMCP_AUTH_MODE=oauth2`) |
| Browser at startup | Every start | **Never** (authorize once, tokens persist) |
| Reads bookmarks | No | **Yes** (`bookmark.read`) |
| Token lifetime | In-memory, per-process | **Persisted** to a store, **auto-refreshed** on 401 |
| Scopes | n/a | **Read-only only** (no write/DM scopes requested) |
| Packaging | bare script | **Dockerfile + compose**, localhost/OrbStack, `op`-injected secrets |

`XMCP_AUTH_MODE=oauth1` restores upstream behavior exactly.

## What changed, file by file

**New files**
- `xauth.py` — OAuth2 token store (atomic 0600 writes), `httpx.Auth` that bearer-
  signs and refreshes once on a 401, PKCE helpers, the read-only scope set.
- `generate_authtoken.py` — the OAuth2 Authorization-Code-+-PKCE authorizer
  upstream's README references but never shipped.
- `Dockerfile`, `compose.yaml`, `requirements.docker.txt`, `.dockerignore` —
  minimal image; token store on a named volume; host bind to `127.0.0.1`;
  configurable OrbStack domain (`XMCP_ORB_DOMAIN`); secrets via `varlock run`.
- `.env.schema`, `mise.toml` — varlock schema (secrets from Automic Vault) +
  pinned tooling and tasks, plus the minimal tool allowlist.
- `README.oauth2-docker.md` — the runbook.

**Modified files**
- `server.py` (+54 / -4) — adds `XMCP_AUTH_MODE` and `build_oauth2_auth()`. In
  `oauth2` mode the client uses `XOAuth2Auth` and skips OAuth1 signing/browser
  consent entirely. The OAuth1 code path is preserved unchanged.
- `.gitignore` — keep `.env.schema` tracked; ignore the local `.tokens/` store.

## Re-basing on upstream

Because `server.py` changes are additive and everything else is new files, a
`git rebase master` should be low-conflict. The only upstream file touched is
`server.py` (and `.gitignore`).
