# chrome-web-mcp: everything bundled, user needs only Docker.
#   docker build -t chrome-web-mcp .
#   echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
#     | docker run -i --rm --security-opt seccomp=docker/seccomp_profile.json chrome-web-mcp
#
# MCP client config (stdio over docker):
#   Add "--security-opt", "seccomp=/absolute/path/to/docker/seccomp_profile.json"
#   to the MCP client's docker run arguments (see README.md).
#
# Visible-window mode needs the host X socket (Linux with X11):
#   See README.md for the UID and Xauthority mounts required by Xephyr.
ARG DEBIAN_FRONTEND=noninteractive

FROM debian:bookworm-slim AS base
ARG DEBIAN_FRONTEND
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip \
      chromium xvfb xserver-xephyr xdotool wmctrl x11-utils \
      fonts-liberation fonts-noto-cjk \
      sqlite3 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f > /dev/null

FROM base AS app
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir . \
 && useradd --create-home --uid 1000 --user-group chrome \
 && install -d -m 1777 /tmp/.X11-unix
ENV PATH="/opt/venv/bin:${PATH}" \
    CW_DISPLAY_MODE="xvfb" \
    HOME="/home/chrome"
USER chrome:chrome
ENTRYPOINT ["chrome-web-mcp"]
