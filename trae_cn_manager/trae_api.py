"""Async HTTP client for the Trae CN cloud API.

Endpoints (all at api.trae.cn):
  * ``POST /cloudide/api/v3/trae/CheckLogin``
  * ``POST /cloudide/api/v3/trae/Login``
  * ``POST /cloudide/api/v3/common/GetUserToken``
  * ``POST /trae/api/v1/pay/web_user_ent_usage``  (usage query)
  * ``POST /trae/api/v1/pay/query_user_usage_group_by_session``

Auth header is ``Cloud-IDE-JWT <token>``.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import get_proxy

log = logging.getLogger(__name__)

API_BASE = "https://api.trae.cn"
AUTH_SCHEME = "Cloud-IDE-JWT"


# ---------------------------------------------------------------------------
@dataclass
class UsageSummary:
    plan_type: str = "Free"
    fast_request_limit: int = 0
    fast_request_used: float = 0.0
    fast_request_left: float = 0.0
    slow_request_limit: int = 0
    slow_request_used: float = 0.0
    slow_request_left: float = 0.0
    advanced_model_limit: int = 0
    advanced_model_used: float = 0.0
    advanced_model_left: float = 0.0
    autocomplete_limit: int = 0
    autocomplete_used: float = 0.0
    autocomplete_left: float = 0.0
    reset_time: int = 0

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class JwtPayload:
    user_id: str = ""
    tenant_id: str = ""
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
def parse_jwt(token: str) -> JwtPayload:
    """Decode a JWT payload (no signature verification)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid JWT (expected 3 parts)")
    payload_b64 = parts[1]
    pad = (-len(payload_b64)) % 4
    standard = payload_b64.replace("-", "+").replace("_", "/") + ("=" * pad)
    try:
        raw_bytes = base64.b64decode(standard)
        raw = json.loads(raw_bytes.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"invalid JWT payload: {e}") from e
    data = raw.get("data") or {}
    return JwtPayload(
        user_id=str(data.get("id", "")),
        tenant_id=str(data.get("tenant_id", "")),
        raw=raw,
    )


# ---------------------------------------------------------------------------
class TraeCnApiClient:
    """HTTP client for Trae CN API."""

    def __init__(self, proxy: str | None = None):
        self.proxy = proxy or get_proxy()
        self._client_args: dict = {"timeout": 30}
        if self.proxy:
            self._client_args["proxy"] = self.proxy

    def _client(self) -> httpx.Client:
        return httpx.Client(**self._client_args)

    def check_login(self, cookies: dict | None = None) -> dict:
        """Check if a session is still valid."""
        with self._client() as c:
            headers = {}
            if cookies:
                c.cookies.update(cookies)
            r = c.post(
                f"{API_BASE}/cloudide/api/v3/trae/CheckLogin",
                json={"GetNickNameEditStatus": True},
                headers=headers,
            )
            r.raise_for_status()
            return r.json()

    def trae_login(self, cookies: dict | None = None) -> dict:
        """Login to Trae CN (get X-Cloudide-Session cookie)."""
        with self._client() as c:
            if cookies:
                c.cookies.update(cookies)
            r = c.post(
                f"{API_BASE}/cloudide/api/v3/trae/Login",
                json={"GetNickNameEditStatus": True},
            )
            r.raise_for_status()
            return {"cookies": dict(c.cookies), "status": r.status_code}

    def get_user_token(self, cookies: dict) -> str | None:
        """Get JWT token using session cookies."""
        with self._client() as c:
            c.cookies.update(cookies)
            r = c.post(
                f"{API_BASE}/cloudide/api/v3/common/GetUserToken",
                json={},
            )
            r.raise_for_status()
            data = r.json()
            result = data.get("Result", {})
            return result.get("Token")

    def get_usage_summary(self, jwt_token: str) -> UsageSummary | None:
        """Get usage/quota summary for the current user."""
        with self._client() as c:
            headers = {f"{AUTH_SCHEME}": jwt_token}
            r = c.post(
                f"{API_BASE}/trae/api/v1/pay/web_user_ent_usage",
                json={"require_usage": True},
                headers=headers,
            )
            if r.status_code != 200:
                log.warning("get_usage_summary returned %d", r.status_code)
                return None
            return self._parse_usage(r.json())

    def _parse_usage(self, data: dict) -> UsageSummary:
        result = data.get("Result", data)
        entitlements = result.get("EntitlementList", []) or result.get("entitlement_list", [])
        usage = UsageSummary()
        for ent in entitlements:
            ent_type = (ent.get("EntitlementType") or ent.get("entitlement_type", "")).lower()
            limit_val = ent.get("LimitValue") or ent.get("limit_value", 0)
            usage_val = ent.get("UsageValue") or ent.get("usage_value", 0)
            if "fast" in ent_type:
                usage.fast_request_limit = int(limit_val)
                usage.fast_request_used = float(usage_val)
            elif "slow" in ent_type:
                usage.slow_request_limit = int(limit_val)
                usage.slow_request_used = float(usage_val)
            elif "advanced" in ent_type or "pro" in ent_type:
                usage.advanced_model_limit = int(limit_val)
                usage.advanced_model_used = float(usage_val)
            elif "autocomplete" in ent_type or "completion" in ent_type:
                usage.autocomplete_limit = int(limit_val)
                usage.autocomplete_used = float(usage_val)
        usage.fast_request_left = max(0, usage.fast_request_limit - usage.fast_request_used)
        usage.slow_request_left = max(0, usage.slow_request_limit - usage.slow_request_used)
        usage.advanced_model_left = max(0, usage.advanced_model_limit - usage.advanced_model_used)
        usage.autocomplete_left = max(0, usage.autocomplete_limit - usage.autocomplete_used)
        return usage
