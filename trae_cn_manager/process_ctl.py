"""Cross-platform Trae CN IDE process control.

Windows-focused (per user target), with macOS support and a Linux no-op
fallback so the switching logic can be unit-tested on a headless box.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol

from .config import get_app_data_dir


def _exe_config_path() -> Path:
    return get_app_data_dir() / "trae_cn_exe.json"


def get_trae_cn_exe_path() -> str | None:
    """Stored Trae CN executable path, or auto-scanned common install paths."""
    env = os.environ.get("TCN_TRAE_EXE")
    if env:
        return env
    p = _exe_config_path()
    if p.exists():
        try:
            v = json.loads(p.read_text(encoding="utf-8")).get("path")
            if v and Path(v).exists():
                return v
        except Exception:
            pass
    # auto-scan common install locations
    candidates: list[str] = []
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        for c in (
            r"D:\Programs\Trae CN\Trae CN.exe",
            rf"{local}\Programs\Trae CN\Trae CN.exe",
            rf"{local}\Trae CN\Trae CN.exe",
        ):
            candidates.append(c)
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Trae CN.app",
            os.path.expanduser("~/Applications/Trae CN.app"),
        ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def set_trae_cn_exe_path(path: str) -> None:
    p = _exe_config_path()
    p.write_text(json.dumps({"path": path}), encoding="utf-8")


class ProcessController(Protocol):
    def is_running(self) -> bool: ...
    def kill(self) -> None: ...
    def launch(self) -> None: ...


class DefaultProcessController:
    """Real process controller (Windows/macOS). Linux = no-op for tests."""

    def is_running(self) -> bool:
        if sys.platform.startswith("win"):
            try:
                out = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq Trae CN.exe", "/NH"],
                    capture_output=True, text=True, timeout=10,
                )
                return "Trae CN.exe" in out.stdout
            except Exception:
                return False
        if sys.platform == "darwin":
            try:
                r = subprocess.run(
                    ["pgrep", "-f", r"Trae CN\.app/Contents/MacOS"],
                    capture_output=True, timeout=10,
                )
                return r.returncode == 0
            except Exception:
                return False
        return False  # Linux: not applicable

    def kill(self) -> None:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/IM", "Trae CN.exe"], capture_output=True)
            time.sleep(0.5)
            if self.is_running():
                subprocess.run(["taskkill", "/F", "/IM", "Trae CN.exe"], capture_output=True)
            time.sleep(1.0)
            return
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", 'tell application "Trae CN" to quit'],
                capture_output=True,
            )
            time.sleep(1.5)
            if self.is_running():
                subprocess.run(["pkill", "-9", "-f", r"Trae CN\.app/Contents/MacOS"], capture_output=True)
                time.sleep(1.0)
            return

    def launch(self) -> None:
        exe = get_trae_cn_exe_path()
        if not exe:
            raise RuntimeError(
                "Trae CN executable path not configured. Set it via "
                "`tcn set-path <path>` or TCN_TRAE_EXE env."
            )
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-a", exe])
        else:
            subprocess.Popen([exe])


class DryRunProcessController:
    """Never touches real processes; records calls. For tests/dry-run."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def is_running(self) -> bool:
        return False

    def kill(self) -> None:
        self.events.append("kill")

    def launch(self) -> None:
        self.events.append("launch")
