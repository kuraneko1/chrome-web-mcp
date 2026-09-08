# macOS setup

`chrome-web-mcp` can run directly on macOS without Xvfb, Xephyr, X11, or a
system-wide Chrome installation.

## Requirements

- macOS
- Python 3.10+
- Apple Silicon or Intel Mac

The normal macOS browser modes are:

- `show_browser: true` -> native visible Chrome window
- `show_browser: false` -> Chrome `--headless=new`

## Quickstart with project-local Chrome for Testing

Create a virtual environment and install the package:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
```

Optionally install Google's official Chrome for Testing build into this
checkout:

```bash
python scripts/install-chrome-for-testing.py
```

The script downloads the current Stable Chrome for Testing build for the host
architecture and stores it under `.local-chrome/`. The directory is git-ignored
and the MCP server never downloads a browser automatically.

After that, run:

```bash
chrome-web-mcp
```

No `CW_CHROME` value is needed when the project-local browser is present.

## Browser discovery order

On macOS the server searches in this order:

1. `CW_CHROME`
2. project-local `.local-chrome` Chrome for Testing
3. Google Chrome stable in `/Applications` or `~/Applications`
4. PATH `google-chrome`
5. Chromium app/PATH locations
6. Google Chrome Beta / Canary

An invalid explicit `CW_CHROME` value fails closed instead of silently falling
back to another browser.

## MCP client example

For a local checkout, point any stdio MCP client at the console script in the
virtual environment:

```json
{
  "mcpServers": {
    "chrome-web": {
      "command": "/absolute/path/to/chrome-web-mcp/.venv/bin/chrome-web-mcp"
    }
  }
}
```

## Profile isolation

The server does not use the normal user Chrome profile by default. Each MCP
server process receives a temporary per-process profile under the system temp
directory. Do not point `CW_PROFILE_DIR` at the normal Chrome profile unless
that is explicitly intended.

## CAPTCHA behavior

The server detects Google CAPTCHA pages but does not bypass them.

- visible mode: solve the challenge in the native Chrome window, then retry
- headless mode: the tool returns `captcha_required: true`; wait or restart in
  visible mode

## GPU behavior

The macOS runtime does not pass Linux's `--disable-gpu` flag. Chrome is allowed
to select its normal macOS graphics backend. No custom Metal shader or kernel is
required by this project.

## Testing

Deterministic tests, including local browser security fixtures:

```bash
CW_DISPLAY_MODE=headless python -m pytest -q -m 'not live'
```

Tests marked `live` contact Google and can legitimately encounter a CAPTCHA.
