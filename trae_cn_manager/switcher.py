"""One-click account switching for Trae CN.

Orchestrates the Trae CN state swap:
  kill Trae CN -> write machineid -> clear runtime cache -> patch storage.json
  telemetry -> write iCubeAuthInfo -> launch Trae CN.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from . import db as tcn_db
from . import machine as tcn_machine
from .models import Account
from .config import get_trae_cn_data_dir
from .machine import TraeCnLoginInfo, login_info_from_dict
from .process_ctl import DefaultProcessController, ProcessController
from .vault import decrypt_obj, encrypt_obj

log = logging.getLogger(__name__)


class Switcher:
    def __init__(self, ctl: ProcessController | None = None):
        self.ctl = ctl or DefaultProcessController()

    # ------------------------------------------------------------------
    @staticmethod
    def _account_login_info(account) -> TraeCnLoginInfo:
        secrets = decrypt_obj(account.secrets_blob)
        li = secrets.get("login_info") or {}
        info = TraeCnLoginInfo(
            token=li.get("token") or secrets.get("token", ""),
            refresh_token=li.get("refresh_token") or secrets.get("refresh_token", ""),
            user_id=li.get("user_id") or account.user_id,
            phone=li.get("phone") or account.phone,
            username=li.get("username") or account.name,
            avatar_url=li.get("avatar_url") or account.avatar_url,
            host=li.get("host", ""),
            region=li.get("region") or account.region,
        )
        if not info.token:
            raise ValueError(f"account {account.phone or account.email} has no token; cannot switch")
        return info

    # ------------------------------------------------------------------
    def switch_to_account(
        self,
        account,
        *,
        launch: bool = True,
        reset_registry: bool = False,
    ) -> dict:
        """Apply ``account`` to the local Trae CN IDE."""
        trae_dir = Path(get_trae_cn_data_dir())
        info = self._account_login_info(account)
        machine_id = account.machine_id or tcn_machine.generate_machine_id()

        log.info("Switching Trae CN -> %s (machineid=%s)", account.phone or account.email, machine_id)

        # Snapshot current state
        try:
            tcn_machine.backup_trae_dir(trae_dir, tag="pre-switch")
        except Exception as e:
            log.warning("backup failed: %s", e)

        # 1. Stop Trae CN
        self.ctl.kill()

        # 2. Write machineid
        tcn_machine.write_machineid(trae_dir, machine_id)

        # 3. Clear runtime caches
        removed = tcn_machine.clear_runtime_cache(trae_dir)

        # 4. Patch telemetry + clear old auth
        tcn_machine.patch_storage_telemetry(machine_id, trae_dir, clear_auth=True)

        # 5. Write new login info
        tcn_machine.write_login_info(trae_dir, info)

        # 6. (Windows) optionally reset registry MachineGuid
        reg_reset = False
        if reset_registry:
            try:
                new_guid = tcn_machine.generate_machine_id()
                tcn_machine.set_windows_machine_guid(new_guid)
                reg_reset = True
            except Exception as e:
                log.warning("MachineGuid reset failed: %s", e)

        # 7. Update current account in DB
        prev_id = tcn_db.get_current_account_id()
        account.is_current = True
        account.machine_id = machine_id
        tcn_db.upsert_account(account)
        tcn_db.set_current_account(account.id)

        # 8. Launch Trae CN
        if launch:
            self.ctl.launch()

        return {
            "account_id": account.id,
            "email": account.email,
            "machine_id": machine_id,
            "previous_account_id": prev_id,
            "caches_cleared": removed,
            "registry_reset": reg_reset,
        }

    # ------------------------------------------------------------------
    def capture_current(
        self,
        *,
        name: str = "",
        email: str = "",
        phone: str = "",
    ) -> object | None:
        """Capture the currently logged-in Trae CN session into local DB.

        Reads iCubeAuthInfo from storage.json, creates an Account record,
        encrypts the secrets, and writes to DB.
        """
        trae_dir = Path(get_trae_cn_data_dir())
        storage = tcn_machine.read_storage(trae_dir)

        # Find the active iCubeAuthInfo key
        auth_key = None
        auth_value = None
        for k, v in storage.items():
            if "iCubeAuthInfo://icube-dc:" in k:
                auth_key = k
                auth_value = v
                break

        if not auth_value:
            log.warning("No iCubeAuthInfo found in storage.json — is Trae CN logged in?")
            return None

        try:
            auth_data = json.loads(auth_value) if isinstance(auth_value, str) else auth_value
        except (json.JSONDecodeError, TypeError):
            log.warning("Failed to parse iCubeAuthInfo")
            return None

        user_id = auth_data.get("userId", "")
        token = auth_data.get("token", "")

        if not user_id or not token:
            log.warning("iCubeAuthInfo missing userId or token")
            return None

        # Build secrets blob
        secrets = {
            "token": token,
            "refresh_token": auth_data.get("refreshToken", ""),
            "user_id": user_id,
            "phone": phone or "",
            "host": auth_data.get("host", ""),
            "region": "CN",
        }
        secrets_blob = encrypt_obj(secrets)

        # Check if already exists
        existing = tcn_db.get_account_by_email(f"CN-{user_id[:8]}")
        if existing:
            existing.secrets_blob = secrets_blob
            existing.phone = phone or existing.phone
            existing.name = name or existing.name
            existing.updated_at = int(__import__("time").time())
            tcn_db.upsert_account(existing)
            log.info("Updated existing account: %s", existing.id)
            return existing

        account = Account(
            email=f"CN-{user_id[:8]}",
            phone=phone or "",
            name=name or f"CN-{user_id[:8]}",
            user_id=user_id,
            region="CN",
            plan_type="Free",
            status="active",
            secrets_blob=secrets_blob,
        )
        saved = tcn_db.upsert_account(account)
        log.info("Captured account: %s (id=%s)", saved.email, saved.id)
        return saved

    # ------------------------------------------------------------------
    def clear_login_state(self, *, launch: bool = False) -> None:
        """Reset Trae CN to logged-out state."""
        trae_dir = Path(get_trae_cn_data_dir())
        self.ctl.kill()

        # Clear auth keys from storage
        tcn_machine.patch_storage_telemetry(
            tcn_machine.generate_machine_id(), trae_dir, clear_auth=True
        )
        tcn_machine.clear_runtime_cache(trae_dir)
        tcn_db.set_current_account(None)

        if launch:
            self.ctl.launch()

        log.info("Login state cleared")
