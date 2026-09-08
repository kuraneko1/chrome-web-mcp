# Contributing to chrome-web-mcp

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
```

Requires Python 3.10+ and Chrome/Chromium. Linux hidden-browser tests require
Xvfb; macOS uses native/headless Chrome and does not require X11.

## Verification

```bash
pytest -q
pytest -q -m 'not live'  # deterministic tests; browser fixtures use local responses
python -m build
```

- `pytest -q` runs the full suite.
- `pytest -q -m 'not live'` runs only deterministic tests (synthetic
  responses and local fixtures, no Google dependency).
- `python -m build` verifies the wheel/sdist builds.

The end-to-end tests exercise the real stdio MCP handshake, `tools/list`,
Google search, public URL fetching, and shutdown cleanup. They require Chrome
and network access; Linux additionally requires Xvfb for hidden mode. macOS
contributors can install an isolated browser with
`python scripts/install-chrome-for-testing.py`. Tests marked `live` contact
Google and can fail if the network is unavailable or Google requires a CAPTCHA.
Browser security regression tests use synthetic responses and local fixtures,
without depending on Google.

E2E tests use a sandbox dir (`.e2e-sandbox` by default, overridable via
`CW_E2E_SANDBOX`) so test Chrome profiles/locks do not collide with a live
server holding the default profile lock.
