"""Deterministic tests for the optional Chrome for Testing bootstrap script."""

from __future__ import annotations

import importlib.util
import os
import stat
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


def _write_member(archive: Path, name: str, mode: int, payload: str = "x") -> None:
    with zipfile.ZipFile(archive, "a") as bundle:
        info = zipfile.ZipInfo(name)
        info.external_attr = mode << 16
        bundle.writestr(info, payload)


def test_safe_extract_restores_exec_bits(tmp_path):
    """zipfile drops Unix modes, and Chrome cannot spawn its GPU, renderer and
    crashpad helpers without the exec bit ("GPU process isn't usable")."""
    archive = tmp_path / "chrome.zip"
    _write_member(archive, "chrome-mac-arm64/App/Contents/MacOS/helper", 0o100755, "binary")
    _write_member(archive, "chrome-mac-arm64/App/Contents/Resources/data", 0o100644, "data")
    destination = tmp_path / "out"
    destination.mkdir()

    installer._safe_extract(archive, destination)

    helper = destination / "chrome-mac-arm64/App/Contents/MacOS/helper"
    data = destination / "chrome-mac-arm64/App/Contents/Resources/data"
    assert stat.S_IMODE(helper.stat().st_mode) == 0o755
    assert os.access(helper, os.X_OK)
    assert stat.S_IMODE(data.stat().st_mode) == 0o644


def test_safe_extract_recreates_framework_symlinks(tmp_path):
    """The macOS framework reaches its Helpers through Versions/Current."""
    archive = tmp_path / "chrome.zip"
    _write_member(archive, "chrome-mac-arm64/App/Framework/Versions/Current", 0o120777, "155.0.8059.79")
    destination = tmp_path / "out"
    destination.mkdir()

    installer._safe_extract(archive, destination)

    link = destination / "chrome-mac-arm64/App/Framework/Versions/Current"
    assert link.is_symlink()
    assert os.readlink(link) == "155.0.8059.79"


def test_safe_extract_rejects_escaping_symlink(tmp_path):
    archive = tmp_path / "chrome.zip"
    _write_member(archive, "chrome-mac-arm64/App/escape", 0o120777, "../../../../etc/passwd")
    destination = tmp_path / "out"
    destination.mkdir()

    with pytest.raises(RuntimeError, match="unsafe symlink"):
        installer._safe_extract(archive, destination)
    assert not (destination / "chrome-mac-arm64/App/escape").exists()
