"""Owned-process teardown and identity checks.

This module owns the process-lifecycle primitives used by the browser runtime and
the startup orphan sweep: ``_process_identity`` captures a same-user process's
birth identity (pid + start time + command) through ``platform_runtime`` so a
reused PID is never signalled, and ``_terminate_owned_process`` stops an owned
process group without ever raising during teardown (macOS reports EPERM while a
crashed child is reaped). ``server`` re-exports both names and keeps
``_stop_recorded_process``, which must observe ``server._process_identity``.
"""

from __future__ import annotations

import os
import signal
import subprocess

from chrome_web_mcp import platform_runtime


def _terminate_owned_process(proc: subprocess.Popen | None, timeout: float) -> None:
    """Stop an owned process group without ever raising during teardown.

    macOS reports EPERM from killpg while a crashed child is being reaped; a
    cleanup path that raised there would mask the original failure.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=timeout)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
            pass


def _process_identity(pid: int) -> dict:
    """Identify a same-user process without relying on a reusable PID alone."""
    return platform_runtime.process_identity(pid)
