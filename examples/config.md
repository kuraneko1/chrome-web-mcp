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

## Precedence and restart

When the same setting is specified more than once, the order is:

1. Tool-call argument
2. Environment variable (`CW_DISPLAY_MODE`, `CW_MIN_DELAY`, or
   `CW_MAX_DELAY`)
3. `config.json`
4. Built-in default

Restart the MCP client after changing the file. Values are loaded when the
server process starts, not on every tool call.
