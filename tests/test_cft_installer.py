"""Deterministic tests for the optional Chrome for Testing bootstrap script."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-chrome-for-testing.py"
SPEC = importlib.util.spec_from_file_location("chrome_web_mcp_cft_installer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.mark.parametrize(
    "machine,expected",
    [("arm64", "mac-arm64"), ("aarch64", "mac-arm64"), ("x86_64", "mac-x64")],
)
def test_cft_platform_mapping(monkeypatch, machine, expected):
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(installer.platform, "machine", lambda: machine)
    assert installer._cft_platform() == expected


def test_selects_requested_channel_and_platform():
    payload = {
        "channels": {
            "Stable": {
                "version": "999.1.2.3",
                "downloads": {
                    "chrome": [
                        {"platform": "mac-x64", "url": "https://example.test/x64.zip"},
                        {"platform": "mac-arm64", "url": "https://example.test/arm64.zip"},
                    ]
                },
            }
        }
    }
    assert installer._select_download(payload, "Stable", "mac-arm64") == (
        "999.1.2.3",
        "https://example.test/arm64.zip",
    )


def test_safe_extract_rejects_path_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.txt", "nope")
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(RuntimeError, match="unsafe path"):
        installer._safe_extract(archive, destination)
    assert not (tmp_path / "escape.txt").exists()
