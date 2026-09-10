"""Session-wide test isolation.

The server resolves its Chrome profile at import time, and the default profile is
now persistent (under the user cache dir) so cookies survive between runs. Tests
must never touch that real profile, nor the live MCP server that owns its lock,
so the profile, lock and shared rate-limit state are pinned to a throwaway
directory *before* any test module imports ``chrome_web_mcp.server``.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_SANDBOX = Path(tempfile.mkdtemp(prefix="chrome-web-tests-"))

os.environ["CW_PROFILE_DIR"] = str(_SANDBOX / "profile")
os.environ["CW_LOCK_PATH"] = str(_SANDBOX / "profile" / ".instance.lock")
os.environ["CW_RATE_LIMIT_DB"] = str(_SANDBOX / "rate-limit.sqlite3")

atexit.register(lambda: shutil.rmtree(_SANDBOX, ignore_errors=True))
