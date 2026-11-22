"""Clipboard access on native Linux and WSL, with simple fallbacks."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


class ClipboardUnavailable(RuntimeError):
    """No usable clipboard command was found."""


def is_wsl() -> bool:
    """Detect WSL through /proc/version."""
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "microsoft" in version.lower()


def _run(command: list[str], *, data: bytes | None = None) -> bytes:
    return subprocess.run(
        command,
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout


def read_clipboard() -> str:
    """Read the clipboard as text.

    Environment overrides MCA_CLIPBOARD_READ_COMMAND and
    MCA_CLIPBOARD_WRITE_COMMAND accept a command with {path} replaced by a
    temporary file path. This makes the behaviour testable and configurable.
    """
    override = os.environ.get("MCA_CLIPBOARD_READ_COMMAND")
    if override:
        return _read_via_command(override)

    if is_wsl():
        script = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Get-Clipboard -Raw"
        )
        raw = _run(["powershell.exe", "-NoProfile", "-Command", script])
        return raw.decode("utf-8", errors="replace").strip("\r\n\ufeff")

    if shutil.which("wl-paste"):
        raw = _run(["wl-paste", "--no-newline"])
        return raw.decode("utf-8", errors="replace")
    if shutil.which("xclip"):
        raw = _run(["xclip", "-selection", "clipboard", "-o"])
        return raw.decode("utf-8", errors="replace")
    raise ClipboardUnavailable(
        "No clipboard reader found. On WSL install powershell.exe/clip.exe; "
        "on Linux install wl-copy/wl-paste or xclip. "
        "Alternatively pass --file response.md to loop.py."
    )


def write_clipboard(text: str) -> bool:
    """Put text on the clipboard. Return True on success, False if unavailable."""
    override = os.environ.get("MCA_CLIPBOARD_WRITE_COMMAND")
    if override:
        return _write_via_command(override, text)

    try:
        if is_wsl():
            # The BOM tells clip.exe the input is UTF-16LE, so accents survive.
            _run(["clip.exe"], data=("\ufeff" + text).encode("utf-16le"))
            return True
        if shutil.which("wl-copy"):
            _run(["wl-copy"], data=text.encode("utf-8"))
            return True
        if shutil.which("xclip"):
            _run(["xclip", "-selection", "clipboard"], data=text.encode("utf-8"))
            return True
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def _temporary_path() -> Path:
    descriptor, name = tempfile.mkstemp(prefix="mca-clip-")
    os.close(descriptor)
    return Path(name)


def _read_via_command(template: str) -> str:
    handle = _temporary_path()
    command = shlex.split(template.replace("{path}", str(handle)))
    try:
        _run(command)
        return handle.read_text(encoding="utf-8")
    finally:
        handle.unlink(missing_ok=True)


def _write_via_command(template: str, text: str) -> bool:
    handle = _temporary_path()
    try:
        handle.write_text(text, encoding="utf-8")
        _run(shlex.split(template.replace("{path}", str(handle))))
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        handle.unlink(missing_ok=True)


def find_downloads_dir() -> Path | None:
    """Locate the Windows Downloads folder when running under WSL."""
    if not is_wsl():
        return None
    try:
        raw = _run(["cmd.exe", "/c", "echo %USERNAME%"])
        username = raw.decode("utf-8", errors="replace").strip()
    except (OSError, subprocess.SubprocessError):
        username = os.environ.get("USER", "")
    if not username:
        return None
    candidate = Path(f"/mnt/c/Users/{username}/Downloads")
    return candidate if candidate.is_dir() else None
