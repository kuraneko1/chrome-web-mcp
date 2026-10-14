# Security review and hardening plan

Scope: `chrome-web-mcp` at `284bf1f` (Linux + macOS browser runtime,
proxy-enforced network policy, three MCP tools: `google_search`, `fetch_url`,
`health_check`). This document is the deliverable of the `security/hardening`
branch (based on `4c8cd04`, the module split); each finding below carries the
disposition it reached there, with the regression test that locks it in.

Method: manual read of `server.py`, `network.py`, `engines.py`
and `platform_runtime.py`, plus targeted probes of the network policy and the
Chrome launch path. Findings are ordered by what an attacker gains.

## Findings

### F1 — ordinary SERP text is treated as a bot challenge (medium)
`_is_google_challenge()` and `engines.looks_blocked()` scan the whole lowercased
`document.body.innerText` for phrases such as "captcha" and "not a robot". A
normal result page for the query `captcha` therefore marks Google as challenged,
which persists an escalating cooldown (300 s → 1800 s) in the shared SQLite
state; every later search silently falls back to another engine, and with
`xpra` enabled the window is pushed at the user. A page can also trigger this on
purpose by ranking a snippet that contains the phrase.

Fix: decide structurally (challenge form/iframe/reCAPTCHA containers and the
`/sorry` path) instead of by free text, and clamp the persisted strike counter
so the exponential backoff cannot overflow.

Disposition: **fixed**. Both call sites now evaluate
`cdp._PAGE_CHALLENGE_PROBE_JS`, a structural probe for the challenge UI
(`form[action*="/sorry"]`, `#captcha-form`, `#challenge-form`,
`div.g-recaptcha`, reCAPTCHA/challenge iframes), and pass only that container's
own text to the classifier; page body text is never inspected, so a result page
about CAPTCHAs can no longer trip it. The phrase list inside the two classifiers
is retained but can only disambiguate text that already came from a challenge
container. `SearchEngineState.note_challenge()` clamps the persisted strike
count to `_MAX_STRIKES = 32`, so a corrupted or hostile row can no longer make
`2 ** (strikes - 1)` build an unbounded integer.
Tests: `test_google_results_ask_the_structural_probe_instead_of_the_body`,
`test_structural_probe_hit_raises_captcha_required`,
`test_hostile_strike_count_is_clamped`.

### F2 — secret filter is an incomplete prefix allowlist and never checks queries (medium)
`_SECRET_RE` covers `sk-`, `ghp_`, `AIza`, … but misses AWS (`AKIA`/`ASIA`),
`gho_/ghs_/ghu_/ghr_`, Google OAuth (`ya29.`), JWTs, `sk_live_`, PEM blocks and
any private token. It is applied to `fetch_url` URLs only; `google_search` sends
its query to the configured engines unchecked, so an agent that is talked into
"search for this" leaks the secret to a third party, and a secret in a hostname
leaks through DNS alone.

Fix: extend the pattern, add the process's own secret-looking environment values
as literal needles (raw and percent-decoded), and apply the same gate to search
queries.

Disposition: **fixed**. `_SECRET_RE` now covers AWS access key IDs,
`gho_/ghs_/ghu_/ghr_`, `ya29.`, `sk_live_`, PEM headers and JWT shape (in
addition to the existing prefixes); `_env_secret_needles()` turns the values of
environment variables whose *name* matches
`(?i)(key|token|secret|passwd|password|credential)` and is at least 16
characters long into literal needles; and `_validate_search_query()` applies the
same raw/percent-decoded check to a `google_search` query before any engine
(including a configured SearXNG host) sees it. Errors name the class of problem
("credential-like value") and never echo the offending value.
Tests: `test_secret_gate_covers_new_credential_shapes`,
`test_secret_gate_checks_percent_decoded_values`,
`test_secret_gate_uses_own_environment_values_as_needles`,
`test_secret_gate_never_echoes_the_offending_value`,
`test_google_search_tool_blocks_queries_that_carry_secrets`.

### F3 — `fetch_url` can make Chrome download arbitrary files (medium)
No download policy is set (`grep -n download src/chrome_web_mcp/*.py` is empty),
so a `Content-Disposition: attachment` response is written to the user's
`~/Downloads` (or the profile dir) by the browser process, and closing the tab
does not cancel it. `_wait_ready()` can never succeed on a download document, so
the tool errors after ~25 s while the transfer continues; repeating it fills the
disk. The HTTP cache is unbounded for the same reason.

Fix: `Browser.setDownloadBehavior` = `deny` on the browser session and a bounded
`--disk-cache-size`.

Disposition: **fixed**. Every browser-level CDP session calls
`_deny_downloads()`, which sends `Browser.setDownloadBehavior` with
`{"behavior": "deny"}` and ignores an unsupported-method error, and
`_chrome_command()` passes `--disk-cache-size=67108864` (64 MiB).
Tests: `test_download_policy_is_denied_on_the_browser_session`,
`test_unsupported_download_policy_is_not_fatal`,
`test_fetch_page_sets_the_download_policy_before_navigating`,
`test_chrome_command_bounds_the_disk_cache`.

### F4 — unauthenticated loopback DevTools listener (medium; deferred)
Chrome listens on `--remote-debugging-address=127.0.0.1 --remote-debugging-port=0`.
Any local user can find the port in `/proc/net/tcp`, connect (a non-browser
client sends no `Origin`, so the default Origin check does not apply) and then
read `file:///home/victim/.ssh/id_rsa` or the victim's cookies. The random port
is not a mitigation.

Disposition: **deferred to the shared-browser work**, where a broker owns the CDP
connection and can switch to `--remote-debugging-pipe` (process-private fds) so
that no TCP listener exists at all. Until then this is a local-user boundary,
documented here rather than silently ignored. The reason is also recorded in a
comment at the port choice in `BrowserRuntime._chrome_command()`.

### F5 — private X display has no access control (medium)
`Xvfb`/`Xephyr` are started with `-nolisten tcp` but no `-auth`, and
`/tmp/.X11-unix/X<n>` is world-connectable. Another local user can screenshot
the browser (search queries, fetched pages) and, with XTEST, type
`file:///home/victim/.ssh/id_rsa` into the omnibox and screenshot the result.
During CAPTCHA solving a grabbed keyboard can capture what the user types.

Fix: create a 0600 `XAUTHORITY` cookie per display, pass `-auth` to Xvfb and
Xephyr, and export `XAUTHORITY` for Chrome.

Disposition: **fixed**. Each private display gets its own 0600
MIT-MAGIC-COOKIE-1 file (`BrowserRuntime._create_xauth_file()`), is started with
`-auth <file>`, and Chrome, the readiness check and the Xpra shadow server
receive `XAUTHORITY`. The file is removed with the display. The display number is
chosen before launch (`_free_display_number()`, as `xvfb-run` does) instead of
with `-displayfd`, because the cookie must exist *before* the server starts: an
empty auth file leaves the display open until the first entry appears. Without
`xauth` the server fails closed instead of starting an unauthenticated display.
The Docker image now installs `xauth`, which `--no-install-recommends` previously
dropped (it is only a Recommends of `xvfb`).
Tests: `test_xvfb_gets_a_private_cookie_file_and_chrome_uses_it`,
`test_start_xvfb_fails_closed_without_xauth`,
`test_private_display_socket_is_not_open_to_other_users` (real Xvfb/xauth pair).

### F6 — `--no-sandbox` when running as root (medium, high impact)
`_chrome_command()` adds `--no-sandbox` whenever `euid == 0`. The server renders
attacker-controlled HTML, so a renderer escape is immediate host root.

Fix: fail closed as root and require an explicit opt-in (`CW_ALLOW_ROOT=1`) that
re-adds `--no-sandbox`; report `running_as_root` in `health_check`. The Docker
image already runs as UID 1000, so this only affects explicitly root-run
containers.

Disposition: **fixed**. `_chrome_command()` raises a clear error for root unless
`_root_allowed()` (`CW_ALLOW_ROOT=1`), which restores `--no-sandbox`;
`health_check` reports `running_as_root`.
Tests: `test_refuses_to_render_as_root_without_the_opt_in`,
`test_root_opt_in_restores_no_sandbox`,
`test_health_check_reports_running_as_root`.

### F7 — predictable profile directory in shared `/tmp` (low-medium)
The ephemeral profile is `/tmp/chrome-web-v2-profile/<pid>`; the intermediate
directory is created with the process umask and is not sticky, so any local user
can pre-create it, then rename/replace the victim's per-PID directory and plant
`Default/Preferences`/`Local State` (and later read the profile, whose cookie
key is trivially recoverable because Chrome runs with
`--password-store=basic`). Bounds verified: the leaf is 0700 before Chrome writes
and `shutil.rmtree` refuses symlinked paths, so this is not arbitrary deletion.

Fix: use `tempfile.mkdtemp()` (atomic, 0700) under a per-user base and refuse a
pre-existing base that is not owned by us or is group/world-writable.

Disposition: **fixed**. `_ephemeral_profile_dir()` creates the base and the
per-pid leaf with `os.mkdir(path, 0o700)` (atomic) and refuses a pre-existing
entry that is not a real directory owned by this user with no group/world access
(`_ensure_private_dir()`, using `lstat` so a symlink is refused rather than
followed). The persistent profile under the user cache is unchanged.
Tests: `test_ephemeral_profile_is_created_atomically_0700`,
`test_ephemeral_profile_refuses_an_open_or_foreign_base`,
`test_ephemeral_profile_refuses_a_symlinked_base`,
`test_ephemeral_profile_refuses_a_precreated_pid_directory`.

### F8 — Chrome auto-discovery can execute a planted binary (low)
On macOS, `platform_runtime.chrome_candidates()` prefers
`<project>/.local-chrome/**`, a git-ignored path: anyone who can write into the
checkout gets code execution as the MCP user, including from a bare
`health_check` (`chrome --version`). On Linux, `shutil.which()` candidates let a
writable `PATH` entry shadow `/usr/bin`. The candidates are executed with
`subprocess.run([binary, "--version"])`.

Fix: before executing a discovered candidate, require it to be owned by the user
or root and not group/world-writable; keep the absolute system paths first.

Disposition: **fixed**. `_trusted_executable()` requires a regular file owned by
this user or root with no group/world write bit; `discover_chrome()` skips an
untrusted candidate (so a trusted system path still wins) and `chrome_version()`
refuses to execute one, which covers `health_check`. Symlinks are followed on
purpose, because `/usr/bin/google-chrome` is normally a symlink chain into
`/opt` and the real file is what runs. `CW_CHROME` keeps working but is validated
the same way, and the candidate order is unchanged (absolute system paths first).
Tests: `test_discover_chrome_skips_a_world_or_group_writable_candidate`,
`test_cw_chrome_is_validated_like_any_candidate`,
`test_chrome_version_refuses_an_untrusted_binary`,
`test_discover_chrome_follows_system_symlinks`.

### F9 — SearXNG requests bypass the URL policy (low)
`_searxng_candidates()` uses `httpx.AsyncClient(follow_redirects=True)` against
`searxng_url` without `_validate_public_url`, so a misconfigured value allows a
blind request to a private address (the body must parse as SearXNG JSON; only
title/url/description surface).

Fix: require an absolute http(s) URL, disable redirects, and keep engine result
URLs behind the normal host policy. The configured base URL stays private-address
friendly on purpose (see the disposition below).

Disposition: **fixed, with a deliberate scope limit.** Redirects are no longer
followed, so a misconfigured or hostile instance cannot bounce the client to
another host, and the value must be an absolute http(s) URL. The *base* URL is
deliberately **not** run through the public-URL policy: it is operator-controlled
(config file or environment, never model output) and a self-hosted instance
normally lives on localhost or a LAN host, so requiring a public address would
break the documented use case (and the README's own `http://10.0.0.1:4000`
example). Engine result URLs still go through the same host policy as every other
search result. Documented in `README.md`, `README.jp.md` and `examples/config.md`.
Tests: `test_searxng_allows_an_operator_configured_private_base_url`,
`test_searxng_rejects_a_non_http_base_url`, `test_searxng_does_not_follow_redirects`,
`test_searxng_redirect_response_is_not_used`.

### F10 — quadratic join in the markdown fallback (low)
`_FETCH_MD_JS` joins the accumulated parts inside the loop (`parts.join("\n\n")`)
on every iteration. It is only reached when both `trafilatura` and `html2text`
return nothing, so it is hard to trigger, but it is a one-line fix.

Disposition: **fixed**. The loop keeps a running `total` of the joined length and
breaks at the same 300000-character cap; the only `parts.join` left is after the
loop.
Test: `test_markdown_fallback_accumulates_length_linearly`.

## Mitigations verified as effective (not re-litigated)

* **Egress control.** All browser HTTP(S)/WS traffic goes through
  `PublicNetworkProxy`: CONNECT parses the authority from `"//" + path` and
  rejects userinfo/path/query/fragment; non-CONNECT requires scheme `http`;
  `public_addresses()` resolves once and rejects the whole answer if any address
  is non-public, then connects to the numeric sockaddr — so there is no second
  DNS lookup and no rebinding window. `0.0.0.0`, `127/8`, `10/8`, `169.254/16`,
  `172.16/12`, `192.168/16`, `100.64/10`, `198.18/15`, `240/4`, decimal/octal/
  hex/`127.1` host forms, IPv4-mapped loopback and the metadata names are all
  rejected. `rbufsize = 0` prevents parser over-read into a tunnel, and the
  request line is rebuilt origin-form with a recomputed `Host`.
* **Browser-wide proxying.** `--proxy-server` plus `--proxy-bypass-list=<-loopback>`
  and `--disable-quic` mean redirects, subresources, workers, service workers and
  `wss://` all re-enter the proxy; the final URL is re-validated.
* **Page → CDP is blocked twice**: loopback goes to the proxy (blocked), and no
  `--remote-allow-origins` is passed, so Chrome's Origin check rejects page
  WebSockets.
* **`file://` is unreachable from a page** (Chrome refuses http→file navigation;
  requested and final URLs must be http(s)).
* **No command injection**: every subprocess call is argv-list, no `shell=True`,
  `os.system`, `os.popen` or `eval` in `src/`; display numbers are digit-checked.
* **Kill safety**: `_stop_recorded_process()` requires pid > 1, same-uid ownership,
  matching birth identity and `getpgid(pid) == pid` before `killpg`; the sweep
  skips symlinks and foreign-uid entries; process records are written atomically.
* **Resource bounds per call**: 2 MiB CDP messages, 16 Mi character extraction cap,
  1.8 MiB engine HTML cap, title/description caps, `limit` 1–20, query ≤ 512,
  URL ≤ 2048, `char_limit` ≤ 200000, linear `_smart_cut`, no nested regex
  quantifiers; lxml parses browser-normalised `outerHTML` (no DTD/entity
  expansion, so no XXE or billion-laughs).

## Open questions that need a live test (not findings)

1. Cleartext `ws://` through the HTTP proxy — the proxy handles
   `Upgrade: websocket`, which implies Chromium routes plain `ws://` there.
   Confirm with a loopback WS listener and a fetched page that opens it.
2. `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` effectiveness is
   version-dependent; confirm with an `RTCPeerConnection` against a public STUN
   server and `tcpdump` on the host.
3. `<link rel=dns-prefetch>`/preconnect make Chrome resolve names outside the
   proxy — a blind DNS channel, the one thing the proxy does not see.
