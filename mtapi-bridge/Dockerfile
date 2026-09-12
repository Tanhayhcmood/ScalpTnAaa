# mt5rest bridge — connects MT5 terminal to the trading robot via HTTP REST.
# Image: https://hub.docker.com/r/timurila/mt5rest
# The bridge listens on port 80 and exposes the following endpoints:
#   GET /Ping              — liveness probe (used by Render health check)
#   GET /ConnectEx         — authenticate and open MT5 connection
#   GET /AccountSummary    — account balance/equity
#   GET /OpenedOrders      — open positions
#   GET /PriceHistoryV2    — OHLCV candle data
#   GET /GetQuote          — current bid/ask
#   GET /Disconnect        — close connection
#
# Required env vars (set in render.yaml / Render dashboard):
#   MT5_HOST      — broker server name (e.g. AMarkets-Demo)
#   MT5_PORT      — broker port (default: 443)
#   MT5_USER      — MT5 account login number
#   MT5_PASSWORD  — MT5 account password
#
# Build context: rootDir=mtapi-bridge, dockerContext=. → context IS mtapi-bridge/
# All COPY paths below are relative to the mtapi-bridge/ directory.

# ── Stage 1: build socat in a compatible Ubuntu 20.04 image ──────────────────
# ubuntu:20.04 ships OpenSSL 1.1 — the same version present in the mt5rest
# Wine image — so socat's libssl.so.1.1 dependency is already satisfied in
# the final image and does NOT need to be copied.
# Only libwrap.so.0 is missing from the mt5rest base; all other socat
# runtime libraries (libc, libssl, libcrypto, libnsl, libutil, libpthread,
# libdl) are already present (verified via docker inspect).
FROM ubuntu:20.04 AS socat-source
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2: final mt5rest image with Render PORT proxy ───────────────────────
FROM timurila/mt5rest:26.07.22-06.37

# mt5rest/.NET watches its JSON configuration files for reloads. Render's
# shared container limit can exhaust inotify instances during Wine/.NET
# restarts, which otherwise crashes the bridge before it can expose /Ping.
# Polling removes that host-level dependency without changing MT5 behaviour.
ENV DOTNET_USE_POLLING_FILE_WATCHER=1

# Copy socat binary and its one missing runtime dependency (libwrap.so.0).
# Paths are from ubuntu:20.04 where libs live under /lib, not /usr/lib.
COPY --from=socat-source /usr/bin/socat                              /usr/local/bin/socat
COPY --from=socat-source /lib/x86_64-linux-gnu/libwrap.so.0         /lib/x86_64-linux-gnu/libwrap.so.0

# ── Render PORT proxy entrypoint ──────────────────────────────────────────────
# ROOT CAUSE FIX: mt5rest always listens on port 80, but Render's reverse-proxy
# routes all external HTTPS traffic — including the /Ping health check — to the
# container on $PORT (typically 10000+ on the free tier).  Without this fix,
# Render marks ger-mtapi unhealthy on every health check, restarts it
# in a continuous loop, and the trading robot can never establish a stable MT5
# connection via ConnectEx.  No trades can be placed.
#
# render-entrypoint.sh starts  socat $PORT → 80  in the background, then
# exec-s the original container command.
#
# CRITICAL: docker inspect shows the base image uses
#   ENTRYPOINT ["dotnet", "mt5rest.dll"]  CMD: null
# Overriding ENTRYPOINT without setting CMD leaves $@ empty — the container
# crashes immediately on exec "".  We restore the original startup command
# via CMD so it is passed as positional args to our wrapper script.
COPY render-entrypoint.sh /render-entrypoint.sh
RUN chmod +x /render-entrypoint.sh

ENTRYPOINT ["/render-entrypoint.sh"]
CMD ["dotnet", "mt5rest.dll"]

EXPOSE 80

# Internal health check: verifies /Ping on mt5rest's fixed port 80.
# Render's external health check reaches the service through the socat proxy.
HEALTHCHECK --interval=60s --timeout=30s --start-period=120s --retries=3 \
    CMD wget -qO- http://localhost:80/Ping || exit 1
