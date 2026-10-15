# chrome-web-mcp Configuration

`config.json` must contain valid JSON. JSON does not support comments, so the
explanations for each setting are in this file instead of inside the JSON.

Copy both files to the user's configuration directory:

```bash
mkdir -p ~/.config/chrome-web-mcp
cp examples/config.json examples/config.md ~/.config/chrome-web-mcp/
```

The server automatically reads `~/.config/chrome-web-mcp/config.json`. Use the
`CW_CONFIG` environment variable when the file is somewhere else.

The file is per user. You do not need to change the server code for a
different language or region.

## Headless environments

If you run on a headless server, CI runner, SSH session without X forwarding,
or any machine without a desktop display, set:

```json
{ "show_browser": false }
```

On Linux this uses hidden Xvfb instead of Xephyr. On macOS it uses native
`--headless=new` Chrome. The built-in default is `true` for desktop use, so
explicitly set `false` on headless machines and restart the MCP client after
changing the file. Docker already sets hidden Xvfb inside the container.

## Settings

### `show_browser`

Controls whether the browser window is visible:

- `true`: visible browser. Linux uses Xephyr; macOS uses a normal native Chrome window.
- `false`: hidden browser. Linux uses Xvfb; macOS uses `--headless=new`.

On Linux, visible mode requires a desktop `DISPLAY` and Xephyr. On macOS no
`DISPLAY` or X11 component is required. The built-in default is `true`, so use
`false` when you do not want a window.

For the Docker version, `show_browser` is not read from the host config file.
The Docker image runs hidden Xvfb by default. If it reports
`captcha_required: true`, the challenge is inside the Docker container, not in
the local browser. Run that Docker MCP with `CW_DISPLAY_MODE=xephyr`, the host
`DISPLAY`, the X11 socket, and the matching Xauthority file to display and
solve the same container browser. See the Docker section in `README.md`.

### `hl` and `gl`

These are Google search parameters, not passwords or model settings. They are
independent settings:

- `hl`: Google interface language. `ja` means Japanese; `en` means English.
- `gl`: Google result region. `jp` means Japan; `us` means the United States.

Common combinations are:

```json
{ "hl": "ja", "gl": "jp" }
```

Japanese interface and Japan-oriented results. For English/US-oriented
results, use:

```json
{ "hl": "en", "gl": "us" }
```

If you omit these keys, the built-in defaults (`ja`/`jp`) or the values in
your config file are used. If a tool call explicitly includes `hl` or `gl`,
that call's values take priority over the config file.

### `limit`

Default maximum number of results returned by `google_search` when the tool
call does not provide `limit`. Allowed range: 1-20.

### `char_limit`

Default maximum number of characters returned by `fetch_url` when the tool call
does not provide `char_limit`. Allowed range: 100-200000.

### `format`

Default output format for `fetch_url`:

- `markdown`: readable markdown with boilerplate removed. Recommended.
- `text`: full rendered page text when markdown extraction looks incomplete.
- `links`: readable text plus follow-up link targets.

### `min_delay` and `max_delay`

Minimum and maximum number of seconds between Google search starts. The actual
delay is randomized in this range and shared across server processes to reduce
CAPTCHA risk. `max_delay` must be greater than or equal to `min_delay`.

## CAPTCHA-resilience settings

These keys are optional; the defaults below are what the server already uses.

### `profile`

- `persistent` (default): one warm Chrome profile under
  `~/.cache/chrome-web-mcp/profile`. Cookies, consent state, and the cache
  survive between runs, so Google sees a returning browser instead of a new one.
  Exactly one server process owns the browser; the owner publishes its CDP
  endpoint in `.browser.json` (0600) and every later process attaches to that
  browser instead of starting its own, so N sessions cost one ~600 MB browser
  tree. An attached process starts no Chrome/Xvfb, keeps its own tabs, and
  cannot kill the owner's processes when it exits. If that registry is missing
  or unusable, a process that finds the profile busy falls back to its own
  throwaway profile and reports `profile_shared_fallback: true` from
  `health_check` (`browser_role` / `owner_pid` / `clients` describe the sharing
  state). To reset it, stop the server and delete that directory.
- `ephemeral`: the older behavior — a throwaway profile per process under
  `/tmp/chrome-web-v2-profile/<pid>`, removed on exit.

### `search_engines`

The engines to try, in order. Default:
`["google", "ddg", "brave", "bing"]`. When Google shows a CAPTCHA, the next
engine answers the same query, so a search still returns results. The response
reports `engine` (who answered), `attempts` (each engine with its outcome), and
`captcha_avoided`.

### `searxng_url`

Base URL of a self-hosted SearXNG, e.g. `"https://searxng.example.com"`. The
`searxng` engine is skipped until this is set. It is queried over SearXNG's JSON
API, so the instance must allow `format=json`. The value comes from your own
config or environment, never from model output, so a private address such as
`http://10.0.0.1:4000` or `http://127.0.0.1:8080` is allowed - that is how a
self-hosted instance is normally reached. Redirects are not followed, so a
misconfigured instance cannot bounce the request to another host; point this at
the final URL instead.

### `human_input`

- `true` (default): open the Google home page, type the query into the search
  box, and press Enter.
- `false`: navigate straight to `/search?q=...`. Use this if typing misbehaves
  in your environment; the direct path is also the automatic fallback.

### `max_searches_per_hour`, `captcha_cooldown_s`, `captcha_cooldown_max_s`

Google-only self-throttling, shared across server processes: after
`max_searches_per_hour` searches (default `60`, `0` disables the budget) Google
is skipped and the fallback engines answer. After a CAPTCHA, Google is skipped
for `captcha_cooldown_s` seconds (default `300`), doubling for each consecutive
CAPTCHA up to `captcha_cooldown_max_s` (default `1800`). A successful Google
search clears the cooldown.

### `spoof_platform`

- `native` (default): leave the UA, `navigator.platform`, and client hints as
  Chrome produces them on Linux. They already agree with each other, which is
  what matters for bot detection.
- `windows`: apply a Windows UA together with a matching `navigator.platform`
  and user-agent client hints, so no single signal contradicts another.

### `disable_gpu`

- `true` (default): pass `--disable-gpu` (stable software rendering on headless
  hosts and CI).
- `false`: let Chrome use the host GPU. Note that on Xvfb/Xephyr hosts without a
  usable GPU path Chrome may expose no WebGL context either way; the WebGL
  vendor/renderer strings are normalized when WebGL is available.

## Idle release

### `idle_release_s`

Seconds without a browser tool call (`google_search` / `fetch_url`) before the
server releases its Chrome + display backend, freeing the browser tree's memory
while the MCP server process keeps running. The next tool call starts the
browser again transparently.

- Default `300` (five minutes).
- `0` disables idle release and restores the pre-idle-release behavior (the
  browser stays up for the whole server session).
- Any non-negative number of seconds is accepted; non-numeric or negative
  values warn on stderr and keep the default.

The owner never releases the browser while another process is attached to it:
attached clients refresh `PROFILE_DIR/clients/<pid>.json` on every browser tool
call, and the reaper holds off until no heartbeat is younger than
`idle_release_s` (heartbeats whose process died or that are malformed are
removed). With no attached clients the behavior is exactly the single-process
one described below.

Measured on a Debian host (Chrome 155 + Xvfb): the released browser tree is
about **600 MB** RSS (Chrome, renderers, GPU, network, Xvfb), a cold start
(Xvfb + Chrome until CDP is ready) takes **0.88 s**, and cleanup takes
**0.62 s**. So an idle release costs roughly 0.9 s on the next call and gives
the ~600 MB back in the meantime. The persistent profile is never deleted by
idle release — only the throwaway ephemeral profile is removed, on exit.

## Precedence and restart

When the same setting is specified more than once, the order is:

1. Tool-call argument
2. Environment variable (`CW_DISPLAY_MODE`, `CW_MIN_DELAY`, `CW_MAX_DELAY`, or
   `CW_IDLE_RELEASE`)
3. `config.json`
4. Built-in default

Some security switches exist only as environment variables, because they are not
per-user preferences:

- `CW_ALLOW_ROOT=1` — allow rendering as root. Unset by default: the server
  refuses to start Chrome as root, since it renders attacker-controlled HTML and
  would have to pass `--no-sandbox`. Run as an unprivileged user instead; the
  Docker image already uses UID 1000.

Restart the MCP client after changing the file. Values are loaded when the
server process starts, not on every tool call.
