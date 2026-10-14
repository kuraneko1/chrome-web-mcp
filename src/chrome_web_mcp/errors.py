"""Exception types shared by the browser, search and MCP layers.

``CaptchaRequired`` marks an interactive Google/engine challenge that the caller
must surface to the client instead of retrying blindly; ``SearchFailed`` carries
the per-engine attempt log when every configured engine failed. Both are defined
here (rather than in the whole-server module) so the CDP layer can import them
without a cycle; ``server`` re-exports them because tests and MCP handlers catch
them as ``server.CaptchaRequired`` / ``server.SearchFailed``.
"""

from __future__ import annotations


class CaptchaRequired(RuntimeError):
    """The Google page requires a human interaction before search can continue."""


class SearchFailed(RuntimeError):
    """Every configured engine failed; carries the per-engine attempt log."""

    def __init__(self, message: str, attempts: list[dict], *, captcha: bool = False) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.captcha = captcha
