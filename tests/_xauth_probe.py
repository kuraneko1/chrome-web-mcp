"""TEMPORARY CI diagnostic - deleted before merge.

Launches Chrome on the authenticated private display with its stderr captured, so
the SIGABRT (-6) seen on the GitHub Linux runner can be read.
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ["CW_DISPLAY_MODE"] = "xvfb"
os.environ["CW_PROFILE_DIR"] = "/tmp/xauth-probe-profile"

from chrome_web_mcp import platform_runtime, server  # noqa: E402


def main() -> int:
    print("platform:", platform_runtime.platform_description())
    print("xauth:", server.shutil.which("xauth"))
    print("TMPDIR:", os.environ.get("TMPDIR"), "DISPLAY:", os.environ.get("DISPLAY"))

    log_path = os.path.join(tempfile.gettempdir(), "chrome-probe.log")
    real_popen = server.subprocess.Popen

    def patched_popen(*args, **kwargs):
        handle = open(log_path, "wb")
        kwargs["stdout"] = handle
        kwargs["stderr"] = handle
        return real_popen(*args, **kwargs)

    server.subprocess.Popen = patched_popen
    runtime = server.BrowserRuntime()
    try:
        ws = runtime.ensure()
        print("ENSURE OK", ws, "display:", runtime.display, "xauth:", runtime.xauth_file)
        print("command:", " ".join(runtime._chrome_command()))
    except Exception as exc:  # noqa: BLE001
        print("ENSURE FAILED:", type(exc).__name__, exc)
        print("display:", runtime.display, "xauth:", runtime.xauth_file)
        print("chrome poll:", runtime.chrome.poll() if runtime.chrome else None)
    finally:
        try:
            with open(log_path, "rb") as handle:
                tail = handle.read()[-4000:].decode("utf-8", "replace")
            print("chrome stderr tail:\n" + tail)
        except OSError as exc:
            print("no chrome log:", exc)
        try:
            runtime.cleanup()
        except Exception as exc:  # noqa: BLE001
            print("cleanup raised:", type(exc).__name__, exc)
    return 0


sys.exit(main())
