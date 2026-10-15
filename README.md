# chrome-web-mcp

[日本語版 README](README.jp.md)

> **Supported OS: Linux and macOS.** The macOS port uses native Chrome (visible
> or `--headless=new`) and does not require X11. Windows is not supported.
> See [docs/macos.md](docs/macos.md) for macOS setup, project-local Chrome for
> Testing, and runtime details.

> [!CAUTION]
> One browser at a time per profile — several MCP client processes share it.
>
> - Normal use never trips the limiter: `pace_warning` fires only at 15+
>   searches per minute. Sequential or lightly-parallel use stays far below it.
> - A warning is advisory, not a block: searches keep running. But Google
>   counts total volume too, so the server throttles itself: `max_searches_per_hour`
>   (default 60) and an exponential CAPTCHA cooldown.
> - A Google CAPTCHA no longer ends the call. Google goes on a shared cooldown
>   and the same query is answered by the next engine in `search_engines`
>   (DuckDuckGo, Brave, Bing, and optionally a self-hosted SearXNG by default).
>   `data.engine` reports who answered and `data.attempts` lists every attempt.
>   In visible (`xephyr`) mode the challenge window is left on screen so you can
>   solve it yourself; `last_captcha_at` and `engine_cooldowns_s` in
>   `health_check` show the current state. Recovery is worth waiting for:
>   sustained barrages end in CAPTCHA regardless of pacing, so if every engine
>   is cooling down, wait a few minutes and retry.
> - opencode + subagents: SAFE. All agents share one server process and one
>   browser. Concurrent searches are pooled in a shared query queue
>   (per-process lock + SQLite pacing) and executed sequentially — verified
>   with 3 simultaneous subagents and 35 rapid searches, no CAPTCHA.
> - Separate processes share one browser: the first process to claim the
>   persistent profile owns Chrome/Xvfb and publishes its CDP endpoint; later
>   processes attach to that browser (each keeps its own tabs), so N sessions
>   cost one ~600 MB browser tree instead of N. If the registry file is missing
>   or unusable, a process with a busy profile still falls back to its own
>   throwaway profile exactly as before (`profile_shared_fallback: true`).
>   Stagger starts by seconds or raise `CW_MIN_DELAY` / `CW_MAX_DELAY`.

A stdio Model Context Protocol server that exposes focused browser tools:

- `google_search` — Google search through a JavaScript-rendered Chrome instance,
  with automatic fallback to other engines when Google challenges.
- `fetch_url` — JavaScript-rendered readable text extraction for public HTTP(S) URLs.
- `health_check` — display, browser, queue, CAPTCHA, profile, engine, and idle
  release status.

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
- CAPTCHA-resilient search: when Google challenges, the same query is answered
  by the next configured engine (DuckDuckGo, Brave, Bing, or a self-hosted
  SearXNG) and the response reports which engine answered, so one fragile
  engine can no longer fail a tool call.
- Warm persistent Chrome profile: cookies and consent state survive restarts, so
  Google sees a returning browser instead of a first-time visitor. Set
  `"profile": "ephemeral"` for the old throwaway per-process profile.
- One shared browser per profile: the process that wins the profile lock owns
  Chrome/Xvfb and publishes its CDP endpoint in
  `~/.cache/chrome-web-mcp/profile/.browser.json`; every later process attaches
  to it instead of starting another ~600 MB browser tree. Attached clients are
  announced with `profile/clients/<pid>.json` heartbeats, and the idle reaper
  refuses to release the browser while any client is still attached.
  `health_check` reports `browser_role`, `owner_pid` and `clients`.
- Consistency-first fingerprint hardening: UA, `navigator.platform`, client
  hints, and WebGL vendor strings are kept mutually consistent (`"spoof_platform"`).
- Shared SQLite pacing, CAPTCHA cooldowns, and an hourly search budget across
  MCP processes.
- Idle release: after `idle_release_s` (default five minutes) without a browser
  call the server frees its Chrome + display backend (~600 MB measured on
  Linux/Xvfb) while the MCP process stays alive, then restarts it transparently
  on the next call (0.88 s cold start measured). `idle_release_s: 0` disables it.
- Query typed into the Google search box (not a direct `/search?q=` hit) by
  default; `"human_input": false` restores direct navigation.
- `health_check` for display/browser/queue/CAPTCHA/profile/engine/idle
  observability.
- Linux + macOS, with platform-specific browser/runtime isolation (Xvfb/Xephyr,
  `fcntl` and process groups on Linux; native Chrome or `--headless=new` on
  macOS).

## Requirements

- Python 3.10 or newer
- Google Chrome, Google Chrome for Testing, or Chromium. On macOS this port
  first auto-detects a project-local Chrome for Testing build under
  `.local-chrome` after `CW_CHROME`, so a system Chrome install is optional.
- `Xephyr` on Linux for the default visible-window mode
- `Xvfb` on Linux for hidden-window mode
- `xauth` on Linux: the private display is started with a per-display
  MIT-MAGIC-COOKIE-1 file, and the server refuses to start a display it cannot
  authenticate
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
sudo apt update && sudo apt install -y chromium xvfb xserver-xephyr xauth x11-utils python3-venv
which chromium || which google-chrome || which chromium-browser
which Xvfb
python3 --version  # 3.10+

# 2. Create an environment and install the package (either one)
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.'
# or: python -m pip install chrome_web_mcp-0.3.0-py3-none-any.whl
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
python -m pip install chrome_web_mcp-0.3.0-py3-none-any.whl
chrome-web-mcp
```

For a published package, an MCP client can let `uvx` install it on demand:

```json
{
  "mcpServers": {
    "chrome-web": {
      "command": "uvx",
      "args": ["--from", "chrome-web-mcp==0.3.0", "chrome-web-mcp"]
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

The image bundles Python, dependencies, Chromium, Xvfb (plus Xephyr/xdotool and
xauth for visible-window mode), so users only need Docker:

```bash
docker build -t chrome-web-mcp .
```

Without `--user`, the image runs as the non-root `chrome` user defined in
`Dockerfile` (UID/GID `1000:1000`) with the Chromium sandbox enabled.
The visible Xephyr example below overrides this default with your host account's
UID/GID.

Pass the included `docker/seccomp_profile.json` on every `docker run`: Docker's
default seccomp policy blocks the user namespaces needed by the sandbox.
The file is read by the Docker CLI on the client host, so use its absolute path
in MCP configurations. The host must also permit unprivileged user namespaces.
Do not override the image user with root or disable the sandbox.

The profile is based on [Moby's default seccomp policy](https://github.com/moby/profiles/blob/61eaf32614c7c71b60bd8927d3e6a4ffc8ff1f31/seccomp/default.json),
licensed under [Apache 2.0](docker/LICENSE). The only added rule allows `clone`,
`setns`, and `unshare` for Chromium's namespaces, as described in
[Playwright's Docker guidance](https://playwright.dev/docs/docker#crawling-and-scraping).
The other default-deny restrictions are retained.

```json
{
  "mcpServers": {
    "chrome-web": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "--security-opt", "seccomp=/absolute/path/to/chrome-web-mcp/docker/seccomp_profile.json", "chrome-web-mcp"]
    }
  }
}
```

For opencode, add a separate entry to `~/.config/opencode/opencode.json` so
you can keep the local and Docker versions available side by side:

```json
"chrome-web-docker": {
  "type": "local",
  "command": ["docker", "run", "--rm", "-i", "--security-opt", "seccomp=/absolute/path/to/chrome-web-mcp/docker/seccomp_profile.json", "chrome-web-mcp:latest"],
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
  --security-opt seccomp=docker/seccomp_profile.json \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e DISPLAY=$DISPLAY \
  -e CW_DISPLAY_MODE=xephyr \
  -e XAUTHORITY=/tmp/.Xauthority \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v "$XAUTHORITY":/tmp/.Xauthority:ro \
  chrome-web-mcp
```

Run this from a non-root Linux desktop account with a readable `XAUTHORITY`
file. The `--user "$(id -u):$(id -g)"` option overrides the image default with the
UID/GID of the host account running the command. Matching the Xauthority file
owner's UID lets Xephyr read a private mode-0600 cookie; `HOME=/tmp` keeps Chrome's
home writable when that UID differs from the image's UID 1000.
Custom profile, lock, or rate-database mounts must also be writable by that UID.
This requires a Linux desktop X display and `Xephyr`. On Wayland/Mutter, the
working Xauthority file may be a `.mutter-Xwaylandauth.*` file under
`$XDG_RUNTIME_DIR` rather than `$XAUTHORITY`; set the host's `XAUTHORITY` to that
file before running the command above. The resulting
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
- xauth: <1 MB
- Total: ~600 MB, dominated by Chromium. No new system packages were added
  for markdown shaping (pure-Python dependencies only).

Idle release (`idle_release_s`, default 300 s) gives that ~600 MB browser tree
back while the server process stays alive; the next tool call pays the 0.88 s
cold start. Set `idle_release_s: 0` to keep the browser warm.

## Tools

Workflow: first `google_search`, then `fetch_url` on interesting result URLs
for full text. `health_check` reports server state without starting a browser.

### `google_search`

Input:

```json
{"query": "search terms", "limit": 5, "hl": "ja", "gl": "jp", "engine": "auto"}
```

`limit` is an integer from 1 to 20. Results are returned as structured JSON
with `title`, `url`, `description`, and `position` fields, plus:

- `engine` — which engine answered (`google`, `ddg`, `brave`, `bing`, `searxng`).
- `attempts` — one entry per engine tried, with `ok`, and either the error or
  the number of results. Google challenges appear as `"error": "captcha"` with
  the `cooldown_s` applied.
- `captcha_avoided` — `true` when Google challenged and another engine answered.
- `waited_ms` — the shared rate-limiter queue wait.
- `pace_warning` — set at 15+ searches per minute.

`hl`/`gl` are optional Google language (`ja`/`en`) and region (`jp`/`us`)
hints; defaults preserve Japanese results. `engine` defaults to `auto` (the
configured order); pinning one engine tries it first and keeps the rest as
fallback. `query` is required, max 512 chars. Searches run one at a time per
process and are paced across processes (see Runtime configuration).

When Google challenges, the server does **not** solve the challenge: it records
the CAPTCHA, puts Google on an exponential cooldown (shared across processes),
and answers the query from the next engine. In visible (`xephyr`) mode the
challenge window is left on screen so you can solve it; solving it clears the
cooldown on the next successful Google search. Failures return
`{"success": false, "error": "...", "attempts": [...]}`, with
`"captcha_required": true` when a challenge was the reason.

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
its `min`/`max` delays, `recent_searches_60s`, `pace_warning`, and
`last_captcha_at`. `running_as_root` reports whether the process runs as root
(in which case rendering is refused unless `CW_ALLOW_ROOT=1`). It also reports
the CAPTCHA-resilience state:

- `profile_mode` (`persistent` or `ephemeral`), `profile_dir`, and
  `profile_shared_fallback` (true when a sibling session owned the shared
  profile and this process used its own throwaway one instead).
- `browser_role` (`owner` runs the profile's one Chrome/Xvfb pair, `shared` is
  attached to another process's browser, `none` has neither), `owner_pid` (the
  owner's pid, set only when `shared`), and `clients` (attached client
  processes, reported by the owner; `null` elsewhere). `shared_attach_error`
  explains why attaching was refused when it was. Attaching validates the
  registry file (private, same user, versioned, loopback CDP endpoint), the
  owner's pid + birth stamp, and a `Browser.getVersion` probe, before any page
  traffic is sent to it.
- `engines` (the order they will be tried), `engine_cooldowns_s` (seconds of
  remaining cooldown per engine), `searches_last_hour`,
  `max_searches_per_hour`, `budget_exhausted`.
- `human_input` and `spoof_platform` (the active fingerprint mode).
- `idle_release_s` (the active idle timeout, `0` when disabled), `browser_idle_s`
  (seconds since the last browser tool call, `null` while no browser is
  running), `browser_releases` (idle releases since this process started), and
  `active_calls` (browser calls in flight; `health_check` itself does not
  count).

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

The CAPTCHA-resilience settings are optional and default to the recommended
values; add them only to change the behavior:

```json
{
  "profile": "persistent",
  "search_engines": ["google", "ddg", "brave", "bing"],
  "searxng_url": "",
  "human_input": true,
  "max_searches_per_hour": 60,
  "captcha_cooldown_s": 300,
  "captcha_cooldown_max_s": 1800,
  "spoof_platform": "native",
  "disable_gpu": true,
  "idle_release_s": 300
}
```

- `profile`: `persistent` keeps a warm profile (fewer CAPTCHAs); `ephemeral`
  restores the throwaway per-process profile.
- `search_engines`: engines to try, in order. `searxng` requires `searxng_url`.
- `searxng_url`: base URL of a self-hosted SearXNG (needs `format=json`). It comes
  from your config or environment, never from model output, so a private address
  (`http://127.0.0.1:8080`, `http://10.0.0.1:4000`) is allowed. Redirects are not
  followed; point it at the final URL.
- `human_input`: type the query into the Google box instead of loading
  `/search?q=` directly.
- `max_searches_per_hour` / `captcha_cooldown_s` / `captcha_cooldown_max_s`:
  Google self-throttling (the cooldown doubles per consecutive CAPTCHA).
- `spoof_platform`: `native` keeps the Linux UA and `navigator.platform`
  consistent; `windows` applies a matching Windows UA, platform, and client
  hints together.
- `disable_gpu`: keep Chrome's software rendering (stable on headless hosts).
- `idle_release_s`: release the browser after this many seconds without a
  `google_search`/`fetch_url` call, freeing ~600 MB while the MCP process keeps
  running; `0` disables it. Measured on a Debian host: the browser tree is about
  600 MB RSS, a cold start takes 0.88 s, and cleanup 0.62 s — so an idle release
  costs roughly 0.9 s on the next call. The persistent profile is kept.

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
- `CW_CHROME` — explicit Chrome/Chromium executable path. On macOS, when unset,
  a project-local `.local-chrome/**/Google Chrome for Testing.app` is preferred
  before system Chrome/Chromium apps.
- `CW_PROFILE_DIR` — explicit browser profile directory. When set it is used
  verbatim, is never deleted, and disables the busy-profile fallback below.
- `CW_PROFILE_MODE` — `persistent` (default) keeps one warm profile under
  `~/.cache/chrome-web-mcp/profile` so cookies and consent state survive
  restarts; `ephemeral` restores the throwaway per-PID profile under `/tmp`.
- `CW_LOCK_PATH` — explicit lock-file path when `CW_PROFILE_DIR` is set.
- `CW_RATE_LIMIT_DB` — shared SQLite path for the Google-search start-slot
  queue, the CAPTCHA cooldowns, and the hourly budget. By default it is
  `/tmp/chrome-web-mcp/search-rate-limit.sqlite3`, so separate MCP processes of
  the same user share one limiter.
- `CW_SEARCH_ENGINES` — comma-separated engine order, e.g. `google,ddg,brave,bing`.
- `CW_SEARXNG_URL` — base URL of a self-hosted SearXNG; enables the `searxng`
  engine (it is skipped while this is unset). The URL must be publicly
  reachable: it goes through the same public-URL policy as a `fetch_url` target,
  so a private address (e.g. `http://10.0.0.1:4000`) is refused rather than
  queried.
- `CW_HUMAN_INPUT` — `0` disables typing into the Google search box.
- `CW_MAX_SEARCHES_PER_HOUR` — Google-only hourly budget (`0` disables it).
- `CW_CAPTCHA_COOLDOWN` / `CW_CAPTCHA_COOLDOWN_MAX` — first CAPTCHA cooldown in
  seconds and the exponential cap. Defaults `300` / `1800`.
- `CW_MIN_DELAY` / `CW_MAX_DELAY` — randomized gap (seconds) between Google
  search starts. Defaults `1.0` / `2.5`.
- `CW_IDLE_RELEASE` — release the browser after this many idle seconds (`0`
  disables it). Default `300`; a bad value falls back to the config.
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
- `CW_ALLOW_ROOT` — set to `1` to allow rendering as root. Off by default:
  the server refuses to start Chrome as root because it renders
  attacker-controlled HTML and would have to pass `--no-sandbox`. Run as an
  unprivileged user instead; the Docker image already uses UID 1000.

The default profile is warm and persistent: cookies, consent state, and the
Chrome cache survive between runs, which is what keeps Google from treating
every session as a first-time visitor. Exactly one process owns it at a time
(the lock file is embedded with the holder's pid/start time and is recovered
automatically if the holder dies). A process that finds the profile held by a
live sibling attaches to the browser that owner published in
`PROFILE_DIR/.browser.json` and skips its own Xvfb/Chrome/proxy startup, so
several MCP sessions cost one browser tree (~600 MB) instead of one each. The
attaching process owns no browser and no lock, so its shutdown can never kill
or clean up the owner's Chrome. If that registry is missing, malformed, stale
or not the owner's (pid + birth stamp + a live `Browser.getVersion` probe),
the process keeps today's behavior: it falls back to its own throwaway profile
and reports `profile_shared_fallback: true` from `health_check`. To delete the
warm profile, stop the server and remove `~/.cache/chrome-web-mcp/profile`.

Two limits are worth knowing. A local user on the same machine can reach the
owner's loopback DevTools port while the browser is up, exactly as they could
reach any of today's per-process ports; the mandatory public-network proxy still
restricts what that browser can fetch. Moving the transport to Chrome's
`--remote-debugging-pipe` (fds 3/4, no TCP listener) is future work; the
owner/attach design is compatible with it because clients would talk to the
owner over a unix socket instead of the port. Also, while a client is attached
the owner will not idle-release the browser: the heartbeat files under
`PROFILE_DIR/clients/` hold it up, and entries whose pid died or that go
unrefreshed for `idle_release_s` are removed by the reaper.

The engine order is `search_engines` (default `google`, `ddg`, `brave`, `bing`).
Google is the only engine with a cooldown and an hourly budget, because it is
the only one that challenges; the others are the reason a challenge no longer
fails a call. `searxng` is only tried when `searxng_url` is set, and it is
queried over its JSON API (not through the browser, whose proxy refuses private
addresses by design).

Google-search calls also reserve a slot in the shared SQLite queue. Separate
MCP processes therefore start searches one at a time with a 1.0–2.5 second
randomized gap. `fetch_url` is not put through this Google-search queue.

## Security and scope

- The server exposes no arbitrary page-context JavaScript tool.
- Browser HTTP(S)/WebSocket connections are restricted to public addresses by
  a local validating proxy; private destinations are rejected before connecting.
  Input URLs also reject embedded credentials and recognizable secret patterns.
  This does not classify every possible secret in arbitrary page URLs/content.
  `google_search` queries pass the same gate before any engine (including a
  configured SearXNG host) sees them.
- Downloads are denied on every browser session, and the HTTP cache is bounded,
  so a `Content-Disposition: attachment` response cannot fill the disk.
- Each server owns and cleans up its Chrome and platform display process groups.
  A process attached to a shared owner's browser owns neither: it starts no
  Chrome/Xvfb and its shutdown removes only its own heartbeat file.
- Sharing trusts `PROFILE_DIR/.browser.json` only after validating that it is a
  private (0600), same-user regular file, that the recorded owner pid still has
  the recorded birth identity, and that the endpoint is a loopback CDP websocket
  that answers `Browser.getVersion`. The browser-to-network policy is unchanged:
  every attached client's page traffic goes through the owner's mandatory
  public-network proxy.
- Linux Chrome is forced onto its private X11 display, which is started with a
  per-display MIT-MAGIC-COOKIE-1 file (`XAUTHORITY`); without `xauth` the server
  fails closed instead of starting a display any local user could read. macOS
  Chrome uses a separate temporary profile and native/headless backend without
  touching the user's normal Chrome profile.
- The throwaway profile directory is created atomically with mode 0700, and a
  pre-existing directory that is not private and owned by this user is refused.
- A discovered Chrome/Chromium binary is only executed when it is a regular file
  owned by this user or root and not group/world-writable; `CW_CHROME` is
  validated the same way.
- Rendering as root is refused unless `CW_ALLOW_ROOT=1`, because it would
  require `--no-sandbox`.
- Known boundary: the owner's CDP endpoint listens on `127.0.0.1`, which any
  local user on the host can also reach while the browser runs (as with the
  previous per-process ports). Chrome's `--remote-debugging-pipe` transport is
  the follow-up that removes the listener; see `docs/security-review.md` (F4).
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
