#!/usr/bin/env python3
"""Install an official Chrome for Testing build into this checkout.

The browser is intentionally not vendored in git and is never downloaded as a
side effect of starting the MCP server.  Run this script explicitly when a
project-local browser is desired.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


MANIFEST_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)
CHANNELS = ("Stable", "Beta", "Dev", "Canary")


def _cft_platform() -> str:
    if sys.platform != "darwin":
        raise RuntimeError("This installer currently supports macOS only")
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "mac-arm64"
    if machine in {"x86_64", "amd64"}:
        return "mac-x64"
    raise RuntimeError(f"Unsupported macOS architecture: {machine or 'unknown'}")


def _select_download(payload: dict, channel: str, cft_platform: str) -> tuple[str, str]:
    try:
        entry = payload["channels"][channel]
        version = str(entry["version"])
        downloads = entry["downloads"]["chrome"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Chrome for Testing manifest has an unexpected format") from exc

    for item in downloads:
        if item.get("platform") == cft_platform and item.get("url"):
            return version, str(item["url"])
    raise RuntimeError(f"No Chrome for Testing download for {channel}/{cft_platform}")


def _read_manifest(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "chrome-web-mcp-cft-installer"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read Chrome for Testing manifest: {exc}") from exc


def _safe_extract(archive: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        bad_member = bundle.testzip()
        if bad_member:
            raise RuntimeError(f"Downloaded Chrome archive failed ZIP integrity check: {bad_member}")
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(destination_resolved):
                raise RuntimeError("Downloaded Chrome archive contains an unsafe path")
        bundle.extractall(destination)


def install(project_root: Path, channel: str) -> Path:
    project_root = project_root.resolve()
    local_root = project_root / ".local-chrome"
    local_root.mkdir(parents=True, exist_ok=True)

    cft_platform = _cft_platform()
    payload = _read_manifest(MANIFEST_URL)
    version, download_url = _select_download(payload, channel, cft_platform)

    install_dir = local_root / f"chrome-{cft_platform}"
    binary_rel = Path("Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
    binary = install_dir / binary_rel
    version_file = install_dir / ".cft-version"

    if binary.is_file() and version_file.is_file():
        try:
            installed_version = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            installed_version = ""
        if installed_version == version:
            print(f"Chrome for Testing {version} is already installed: {binary}")
            return binary

    with tempfile.TemporaryDirectory(prefix="cft-install-", dir=local_root) as tmp_name:
        tmp = Path(tmp_name)
        archive = tmp / "chrome.zip"
        request = urllib.request.Request(
            download_url,
            headers={"User-Agent": "chrome-web-mcp-cft-installer"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Could not download Chrome for Testing: {exc}") from exc

        extracted = tmp / "extracted"
        extracted.mkdir()
        _safe_extract(archive, extracted)

        extracted_root = extracted / f"chrome-{cft_platform}"
        extracted_binary = extracted_root / binary_rel
        if not extracted_binary.is_file():
            raise RuntimeError("Downloaded Chrome archive did not contain the expected executable")

        if install_dir.exists():
            shutil.rmtree(install_dir)
        shutil.move(str(extracted_root), str(install_dir))

    mode = binary.stat().st_mode
    binary.chmod(mode | stat.S_IXUSR)
    version_file.write_text(version + "\n", encoding="utf-8")
    print(f"Installed Chrome for Testing {version}: {binary}")
    return binary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel",
        choices=CHANNELS,
        default="Stable",
        help="Chrome for Testing channel (default: Stable)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="chrome-web-mcp checkout root (default: repository containing this script)",
    )
    args = parser.parse_args()
    try:
        install(args.root, args.channel)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
