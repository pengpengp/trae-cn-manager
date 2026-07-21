"""Path & runtime configuration for Trae CN Manager.

All Trae CN-related paths are overridable via environment variables so the
switching logic can be unit-tested on Linux against a fake Trae CN data dir.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "trae_cn_manager"
APP_AUTHOR = "tcn"

# Trae CN API endpoints (phone/SMS registration flow).
# Discovered via Playwright DevTools capture (2026-07-21).
REGION_HOSTS = {"CN": "https://api.trae.cn"}

# SMS send-code endpoint (requires captcha from Bytedance verifycenter).
SMS_SEND_CODE_URL = "https://www.trae.cn/passport/web/send_code/"

# Login / register-verify (best guess — needs confirmation).
SMS_LOGIN_URL = "https://www.trae.cn/passport/web/sms_login/"
SMS_REGISTER_VERIFY_URL = "https://www.trae.cn/passport/web/register_verify_login/"
TRAE_LOGIN_URL = "https://api.trae.cn/cloudide/api/v3/trae/Login"
TRAE_CHECK_LOGIN_URL = "https://api.trae.cn/cloudide/api/v3/trae/CheckLogin"
GET_USER_TOKEN_URL = "https://api.trae.cn/cloudide/api/v3/common/GetUserToken"

# Default proxy used by the HTTP client / browser when set.
DEFAULT_PROXY = os.environ.get("TCN_PROXY") or os.environ.get("TAM_PROXY", "http://127.0.0.1:7897")


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def get_app_data_dir() -> Path:
    """Directory where TCN stores its own data (db, logs, backups)."""
    override = os.environ.get("TCN_DATA_DIR")
    if override:
        p = Path(override)
    else:
        p = Path(user_data_dir(APP_NAME, APP_AUTHOR))
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_trae_cn_data_dir() -> Path:
    """Trae CN IDE user-data directory.

    Override with ``TCN_TRAE_DATA_DIR`` for testing or non-standard installs.
    """
    override = os.environ.get("TCN_TRAE_DATA_DIR")
    if override:
        return Path(override)

    if is_windows():
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA env var not set")
        return Path(appdata) / "Trae CN"
    if is_macos():
        home = os.environ.get("HOME", str(Path.home()))
        return Path(home) / "Library" / "Application Support" / "Trae CN"
    raise RuntimeError(
        "Trae CN IDE is not supported on this OS; set TCN_TRAE_DATA_DIR to a "
        "Trae CN data directory (e.g. a copy from a Windows host)."
    )


def get_db_path() -> Path:
    return get_app_data_dir() / "accounts.db"


def get_logs_dir() -> Path:
    p = get_app_data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_backups_dir() -> Path:
    p = get_app_data_dir() / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_proxy() -> str | None:
    """Proxy URL for outbound HTTP (registration/API). None to disable."""
    val = os.environ.get("TCN_PROXY") or os.environ.get("TAM_PROXY", DEFAULT_PROXY)
    if val and val.lower() in ("none", "off", "disabled", ""):
        return None
    return val


def host_for_region(region: str) -> str:
    return REGION_HOSTS.get((region or "").upper(), "https://api.trae.com.cn")
