"""Trae CN SMS registration flow — fully automatic (captcha + OTP).

Flow:
  1. Fetch a clean +86 number from SMS client
  2. Launch Playwright (headless), open trae.cn/login
  3. Fill phone, click send code
  4. Auto-solve Bytedance slider captcha via captcha-recognizer
  5. Poll SMS client for OTP
  6. Fill OTP, click login
  7. Extract session/JWT from browser storage
  8. Save account to local DB
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

from . import db as tcn_db
from .auto_slider import AutoSlider
from .config import get_proxy
from .machine import TraeCnLoginInfo, login_info_to_dict
from .models import Account
from .sms_client import SmsProvider, create_sms_client
from .trae_api import TraeCnApiClient, parse_jwt
from .vault import encrypt_obj

log = logging.getLogger(__name__)

_CLEAN_PHONE_RE = re.compile(r"\b\d{6,}\b")
_MAX_RETRY_PHONES = 5


@dataclass
class RegisterResult:
    success: bool = False
    phone: str = ""
    user_id: str = ""
    email: str = ""
    token: str = ""
    account_id: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Core registration
# ---------------------------------------------------------------------------

async def register_one(
    sms_client: SmsProvider | None = None,
    headed: bool = False,
    persist: bool = True,
) -> RegisterResult:
    """Register one Trae CN account — fully automatic.

    Args:
        sms_client: Reusable SMS provider (created fresh if None).
        headed: Show browser window (for debugging; captcha is auto-solved).
        persist: Save account to DB.

    Returns:
        RegisterResult with success/failure details.
    """
    if sms_client is None:
        sms_client = create_sms_client()

    proxy = get_proxy()

    # Get available numbers, sorted by cleanliness
    all_numbers = sms_client.get_available_numbers()
    if not all_numbers:
        return RegisterResult(success=False, error="No +86 numbers available from SMS provider")

    all_numbers.sort(key=lambda n: n.recent_count)

    last_error = ""
    for attempt_idx, number in enumerate(all_numbers[:_MAX_RETRY_PHONES]):
        phone = number.phone
        log.info("Attempt %d/%d: phone %s (%s, %d msgs)",
                 attempt_idx + 1, _MAX_RETRY_PHONES, phone, number.carrier, number.recent_count)

        try:
            result = await _register_with_phone(phone, proxy, headed, sms_client)
            if result.success:
                if persist:
                    _save_account(result)
                return result
            last_error = result.error
            log.warning("Phone %s failed: %s", phone, result.error)
        except Exception as exc:
            last_error = str(exc)
            log.warning("Phone %s exception: %s", phone, exc)

        await asyncio.sleep(2)

    return RegisterResult(success=False, error=f"All phones exhausted. Last error: {last_error}")


async def _register_with_phone(
    phone: str,
    proxy: str | None,
    headed: bool,
    sms_client: SmsProvider,
) -> RegisterResult:
    """Complete registration with a specific phone number."""
    result = RegisterResult(phone=phone)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headed,
            proxy={"server": proxy} if proxy else None,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=ChromeWhatsNewUI",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        auth_data: dict = {}
        cookies_list: list = []

        def _on_cookie():
            nonlocal cookies_list
            try:
                cookies_list = context.cookies()
            except Exception:
                pass

        context.on("cookie", lambda _: _on_cookie())

        try:
            # ---- Step 1: Navigate ----
            log.info("Opening trae.cn/login...")
            await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # ---- Step 2: Fill phone ----
            phone_input = await page.query_selector("input.mobile-phone")
            if not phone_input:
                phone_input = await page.query_selector("input[type='text']")
            if not phone_input:
                return _fail(result, "Phone input not found")

            await phone_input.click()
            await phone_input.fill(_format_phone_for_input(phone))
            await page.wait_for_timeout(500)

            # ---- Step 3: Click send code → triggers captcha ----
            send_btn = await page.query_selector("div.send-code")
            if not send_btn:
                return _fail(result, "Send code button not found")
            await send_btn.click()
            log.info("Send code clicked — captcha will be solved automatically...")

            # ---- Step 4: Auto-solve captcha ----
            solver = AutoSlider()
            captcha_ok = await solver.solve(page, phone)

            if not captcha_ok:
                # captcha-recognizer might not be installed, or detection failed
                log.warning("Auto-slider failed. Falling back to manual solve (60s timeout)...")
                captcha = await page.query_selector(
                    "#captcha_container iframe, div[class*='captcha'] iframe"
                )
                if captcha:
                    if not headed:
                        log.warning("Captcha in headless mode — re-run with --headed for manual solve")
                    try:
                        await page.wait_for_selector(
                            "#captcha_container iframe", state="hidden", timeout=60000
                        )
                        log.info("Captcha manually solved!")
                    except Exception:
                        return _fail(result, "Captcha not solved within 60s")
                await page.wait_for_timeout(2000)

            # ---- Step 5: Poll for OTP ----
            log.info("Waiting for SMS OTP on %s...", phone)
            otp_code = sms_client.wait_for_otp(phone, timeout=180)
            if not otp_code:
                return _fail(result, f"OTP not received on {phone} within 180s")
            log.info("OTP received: %s", otp_code)

            # ---- Step 6: Fill OTP ----
            code_input = await page.query_selector("input[placeholder*='验证码']")
            if not code_input:
                code_input = await page.query_selector("input:not([type='hidden'])")
            if code_input:
                await code_input.click()
                await code_input.fill(otp_code)
                await page.wait_for_timeout(1000)

            # ---- Step 7: Click login ----
            login_btn = await page.query_selector("div[class*='btn-submit']")
            if not login_btn:
                login_btn = await page.query_selector("button[type='submit']")
            if not login_btn:
                login_btn = await page.query_selector("div:has-text('登录'):not(:has(span))")
            if login_btn:
                await login_btn.click()
                log.info("Clicked login button")
            else:
                log.warning("Login button not found, may have auto-submitted")

            # ---- Step 8: Wait for redirect ----
            await page.wait_for_timeout(5000)
            try:
                await page.wait_for_url("**/*", timeout=15000)
            except Exception:
                pass

            current_url = page.url
            log.info("Current URL after login: %s", current_url)

            # ---- Step 9: Extract auth data ----
            await page.wait_for_timeout(2000)
            cookies_list = await context.cookies()

            # Token from localStorage
            token = ""
            try:
                token = await page.evaluate(
                    "localStorage.getItem('Cloud-IDE-Token') || ''"
                )
            except Exception:
                pass

            # If no token, try GetUserToken API
            if not token and cookies_list:
                cookies_dict = {c["name"]: c["value"] for c in cookies_list}
                try:
                    api = TraeCnApiClient(proxy=proxy)
                    token = api.get_user_token(cookies_dict)
                except Exception as exc:
                    log.warning("GetUserToken failed: %s", exc)

            # Extract user info
            user_id = ""
            if token:
                try:
                    jwt_payload = parse_jwt(token)
                    user_id = jwt_payload.user_id
                except Exception:
                    pass

            # Also try localStorage
            try:
                storage = await page.evaluate(
                    "() => { const k = Object.keys(localStorage); const r = {}; "
                    "for (const key of k) r[key] = localStorage.getItem(key); return r; }"
                )
                for k, v in storage.items():
                    if "iCubeAuth" in k and v:
                        parsed = json.loads(v) if isinstance(v, str) else v
                        if isinstance(parsed, dict) and parsed.get("userId"):
                            if not user_id:
                                user_id = parsed["userId"]
                            if not token and parsed.get("token"):
                                token = parsed["token"]
            except Exception:
                pass

            result.success = bool(user_id or token)
            result.user_id = user_id
            result.token = token or ""
            result.email = f"+86{phone[-4:]}" if not result.token else ""

            if not result.success:
                return _fail(result, f"Login may have failed. URL: {current_url}")

            await browser.close()
            return result

        except Exception as exc:
            await browser.close()
            return _fail(result, str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_phone_for_input(phone: str) -> str:
    """Format phone for input. Accepts e.g. '8619604191344' → '13800138000'."""
    raw = re.sub(r"^86|\+86|^00", "", phone.strip())
    digits = re.sub(r"\D", "", raw)
    if len(digits) > 11:
        digits = digits[-11:]
    return digits


def _fail(result: RegisterResult, error: str) -> RegisterResult:
    result.success = False
    result.error = error
    return result


def _save_account(result: RegisterResult) -> None:
    """Save a successful registration to the local database."""
    secrets = {
        "token": result.token,
        "user_id": result.user_id,
        "phone": result.phone,
        "region": "CN",
    }
    secrets_blob = encrypt_obj(secrets)

    account = Account(
        email=f"+86{result.phone[-4:]}" if result.phone else "",
        phone=result.phone,
        name=f"CN-{result.user_id[:8]}" if result.user_id else "CN-user",
        user_id=result.user_id,
        region="CN",
        plan_type="Free",
        status="active",
        secrets_blob=secrets_blob,
    )
    saved = tcn_db.upsert_account(account)
    result.account_id = saved.id
    log.info("Account saved to DB: %s (id=%s)", account.email, saved.id)


def register_one_sync(
    headed: bool = False,
    persist: bool = True,
) -> RegisterResult:
    """Synchronous wrapper for register_one."""
    return asyncio.run(register_one(headed=headed, persist=persist))


def register_batch_sync(
    total: int = 1,
    concurrency: int = 1,
    headed: bool = False,
    persist: bool = True,
) -> list[RegisterResult]:
    """Register multiple accounts sequentially."""
    sms_client = create_sms_client()
    results: list[RegisterResult] = []
    for i in range(total):
        log.info("--- Batch registration %d/%d ---", i + 1, total)
        result = asyncio.run(register_one(sms_client=sms_client, headed=headed, persist=persist))
        results.append(result)
        if result.success:
            log.info("Account %d/%d registered: %s", i + 1, total, result.phone)
        else:
            log.error("Account %d/%d failed: %s", i + 1, total, result.error)
    return results
