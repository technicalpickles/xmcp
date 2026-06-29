"""One-time OAuth2 (Authorization Code + PKCE) token generator for the X API.

Run this once to authorize the app under your X account and write the access +
refresh tokens to the token store. The server (``server.py`` in oauth2 mode)
then uses and auto-refreshes those tokens; you never need a browser again unless
the refresh token is revoked or the store is wiped.

Usage (host):    python generate_authtoken.py
Usage (docker):  docker compose run --rm --service-ports authorize

Required env (typically injected from 1Password via ``op run``):
  X_OAUTH2_CLIENT_ID      (a.k.a. CLIENT_ID)      - the app's OAuth2 Client ID
  X_OAUTH2_CLIENT_SECRET  (a.k.a. CLIENT_SECRET)  - required for confidential clients

Optional env:
  XMCP_OAUTH2_SCOPES          default: read-only scope set (see xauth.DEFAULT_SCOPES)
  XMCP_OAUTH2_REDIRECT_URI    default: http://127.0.0.1:8976/callback
  XMCP_OAUTH2_CALLBACK_BIND   default: 127.0.0.1 (set to 0.0.0.0 inside a container)
  XMCP_OAUTH2_CALLBACK_PORT   default: 8976
  XMCP_OAUTH2_CALLBACK_PATH   default: /callback
  XMCP_OAUTH2_TIMEOUT         default: 300
  XMCP_TOKEN_STORE            default: ./.tokens/oauth2.json
"""

from __future__ import annotations

import os
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

import xauth


def _load_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_path, override=True)


def _require(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    raise SystemExit(f"Missing required env: one of {', '.join(names)}")


def main() -> None:
    _load_env()

    client_id = _require("X_OAUTH2_CLIENT_ID", "CLIENT_ID")
    client_secret = (os.getenv("X_OAUTH2_CLIENT_SECRET") or os.getenv("CLIENT_SECRET") or "").strip()

    scopes = os.getenv("XMCP_OAUTH2_SCOPES", "").strip() or xauth.DEFAULT_SCOPES
    redirect_uri = os.getenv("XMCP_OAUTH2_REDIRECT_URI", "http://127.0.0.1:8976/callback")
    bind_host = os.getenv("XMCP_OAUTH2_CALLBACK_BIND", "127.0.0.1")
    callback_port = int(os.getenv("XMCP_OAUTH2_CALLBACK_PORT", "8976"))
    callback_path = os.getenv("XMCP_OAUTH2_CALLBACK_PATH", "/callback")
    timeout = int(os.getenv("XMCP_OAUTH2_TIMEOUT", "300"))

    verifier, challenge = xauth.make_pkce_pair()
    state = secrets.token_urlsafe(32)
    authorize_url = xauth.build_authorize_url(client_id, redirect_uri, scopes, state, challenge)

    print("\n=== X API OAuth2 authorization ===")
    print(f"Scopes: {scopes}")
    print(f"Redirect URI (must be registered in the app): {redirect_uri}\n")
    print("Open this URL in your browser and approve:\n")
    print(f"  {authorize_url}\n")
    try:
        webbrowser.open(authorize_url)
    except Exception:
        pass

    print(f"Waiting for the redirect on {bind_host}:{callback_port}{callback_path} ...")
    code, returned_state = xauth.wait_for_oauth2_callback(bind_host, callback_port, callback_path, timeout)
    if returned_state != state:
        raise SystemExit("State mismatch; aborting (possible CSRF).")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "client_id": client_id,
    }
    auth = (client_id, client_secret) if client_secret else None
    resp = httpx.post(xauth.TOKEN_URL, data=data, auth=auth, timeout=30)
    if resp.status_code != 200:
        raise SystemExit(f"Token exchange failed ({resp.status_code}): {resp.text[:1000]}")
    payload = resp.json()

    store = xauth.TokenStore(xauth.default_token_store_path())
    store.update(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in", 7200),
        scope=payload.get("scope"),
    )

    print("\nAuthorized. Tokens written to:", store.path)
    print("Granted scopes:", payload.get("scope"))
    if not payload.get("refresh_token"):
        print(
            "\nWARNING: no refresh token returned. Make sure 'offline.access' is in the "
            "scope set, or the server will stop working after the access token expires."
        )
    else:
        print(
            "\nOptional: stash this refresh token in 1Password as a seed (it rotates on "
            "each use, so the live value lives in the token store / docker volume):"
        )
        print("  refresh_token:", payload["refresh_token"])


if __name__ == "__main__":
    main()
