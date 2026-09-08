# chrome-web-mcp

[日本語版 README](README.jp.md)

> **Supported OS: Linux and macOS.** The macOS port uses native Chrome (visible
> or `--headless=new`) and does not require X11. Windows is not supported.
> See [docs/macos.md](docs/macos.md) for macOS setup, project-local Chrome for
> Testing, and runtime details.

> [!CAUTION]
> Single-session use only: one client process, one browser, sequential searches.
>
> - Normal use never trips the limiter: `pace_warning` fires only at 15+
>   searches per minute. Sequential or lightly-parallel use stays far below it.
> - A warning is advisory, not a block: searches keep running. But Google
>   counts total volume too — sustained barrages end in CAPTCHA regardless of
>   pacing (observed after dozens of searches in one session). When challenged:
>   wait a few minutes and retry in hidden mode — it usually clears by itself;
>   in visible mode, solve the challenge in the browser window yourself, then
>   retry. `last_captcha_at` in
>   `health_check` shows the last hit.
> - opencode + subagents: SAFE. All agents share one server process and one
>   browser. Concurrent searches are pooled in a shared query queue
>   (per-process lock + SQLite pacing) and executed sequentially — verified
>   with 3 simultaneous subagents and 35 rapid searches, no CAPTCHA.
> - Separate processes at once are NOT absorbed: each process spawns its OWN
>   browser, so parallel CLIs look like fresh users hitting Google together.
>   Each one can be CAPTCHA-challenged independently (observed). Stagger starts
>   by seconds or raise `CW_MIN_DELAY` / `CW_MAX_DELAY`.

A stdio Model Context Protocol server that exposes focused browser tools:

- `google_search` — Google search through a JavaScript-rendered Chrome instance.
- `fetch_url` — JavaScript-rendered readable text extraction for public HTTP(S) URLs.
- `health_check` — display, browser, queue, and CAPTCHA status.

The server is designed to be configured by any MCP client that can launch a
stdio command.

## Features

- JS-rendered Google search + public URL fetch through real Chrome. Linux uses
  Xephyr/Xvfb; macOS uses native visible Chrome or native `--headless=new`.
- Shaped markdown by default (`trafilatura` + `html2text`, pure-Python, no
  extra service), with full-text fallback and follow-up link targets.
- Language/region hints (`hl`/`gl`) for reproducible JA/EN results.
- Parallel-safe: concurrent searches and fetches serialize only where the
  browser lifecycle requires it; each fetch uses its own tab.
- Fail-closed fetching: private networks, metadata hosts, and
  credential-bearing URLs are blocked, including post-redirect targets.
- Shared SQLite pacing for Google searches across MCP processes.
- `health_check` for display/browser/queue/CAPTCHA observability.
- Linux + macOS, with platform-specific browser/runtime isolation.

## Requirements

- Python 3.10 or newer
- Google Chrome, Google Chrome for Testing, or Chromium. On macOS this port
  first auto-detects a project-local Chrome for Testing build under
  `.local-chrome` after `CW_CHROME`, so a system Chrome install is optional.
- `Xephyr` on Linux for the default visible-window mode
- `Xvfb` on Linux for hidden-window mode
- `xpra` on Linux if interactive CAPTCHA recovery is desired

The Python dependencies are installed with the package. Chrome remains an
external host prerequisite. Xvfb/Xephyr are Linux-only prerequisites.

Non-headless Chrome on Xvfb is intentional: `--headless` is easier to
bot-detect, so Xvfb is kept as a requirement even though it is heavier.

## Platform support

Linux keeps the upstream X11 backend: Xephyr for visible mode and Xvfb for
hidden mode. macOS uses native Chrome directly: visible mode opens a normal
Chrome window and hidden mode uses `--headless=new`. macOS does not require
`DISPLAY`, Xvfb, Xephyr, or `--ozone-platform=x11`. `fcntl.flock`, POSIX process
groups, and process birth identity are retained; macOS process identity uses
`psutil` instead of `/proc`. Windows is not supported.

### macOS quickstart

macOS users can keep Chrome completely local to the checkout:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python scripts/install-chrome-for-testing.py
chrome-web-mcp
```

The installer fetches Google's official Chrome for Testing build for the host
architecture into the git-ignored `.local-chrome/` directory. Browser download
is always explicit; starting the MCP server never downloads Chrome. See
[docs/macos.md](docs/macos.md) for details.

## Headless environments

On a headless server, CI runner, SSH session without X forwarding, or any
machine without a desktop display, set `show_browser` to `false`:

```json
{
  "show_browser": false
}
```

Edit `~/.config/chrome-web-mcp/config.json` (or the file specified by
`CW_CONFIG`) and restart the MCP client. On Linux this selects hidden Xvfb; on
macOS it selects native `--headless=new` Chrome.

The built-in default is `true` for desktop use, so do not omit this setting on
a headless machine. A Docker installation already sets
`CW_DISPLAY_MODE=xvfb` inside the container, so it normally needs no host
display or extra setting.

## Linux quickstart (Debian/Ubuntu, copy-paste)

```bash
# 1. System dependencies
sudo apt update && sudo apt install -y chromium xvfb xserver-xephyr x11-utils python3-venv
which chromium || which google-chrome || which chromium-browser
which Xvfb
python3 --version  # 3.10+

# 2. Create an environment and install the package (either one)
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.'
# or: python -m pip install chrome_web_mcp-0.2.0-py3-none-any.whl
```

MCP handshake check (`TOOLS: ['fetch_url', 'google_search', 'health_check']` expected):

```bash
uv run python -c "
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    params = StdioServerParameters(command='uv', args=['run','--directory','/absolute/path/to/chrome-web-mcp','chrome-web-mcp'])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print('TOOLS:', [t.name for t in (await session.list_tools()).tools])
asyncio.run(main())
"
```

opencode (`~/.config/opencode/opencode.json`, Linux path example):

```json
{
  "mcp": {
    "chrome-web": {
      "type": "local",
      "command": ["uv", "run", "--directory", "/home/user/projects/chrome-web-mcp", "chrome-web-mcp"],
      "enabled": true
    }
  }
}
```

Notes:

- Replace `/absolute/path/to/chrome-web-mcp` with your checkout path.
- Set `CW_CHROME` only if auto-detection misses your binary. On macOS the normal
  stable app path is `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.
- Concurrent calls within one server are supported. Searches are serialized;
  fetches use independent tabs and share a serialized browser startup.
- `xephyr` mode needs a real desktop `DISPLAY` plus `xserver-xephyr`;
  on a headless host or over SSH without X forwarding it will not start.
  Default `xvfb` mode needs no `DISPLAY`.

## Install and run

From a built wheel:

```bash
python -m pip install chrome_web_mcp-0.2.0-py3-none-any.whl
chrome-web-mcp
```

For a published package, an MCP client can let `uvx` install it on demand:

```json
{
  "mcpServers": {
    "chrome-web": {
      "command": "uvx",
      "args": ["--from", "chrome-web-mcp==0.2.0", "chrome-web-mcp"]
    }
  }
}
```

For a local checkout:

```json
{
  "mcpServers": {
    "chrome-web": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/chrome-web-mcp", "chrome-web-mcp"]
    }
  }
}
```

The same server can be registered in Hermes with YAML:

```yaml
mcp_servers:
  chrome-web:
    command: /absolute/path/to/python
    args:
      - -m
      - chrome_web_mcp
    timeout: 120
    connect_timeout: 60
    enabled: true
```

For a wheel installation, use the Python interpreter from the environment
where the wheel was installed. MCP clients should launch the process over
stdio and must not add shell-specific quoting around the arguments.

## Docker

The image bundles Python, dependencies, Chromium, Xvfb (plus Xephyr/xdotool
for visible-window mode), so users only need Docker:

```bash
docker build -t chrome-web-mcp .
```

```json
{
  "mcpServers": {
    "chrome-web": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "chrome-web-mcp"]
    }
  }
}
```

For opencode, add a separate entry to `~/.config/opencode/opencode.json` so
you can keep the local and Docker versions available side by side:

```json
"chrome-web-docker": {
  "type": "local",
  "command": ["docker", "run", "--rm", "-i", "chrome-web-mcp:latest"],
  "enabled": true,
  "timeout": 120000
}
```

The Docker image uses hidden Xvfb by default. This is separate from the local
`chrome-web` entry, whose `show_browser` setting can display a desktop window.
Restart opencode after adding or changing the entry.

Hidden mode is normally sufficient. If Google returns
`captcha_required: true`, the CAPTCHA is displayed inside the Docker
container's browser, so the local `chrome-web` window cannot solve it. Start
the Docker MCP server in visible Xephyr mode instead and retry the search:

```bash
docker run -i --rm \
  -e DISPLAY=$DISPLAY \
  -e CW_DISPLAY_MODE=xephyr \
  -e XAUTHORITY=$XAUTHORITY \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v "$XAUTHORITY":"$XAUTHORITY":ro \
  chrome-web-mcp
```

This requires a Linux desktop X display and `Xephyr`. On Wayland/Mutter, the
working Xauthority file may be a `.mutter-Xwaylandauth.*` file under
`$XDG_RUNTIME_DIR` rather than `$XAUTHORITY`; mount that directory and set
`XAUTHORITY` to the matching path inside the container. The resulting
`chrome-web-mcp` window belongs to the Docker process, and you must retry using
that same Docker MCP session. For the simplest CAPTCHA recovery, use the local
`chrome-web` entry with `show_browser: true` from the beginning.

Image size is about 1.5 GB (mostly Chromium and fonts).

## Update

For a local checkout, pull and re-sync (restart the MCP client afterwards):

```bash
git -C /absolute/path/to/chrome-web-mcp fetch origin
git -C /absolute/path/to/chrome-web-mcp reset --hard origin/main
```

`reset --hard` is used instead of `pull --ff-only` because history is
occasionally force-pushed; stash local changes first if you have any
(`git stash`). `uv run --directory ... chrome-web-mcp` picks up the new
dependencies automatically on next launch — no reinstall step needed.

For a dedicated venv (e.g. `~/.local/share/chrome-web-mcp/venv`), reinstall
after pulling:

```bash
uv pip install --python ~/.local/share/chrome-web-mcp/venv/bin/python -U -e /absolute/path/to/chrome-web-mcp
```

For a wheel install, rebuild and reinstall:

```bash
python -m build --wheel --outdir /absolute/path/to/chrome-web-mcp/dist /absolute/path/to/chrome-web-mcp
python -m pip install -U /absolute/path/to/chrome-web-mcp/dist/chrome_web_mcp-*.whl
```

For Docker, just rebuild — no `Dockerfile` edit is needed (it copies
`pyproject.toml` + `src/` and runs `pip install .`, so code and dependency
changes are picked up automatically):

```bash
docker build -t chrome-web-mcp /absolute/path/to/chrome-web-mcp
```

Verify after update:

```bash
uv pip list --python ~/.local/share/chrome-web-mcp/venv/bin/python | grep -E "chrome-web|trafilatura"
```

## Install size (rough, Debian host)

- Python environment (`.venv`, incl. trafilatura/html2text): ~100 MB
- This package source: under 1 MB
- Chromium set: ~485 MB
- Xvfb + x11-utils: ~5 MB
- Total: ~600 MB, dominated by Chromium. No new system packages were added
  for markdown shaping (pure-Python dependencies only).

## Tools

Workflow: first `google_search`, then `fetch_url` on interesting result URLs
for full text. `health_check` reports server state without starting a browser.

### `google_search`

Input:

```json
{"query": "search terms", "limit": 5, "hl": "ja", "gl": "jp"}
```

`limit` is an integer from 1 to 20. Results are returned as structured JSON
with `title`, `url`, `description`, and `position` fields, plus `waited_ms`
(the shared rate-limiter queue wait). `hl`/`gl` are optional Google language
(`ja`/`en`) and region (`jp`/`us`) hints; defaults preserve Japanese results.
`query` is required, max 512 chars. Searches run one at a time per process
and are paced across processes (see Runtime configuration). Bursts of 15+
searches per minute return a `pace_warning` — slow down or batch queries to
avoid a Google CAPTCHA; `health_check` reports the recent count.

### `fetch_url`

Input:

```json
{"url": "https://example.com", "char_limit": 15000, "format": "markdown"}
```

Only public `http://` and `https://` URLs without embedded credentials are
accepted. Localhost, private IP ranges, metadata hosts, and non-public DNS
resolutions are rejected. A mandatory local HTTP proxy validates every upstream
connection and connects to the validated numeric address, including redirects,
subresources, WebSockets, and DNS changes. Chrome's loopback proxy bypass, QUIC,
and non-proxied WebRTC UDP are disabled. IPv6 translation/tunneling addresses
(NAT64, 6to4, Teredo) are also rejected. `char_limit` is an integer from 100 to
200000. HTML/text snapshots are read in chunks, with a 16Mi-character extraction
limit independent of the returned text limit. Text is cut at a
sentence boundary when possible. Returns `requested_url`, `final_url`,
`redirected`, `total_chars`, and `truncated` alongside `title` and content
(`url` mirrors `final_url` for compatibility). `format` defaults to
`markdown`: shaped readable markdown with boilerplate removed
(`trafilatura`, `html2text` fallback). The response reports `formatted: true`
and the `extraction` method, so agents can tell it was shaped — if content
looks missing, retry with `format: "text"` for the full rendered text.
`format: "links"` adds follow-up link targets. Concurrent fetches are
parallel-safe; each uses its own tab. `url` is required, max 2048 chars.
Failures return `{"success": false, "error": "..."}` (plus
`"captcha_required": true` for Google challenges). This shaping reuses the same
`trafilatura` + `html2text` approach as a self-hosted jina-compatible Reader,
without needing the extra service.

### `health_check`

Input: `{}` (no arguments).

Returns `display_mode`, browser/process liveness, the rate-limiter queue wait,
its `min`/`max` delays, and the last CAPTCHA time.

## Runtime configuration

Most users only need the JSON file:

```bash
mkdir -p ~/.config/chrome-web-mcp
cp examples/config.json examples/config.md ~/.config/chrome-web-mcp/
```

Then edit `~/.config/chrome-web-mcp/config.json` and restart the MCP client.
The settings most people change are:

```json
{
  "show_browser": true,
  "hl": "ja",
  "gl": "jp"
}
```

- `show_browser`: `true` shows the browser window; `false` hides it.
- `hl`: Google interface language. `ja` is Japanese and `en` is English.
- `gl`: Google result region. `jp` is Japan and `us` is the United States.

The built-in search defaults are `hl: ja` and `gl: jp`, but each user can set
their own values in this file. For example, use `"hl": "en"` and
`"gl": "us"` for English/US-oriented Google results. See
[`examples/config.md`](examples/config.md) for all settings and examples.

The JSON file is optional. If it is absent, the built-in defaults are used and
the browser window is shown (`show_browser: true`). Set it to `false` to hide
the window. JSON comments are not supported, so keep `config.json` as plain
JSON.

Optional environment variables:

- `CW_CONFIG` — path to an optional JSON config file for window on/off and
  tool defaults. When unset, `~/.config/chrome-web-mcp/config.json` is used
  if it exists. Explicit environment variables below win over the file.
  Settings become tool defaults when the caller omits them. Unknown keys warn
  on stderr; invalid values warn and keep the built-in default per key.
- `CW_CHROME` — explicit Chrome/Chromium executable path.
  On macOS, when unset, a project-local `.local-chrome/**/Google Chrome for
  Testing.app` is preferred before system Chrome/Chromium apps.
- `CW_PROFILE_DIR` — explicit browser profile directory. By default, each
  server process uses an isolated per-PID temporary profile.
- `CW_LOCK_PATH` — explicit lock-file path when `CW_PROFILE_DIR` is set.
- `CW_RATE_LIMIT_DB` — shared SQLite path for the Google-search start-slot
  queue. By default it is `/tmp/chrome-web-mcp/search-rate-limit.sqlite3`, so
  separate MCP processes of the same user share one limiter.
- `CW_MIN_DELAY` / `CW_MAX_DELAY` — randomized gap (seconds) between Google
  search starts. Defaults `1.0` / `2.5`.
- `CW_DISPLAY_MODE` — advanced environment override for `show_browser`.
  Linux uses `xvfb` or `xephyr`. macOS uses `headless` or `native` (Linux names
  are accepted as compatibility aliases on macOS).
- When `CW_DISPLAY_MODE=xephyr`, the server preserves an explicit `XAUTHORITY`
  or automatically discovers Mutter's `.mutter-Xwaylandauth.*` file under
  `XDG_RUNTIME_DIR`, then falls back to `~/.Xauthority`. This lets stdio MCP
  clients that filter their inherited environment still connect to the user's
  Xwayland display.
- `CW_XPRA_EXPOSE` — set to `1` to re-enable automatic Xpra attach when a
  CAPTCHA appears. Off by default: automatic attach once crashed the desktop
  session, so the server only reports the CAPTCHA and leaves the browser
  where it is.

The default per-process profile prevents separate MCP clients from contending
for one Chrome profile. Do not share a profile between live server processes
unless that is intentional.

Google-search calls also reserve a slot in the shared SQLite queue. Separate
MCP processes therefore start searches one at a time with a 1.0–2.5 second
randomized gap. A reserved slot is not retried automatically if Google returns
an error; the tool returns the error to the MCP client. `fetch_url` is not put
through this Google-search queue.

## Security and scope

- The server exposes no arbitrary page-context JavaScript tool.
- Browser HTTP(S)/WebSocket connections are restricted to public addresses by
  a local validating proxy; private destinations are rejected before connecting.
  Input URLs also reject embedded credentials and recognizable secret patterns.
  This does not classify every possible secret in arbitrary page URLs/content.
- Each server owns and cleans up its Chrome and platform display process groups.
- Linux Chrome is forced onto its private X11 display. macOS Chrome uses a
  separate temporary profile and native/headless backend without touching the
  user's normal Chrome profile.
- SIGTERM and SIGINT trigger browser and temporary-profile cleanup before exit.
  Startup recovers recorded Chrome/display processes using PID and start time,
  not broad process-name matching. Legacy profiles without process records can
  recover Chrome by exact profile argument; their Xvfb cannot be safely identified.
- Google result URLs are normalized and deduplicated before returning.

This package does not bypass authentication or CAPTCHA challenges. It is a
browser-backed search/fetch MCP server, not a general remote browser-control
API.

When Google presents a CAPTCHA during `google_search`, the server returns
`captcha_required: true`. With `show_browser: true`, solve the challenge in the
visible Chrome/Xephyr window and retry the same search. With
`show_browser: false`, wait a while and retry or restart in visible mode.
Automatic Xpra attach is Linux-only and disabled unless explicitly enabled.

## Acknowledgements

Browser-backed web tooling was originally derived from
[antirez/ds4](https://github.com/antirez/ds4) and subsequently substantially
reworked for MCP. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the
applicable MIT license notice.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, test (`pytest -q -m 'not live'` for deterministic only), and build steps.
