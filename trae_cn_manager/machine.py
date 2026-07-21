"""Trae CN machine-id & storage.json management.

Adapted from TAM machine.py for Trae CN:
- Trae CN data dir: ``%APPDATA%/Trae CN``
- Auth keys use ``icube-dc:<user_id>`` domain (different from international)
- Machine id file at the root of Trae CN user-data directory
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid as _uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import get_backups_dir, get_trae_cn_data_dir

# storage.json keys that carry the Trae CN login state.
# Note: CN uses "icube-dc" instead of international's "icube.cloudide"
AUTH_KEYS = (
    "iCubeAuthInfo://icube-dc:*",
    "iCubeEntitlementInfo://icube-dc:*",
    "iCubeServerData://icube-dc:*",
    "iCubeAuthInfo://usertag",
)

# Runtime caches that must be cleared on switch (relative to Trae CN data dir).
RUNTIME_CACHE_PATHS = (
    "User/globalStorage/state.vscdb",
    "User/globalStorage/state.vscdb.backup",
    "Local State",
    "IndexedDB",
    "Local Storage",
    "Session Storage",
    "Network/Cookies",
    "Network/Cookies-journal",
    "Cache",
    "CachedData",
    "GPUCache",
    "Code Cache",
)


@dataclass
class TraeCnLoginInfo:
    token: str
    refresh_token: str = ""
    user_id: str = ""
    phone: str = ""
    username: str = ""
    avatar_url: str = ""
    host: str = ""
    region: str = "CN"


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------
def generate_machine_id() -> str:
    """A fresh Trae CN ``machineid`` (UUID v4)."""
    return str(_uuid.uuid4())


def telemetry_machine_id(machine_id: str) -> str:
    """``telemetry.machineId`` = md5(machineid) hex."""
    return hashlib.md5(machine_id.encode("utf-8")).hexdigest()


def telemetry_sqm_id() -> str:
    """``telemetry.sqmId`` = ``{UUID-UPPERCASE}``."""
    return "{" + str(_uuid.uuid4()).upper() + "}"


def telemetry_dev_device_id() -> str:
    return str(_uuid.uuid4())


def telemetry_fields(machine_id: str) -> dict:
    return {
        "telemetry.machineId": telemetry_machine_id(machine_id),
        "telemetry.sqmId": telemetry_sqm_id(),
        "telemetry.devDeviceId": telemetry_dev_device_id(),
    }


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------
def _resolve_trae_dir(trae_dir: str | Path | None = None) -> Path:
    """Normalize trae_dir to Path, accepting str or None."""
    d = trae_dir or get_trae_cn_data_dir()
    return Path(d) if isinstance(d, str) else d


# ---------------------------------------------------------------------------
# machineid file
# ---------------------------------------------------------------------------
def _machineid_path(trae_dir: Path) -> Path:
    return trae_dir / "machineid"


def read_machineid(trae_dir: str | Path | None = None) -> str | None:
    trae_dir = _resolve_trae_dir(trae_dir)
    p = _machineid_path(trae_dir)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip()


def write_machineid(trae_dir: str | Path | None, machine_id: str) -> None:
    trae_dir = _resolve_trae_dir(trae_dir)
    trae_dir.mkdir(parents=True, exist_ok=True)
    _machineid_path(trae_dir).write_text(machine_id, encoding="utf-8")


# ---------------------------------------------------------------------------
# storage.json
# ---------------------------------------------------------------------------
def _storage_path(trae_dir: Path) -> Path:
    return trae_dir / "User" / "globalStorage" / "storage.json"


def read_storage(trae_dir: str | Path | None = None) -> dict:
    trae_dir = _resolve_trae_dir(trae_dir)
    p = _storage_path(trae_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def write_storage(trae_dir: str | Path | None, obj: dict) -> None:
    trae_dir = _resolve_trae_dir(trae_dir)
    p = _storage_path(trae_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def patch_storage_telemetry(
    machine_id: str,
    trae_dir: str | Path | None = None,
    clear_auth: bool = True,
) -> dict:
    """Update telemetry ids and optionally drop iCube auth keys."""
    obj = read_storage(trae_dir)
    if clear_auth:
        # Remove all iCubeAuthInfo/iCubeEntitlementInfo keys
        keys_to_remove = [k for k in obj if k.startswith("iCubeAuthInfo://") or k.startswith("iCubeEntitlementInfo://") or k.startswith("iCubeServerData://")]
        for k in keys_to_remove:
            obj.pop(k, None)
    obj.update(telemetry_fields(machine_id))
    write_storage(trae_dir, obj)
    return obj


# ---------------------------------------------------------------------------
# Login info (iCubeAuthInfo / iCubeEntitlementInfo for CN)
# ---------------------------------------------------------------------------
def _utc(fmt: str = "%Y-%m-%dT%H:%M:%S.000Z", dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime(fmt)


def build_auth_info(info: TraeCnLoginInfo) -> dict:
    host = info.host or "https://api.trae.cn"
    now = datetime.now(timezone.utc)
    return {
        "token": info.token,
        "refreshToken": info.refresh_token,
        "expiredAt": _utc(dt=now + timedelta(days=14)),
        "refreshExpiredAt": _utc(dt=now + timedelta(days=180)),
        "tokenReleaseAt": _utc(dt=now),
        "userId": info.user_id,
        "host": host,
        "userRegion": {
            "region": "CN",
            "_aiRegion": "CN",
        },
        "account": {
            "username": info.username,
            "iss": "",
            "iat": 0,
            "organization": "",
            "work_country": "",
            "phone": info.phone,
            "avatar_url": info.avatar_url,
            "description": "",
            "scope": "marscode",
            "loginScope": "trae",
            "storeCountryCode": "cn",
            "storeCountrySrc": "uid",
            "storeRegion": "CN",
            "userTag": "row",
        },
    }


def build_entitlement_info() -> dict:
    return {
        "identityStr": "Free",
        "identity": 0,
        "isPayFreshman": False,
        "isSupportCommercialization": True,
        "hasPackage": False,
        "enableEntitlement": True,
    }


def write_login_info(trae_dir: Path | None, info: TraeCnLoginInfo) -> None:
    obj = read_storage(trae_dir)
    user_id = info.user_id
    obj[f"iCubeAuthInfo://icube-dc:{user_id}"] = json.dumps(
        build_auth_info(info), ensure_ascii=False
    )
    obj[f"iCubeEntitlementInfo://icube-dc:{user_id}"] = json.dumps(
        build_entitlement_info(), ensure_ascii=False
    )
    write_storage(trae_dir, obj)


# ---------------------------------------------------------------------------
# Runtime cache clearing
# ---------------------------------------------------------------------------
def clear_runtime_cache(trae_dir: str | Path | None = None) -> list[str]:
    """Delete runtime caches. Returns the list of paths actually removed."""
    trae_dir = _resolve_trae_dir(trae_dir)
    removed: list[str] = []
    for rel in RUNTIME_CACHE_PATHS:
        p = trae_dir / rel
        if not p.exists():
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
            removed.append(rel)
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# Backup / Restore
# ---------------------------------------------------------------------------
def backup_trae_dir(trae_dir: str | Path | None = None, tag: str = "") -> Path:
    trae_dir = _resolve_trae_dir(trae_dir)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    name = f"trae-cn-{tag}-{ts}" if tag else f"trae-cn-{ts}"
    dest = get_backups_dir() / name
    dest.mkdir(parents=True, exist_ok=True)
    for rel in ("machineid", "User/globalStorage/storage.json"):
        src = trae_dir / rel
        if src.exists():
            d = dest / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, d)
    return dest


def restore_profile(src_backup: Path, trae_dir: str | Path | None = None, preserve_auth: bool = True) -> None:
    """Restore a backup to the Trae CN data dir, optionally preserving auth keys."""
    trae_dir = _resolve_trae_dir(trae_dir)
    if preserve_auth:
        current = read_storage(trae_dir)
        auth_snapshot = {k: v for k, v in current.items() if "iCubeAuth" in k or "iCubeEntitlement" in k}

    for rel in ("machineid", "User/globalStorage/storage.json"):
        src = src_backup / rel
        if src.exists():
            dst = trae_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    if preserve_auth and auth_snapshot:
        obj = read_storage(trae_dir)
        obj.update(auth_snapshot)
        write_storage(trae_dir, obj)


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------
def login_info_from_dict(d: dict) -> TraeCnLoginInfo:
    return TraeCnLoginInfo(
        token=d.get("token", ""),
        refresh_token=d.get("refresh_token", d.get("refreshToken", "")),
        user_id=d.get("user_id", d.get("userId", "")),
        phone=d.get("phone", ""),
        username=d.get("username", d.get("name", "")),
        avatar_url=d.get("avatar_url", d.get("avatarUrl", "")),
        host=d.get("host", ""),
        region=d.get("region", "CN"),
    )


def login_info_to_dict(info: TraeCnLoginInfo) -> dict:
    return asdict(info)
