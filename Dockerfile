# Minimal image for the X API read-only MCP server (OAuth2 user-context mode).
FROM python:3.12-slim

# uv for fast, no-cache dependency installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY requirements.docker.txt ./
RUN uv pip install --system --no-cache -r requirements.docker.txt

COPY server.py xauth.py generate_authtoken.py ./

# Bind to all interfaces *inside* the container; compose publishes only to
# 127.0.0.1 on the host, so the server stays localhost-only from the outside.
ENV PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    XMCP_AUTH_MODE=oauth2 \
    XMCP_TOKEN_STORE=/data/oauth2.json

# Token store (rotating refresh token) persists here across restarts.
VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "server.py"]
