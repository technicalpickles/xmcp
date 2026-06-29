"""OAuth2 (Authorization Code + PKCE) user-context auth for the X API.

This module is an addition to the upstream xdevplatform/xmcp project. Upstream
only implements OAuth1 user-context signing, which cannot read the bookmarks
endpoints (those require an OAuth2 user token with the ``bookmark.read`` scope).
This adds:

- A persistent token store (access token + rotating refresh token).
- An ``httpx.Auth`` implementation that injects ``Authorization: Bearer`` and
  transparently refreshes on a 401, so a long-running server survives the ~2h
  access-token lifetime without a browser round-trip.
- PKCE helpers and a local callback waiter used by ``generate_authtoken.py``.

Tokens are read-only scopes by design (see ``DEFAULT_SCOPES``); the credential
physically cannot post, like, or send DMs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
from pathlib import Path

import httpx

LOGGER = logging.getLogger("xmcp.oauth2")

# x.com and twitter.com both work; x.com is the current canonical host.
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"

# Minimal read-only scope set. offline.access is required to receive a refresh
# token. No write/DM scopes are requested, so the token cannot mutate anything.
DEFAULT_SCOPES = "tweet.read users.read bookmark.read like.read follows.read offline.access"

# Refresh this many seconds before the access token actually expires.
_EXPIRY_SKEW_SECONDS = 60


def default_token_store_path() -> Path:
    raw = os.getenv("XMCP_TOKEN_STORE", "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent / ".tokens" / "oauth2.json"


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(
    client_id: str,
    redirect_uri: str,
    scopes: str,
    state: str,
    code_challenge: str,
) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def wait_for_oauth2_callback(
    bind_host: str, port: int, path: str, timeout_seconds: int
) -> tuple[str, str]:
    """Run a one-shot local HTTP server and return (code, state) from the redirect."""
    captured: dict[str, str | None] = {"code": None, "state": None, "error": None}
    event = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found.")
                return
            query = urllib.parse.parse_qs(parsed.query)
            captured["code"] = (query.get("code") or [None])[0]
            captured["state"] = (query.get("state") or [None])[0]
            captured["error"] = (query.get("error") or [None])[0]
            event.set()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OAuth2 complete. You may close this tab and return to the terminal.")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            LOGGER.debug("OAuth2 callback: " + format, *args)

    class _Server(socketserver.TCPServer):
        allow_reuse_address = True

    server = _Server((bind_host, port), _Handler)
    server.timeout = 1
    deadline = time.time() + timeout_seconds
    try:
        while time.time() < deadline:
            server.handle_request()
            if event.is_set():
                break
    finally:
        server.server_close()

    if captured["error"]:
        raise RuntimeError(f"Authorization failed: {captured['error']}")
    code = captured["code"]
    state = captured["state"]
    if not code or not state:
        raise TimeoutError("OAuth2 callback not received before timeout.")
    return code, state


class TokenStore:
    """JSON-backed access/refresh token store with atomic, 0600 writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expires_at: float = 0.0
        self.scope: str | None = None

    def load(self) -> bool:
        if not self.path.exists():
            return False
        data = json.loads(self.path.read_text())
        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token")
        self.expires_at = float(data.get("expires_at") or 0.0)
        self.scope = data.get("scope")
        return bool(self.refresh_token or self.access_token)

    def seed_from_env(self) -> bool:
        """Bootstrap from env (e.g. 1Password-injected) when no store file exists."""
        refresh = os.getenv("X_OAUTH2_REFRESH_TOKEN", "").strip()
        access = os.getenv("X_OAUTH2_ACCESS_TOKEN", "").strip()
        if not refresh and not access:
            return False
        self.refresh_token = refresh or None
        self.access_token = access or None
        # Force a refresh on first use; we don't trust a seeded access token's clock.
        self.expires_at = 0.0
        return True

    def update(self, access_token: str, refresh_token: str | None, expires_in: float, scope: str | None) -> None:
        self.access_token = access_token
        if refresh_token:
            self.refresh_token = refresh_token
        self.expires_at = time.time() + float(expires_in)
        if scope:
            self.scope = scope
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }
        tmp.write_text(json.dumps(payload, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


class TokenManager:
    """Holds the live token and refreshes it (rotating refresh token) on demand."""

    def __init__(self, store: TokenStore, client_id: str, client_secret: str | None) -> None:
        self.store = store
        self.client_id = client_id
        self.client_secret = client_secret or None
        self._lock = asyncio.Lock()

    def _is_fresh(self) -> bool:
        return bool(self.store.access_token) and (
            time.time() < self.store.expires_at - _EXPIRY_SKEW_SECONDS
        )

    async def valid_access_token(self) -> str:
        if self._is_fresh():
            return self.store.access_token  # type: ignore[return-value]
        await self._refresh()
        return self.store.access_token  # type: ignore[return-value]

    async def force_refresh(self) -> str:
        await self._refresh(force=True)
        return self.store.access_token  # type: ignore[return-value]

    async def _refresh(self, force: bool = False) -> None:
        async with self._lock:
            # Another coroutine may have refreshed while we waited for the lock.
            if not force and self._is_fresh():
                return
            if not self.store.refresh_token:
                raise RuntimeError(
                    "No OAuth2 refresh token available. Run generate_authtoken.py to "
                    "authorize, or set X_OAUTH2_REFRESH_TOKEN."
                )
            data = {
                "grant_type": "refresh_token",
                "refresh_token": self.store.refresh_token,
                "client_id": self.client_id,
            }
            auth = (self.client_id, self.client_secret) if self.client_secret else None
            LOGGER.info("Refreshing OAuth2 access token.")
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(TOKEN_URL, data=data, auth=auth)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"OAuth2 token refresh failed ({resp.status_code}): {resp.text[:500]}"
                )
            payload = resp.json()
            self.store.update(
                access_token=payload["access_token"],
                refresh_token=payload.get("refresh_token"),
                expires_in=payload.get("expires_in", 7200),
                scope=payload.get("scope"),
            )
            LOGGER.info("OAuth2 access token refreshed; expires in ~%ss.", payload.get("expires_in"))


class XOAuth2Auth(httpx.Auth):
    """httpx auth that bearer-signs requests and refreshes once on a 401."""

    def __init__(self, manager: TokenManager) -> None:
        self.manager = manager

    async def async_auth_flow(self, request):  # type: ignore[override]
        token = await self.manager.valid_access_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            LOGGER.info("Got 401 from X API; forcing token refresh and retrying once.")
            token = await self.manager.force_refresh()
            request.headers["Authorization"] = f"Bearer {token}"
            yield request

    def sync_auth_flow(self, request):  # type: ignore[override]
        # The MCP server uses an async client; this is a best-effort fallback only.
        token = self.manager.store.access_token
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request
