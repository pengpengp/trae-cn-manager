"""End-to-end test: Trae CN send_code → smscodeonline.com OTP reception.

This script verifies whether smscodeonline.com numbers can actually receive
Trae CN OTP after slider captcha is solved. It does NOT complete the full
registration — only tests OTP delivery.

Usage:
    python scripts/test_smscodeonline_otp.py [--headed] [--max-phones N]

Default: tests 3 numbers, headless, polls for 10 minutes (600s) per number.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

# Ensure trae_cn_manager package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright

from trae_cn_manager.auto_slider import AutoSlider
from trae_cn_manager.sms_client import SmsCodeOnlineProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("smscodeonline_e2e.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("e2e")

# ByteDance series keywords — if any appear in sender/content, it's a hit
BD_KEYWORDS = (
    "TikTok", "抖音", "Trae", "doubao", "豆包", "火山", "Volcengine",
    "飞书", "Lark", "Feishu", "Bytedance", "字节",
)

# 6-digit OTP regex (matches provider's _OTP_RE)
OTP_RE = re.compile(r"\b(\d{6})\b")
# Also match 4-digit OTP (some Chinese services use 4-digit)
OTP4_RE = re.compile(r"验证码[：:\s]*(\d{4,6})")


async def on_response(response, captured: list):
    """Intercept Trae/Passport API responses for diagnosis."""
    url = response.url
    if any(kw in url for kw in ["send_code", "passport/web", "captcha/verify", "sms"]):
        try:
            body = await response.text()
        except Exception:
            body = "<binary>"
        captured.append({
            "url": url,
            "method": response.request.method,
            "status": response.status,
            "resp_body": body[:1500],
        })
        log.info("[RESP %d] %s %s", response.status, response.request.method, url)
        if "send_code" in url:
            log.info("  → send_code response: %s", body[:500])


def parse_messages_from_html(html: str):
    """Parse messages from smscodeonline.com number page using regex.

    Page structure (verified 2026-07-24):
        <div class="card m-2 text-center">
            <div class="card-header">
                <span class="mt-0"> Sender: <a href="sender/X">X</a></span>
            </div>
            <div class="card-body">
                {content}
                <div class="clear"></div>
                <footer class="blockquote-footer float-right"> {time_ago}</footer>
            </div>
        </div>
    """
    msgs = []
    card_re = re.compile(
        r'<div class="card m-2 text-center"[^>]*>.*?'
        r'<span class="mt-0">\s*Sender:\s*<a href="[^"]*">([^<]+)</a>\s*</span>.*?'
        r'<div class="card-body"[^>]*>(.*?)</div>\s*</div>',
        re.DOTALL,
    )
    for m in card_re.finditer(html):
        sender = m.group(1).strip()
        body = m.group(2)
        # Strip clear div and footer
        body_clean = re.sub(r'<div class="clear"></div>', '', body, flags=re.DOTALL)
        body_clean = re.sub(r'<footer[^>]*>.*?</footer>', '', body_clean, flags=re.DOTALL)
        content = re.sub(r'<[^>]+>', '', body_clean).strip()
        content = (content.replace('&amp;', '&').replace('&lt;', '<')
                   .replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'"))
        content = re.sub(r'\s+', ' ', content).strip()
        # Timestamp
        ts_m = re.search(r'<footer[^>]*>\s*([^<]+?)\s*</footer>', body)
        timestamp = ts_m.group(1).strip() if ts_m else ""
        # OTP
        otp6 = OTP_RE.search(content)
        otp4 = OTP4_RE.search(content)
        otp = (otp6.group(1) if otp6 else (otp4.group(1) if otp4 else ""))
        msgs.append({
            "sender": sender,
            "content": content,
            "timestamp": timestamp,
            "otp": otp,
        })
    return msgs


async def fetch_page_html(url: str, timeout: int = 30):
    """Fetch a URL with stdlib urllib (no external deps)."""
    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/125.0.0.0 Safari/537.36",
                 "Accept-Language": "zh-CN,zh,en"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read().decode("utf-8", errors="replace")


def baseline_messages(html: str):
    """Extract baseline message count + signatures to detect new arrivals."""
    msgs = parse_messages_from_html(html)
    # Signature: (sender, content[:50], timestamp)
    sigs = set((m["sender"], m["content"][:80], m["timestamp"]) for m in msgs)
    return msgs, sigs


async def poll_for_new_otp(phone: str, baseline_sigs: set, timeout: int = 600):
    """Poll smscodeonline.com number page for new messages carrying Trae/ByteDance OTP."""
    url = f"https://smscodeonline.com/virtual-phone/p-{phone}"
    deadline = time.time() + timeout
    poll_interval = 10  # seconds
    last_count = 0

    log.info("Polling %s for new OTP (timeout=%ds, interval=%ds)", url, timeout, poll_interval)

    while time.time() < deadline:
        try:
            html = await fetch_page_html(url)
            msgs, sigs = baseline_messages(html)
            new_sigs = sigs - baseline_sigs
            new_msgs = [m for m in msgs if (m["sender"], m["content"][:80], m["timestamp"]) in new_sigs]
            if new_msgs:
                log.info("✨ New messages detected: %d (total now %d)", len(new_msgs), len(msgs))
                for m in new_msgs:
                    log.info("  [NEW] sender=%s otp=%s time=%s", m["sender"], m["otp"], m["timestamp"])
                    log.info("         content: %s", m["content"][:200])
                    # Check if ByteDance series
                    text = (m["sender"] + " " + m["content"]).lower()
                    for kw in BD_KEYWORDS:
                        if kw.lower() in text:
                            log.info("  🔥 ByteDance series SMS detected: %s", kw)
                            if m["otp"]:
                                log.info("  🎉 TRAE OTP MAY HAVE ARRIVED: %s", m["otp"])
                                return m["otp"]
                # Even if not ByteDance series, if any new OTP appeared, return it
                # (Trae's sender name might be "Trae" or "Bytedance" or "字节跳动")
                for m in new_msgs:
                    if m["otp"]:
                        # Check if it might be Trae OTP (sender name contains Trae)
                        if "trae" in m["sender"].lower() or "字节" in m["sender"]:
                            log.info("  🎉 Trae sender detected! OTP=%s", m["otp"])
                            return m["otp"]
            else:
                if len(msgs) != last_count:
                    log.info("  ... still %d messages (no new)", len(msgs))
                    last_count = len(msgs)
        except Exception as exc:
            log.warning("  poll error: %s", exc)
        await asyncio.sleep(poll_interval)

    log.warning("⏰ Timed out after %ds waiting for OTP on %s", timeout, phone)
    return None


async def try_one_phone(phone: str, headed: bool) -> dict:
    """Test one phone number end-to-end.

    Returns dict with:
        phone, send_code_response, otp_received, baseline_msg_count
    """
    log.info("=" * 70)
    log.info("Testing phone %s", phone)
    captured: list = []

    # Step 1: capture baseline messages on smscodeonline.com
    msg_url = f"https://smscodeonline.com/virtual-phone/p-{phone}"
    try:
        baseline_html = await fetch_page_html(msg_url)
        baseline_msgs, baseline_sigs = baseline_messages(baseline_html)
        log.info("Baseline: %d existing messages on smscodeonline.com", len(baseline_msgs))
    except Exception as exc:
        log.error("Failed to fetch baseline: %s", exc)
        return {"phone": phone, "error": f"baseline failed: {exc}"}

    # Step 2: launch browser, fill phone, click send code
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36",
        )
        context.on("response", lambda r: asyncio.create_task(on_response(r, captured)))
        page = await context.new_page()

        try:
            log.info("Opening trae.cn/login...")
            await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Fill phone (strip +86, take last 11 digits)
            phone_input = await page.query_selector("input.mobile-phone")
            if not phone_input:
                phone_input = await page.query_selector("input[type='text']")
            if not phone_input:
                return {"phone": phone, "error": "Phone input not found"}

            raw = re.sub(r"^86|\+86|^00", "", phone.strip())
            digits = re.sub(r"\D", "", raw)
            clean_phone = digits[-11:] if len(digits) > 11 else digits
            await phone_input.click()
            await phone_input.fill(clean_phone)
            await page.wait_for_timeout(500)

            # Click send code button → may trigger captcha
            send_btn = await page.query_selector("div.send-code, [class*='send-code']")
            if not send_btn:
                # Fallback: find by text
                for tag in ["div", "span", "button"]:
                    for el in await page.query_selector_all(tag):
                        if await el.is_visible():
                            text = (await el.inner_text()).strip()
                            if "获取验证码" in text or "获取验证码" in text:
                                send_btn = el
                                break
                    if send_btn:
                        break
            if not send_btn:
                return {"phone": phone, "error": "Send code button not found"}

            await send_btn.click()
            log.info("Send code clicked. Waiting for captcha or response...")

            # Wait a bit to see if captcha appears
            await page.wait_for_timeout(3000)

            # Try to solve captcha if present
            solver = AutoSlider()
            captcha_solved = False
            try:
                # Check if captcha iframe is present
                captcha_frame = None
                for _ in range(15):
                    for frame in page.frames:
                        if "verifycenter" in frame.url and "captcha" in frame.url:
                            captcha_frame = frame
                            break
                    if captcha_frame:
                        break
                    await page.wait_for_timeout(1000)

                if captcha_frame:
                    log.info("Captcha detected, solving with solve_existing()...")
                    # Use solve_existing() — phone is already filled + send_code already clicked.
                    # Calling solve() would re-fill the phone input, which fails with
                    # "subtree intercepts pointer events" once the captcha overlay is shown.
                    captcha_solved = await solver.solve_existing(page)
                    log.info("Captcha solved: %s", captcha_solved)
                else:
                    log.info("No captcha iframe detected, send_code may have succeeded directly")
                    captcha_solved = True  # assume succeeded
            except Exception as exc:
                log.warning("Captcha solve error: %s", exc)

            # Step 3: Poll smscodeonline for OTP (10 minutes)
            otp = await poll_for_new_otp(phone, baseline_sigs, timeout=600)

            # Step 4: collect captured responses
            send_code_resp = None
            for c in captured:
                if "send_code" in c["url"]:
                    send_code_resp = c
                    break

            return {
                "phone": phone,
                "captcha_solved": captcha_solved,
                "send_code_response": send_code_resp,
                "all_captured": captured,
                "otp_received": otp,
                "baseline_msg_count": len(baseline_msgs),
            }

        except Exception as exc:
            return {"phone": phone, "error": str(exc)}
        finally:
            await browser.close()


async def main_async(max_phones: int, headed: bool):
    log.info("=" * 70)
    log.info("Trae CN → smscodeonline.com OTP delivery test")
    log.info("=" * 70)

    # Get numbers from SmsCodeOnlineProvider
    provider = SmsCodeOnlineProvider()
    numbers = provider.get_available_numbers()
    if not numbers:
        log.error("No +86 numbers available from smscodeonline.com")
        return

    # Prefer real-segment numbers (13x/15x/17x/18x)
    real_seg = [n for n in numbers if n.carrier in ("CM", "CU", "CT")]
    test_pool = (real_seg + numbers)[:max_phones]
    log.info("Testing %d numbers (real-segment preferred): %s",
             len(test_pool), [n.phone for n in test_pool])

    results = []
    for i, number in enumerate(test_pool):
        log.info("\n[%d/%d] Phone %s (carrier=%s)", i + 1, len(test_pool),
                 number.phone, number.carrier)
        result = await try_one_phone(number.phone, headed)
        results.append(result)

        if result.get("otp_received"):
            log.info("🎉🎉🎉 SUCCESS! OTP received for %s: %s",
                     result["phone"], result["otp"])
            break

        # Cooldown between attempts
        if i + 1 < len(test_pool):
            log.info("Cooldown 30s before next attempt...")
            await asyncio.sleep(30)

    # Summary
    log.info("\n" + "=" * 70)
    log.info("TEST SUMMARY")
    log.info("=" * 70)
    for r in results:
        if r.get("error"):
            log.info("  %s: ERROR %s", r["phone"], r["error"])
        else:
            sc = r.get("send_code_response") or {}
            log.info("  %s: captcha=%s send_code_status=%s otp=%s",
                     r["phone"], r.get("captcha_solved"),
                     sc.get("status", "?"), r.get("otp_received"))
    # Save full results to JSON
    out = Path("smscodeonline_e2e_results.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Full results saved to %s", out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument("--max-phones", type=int, default=3,
                        help="Max number of phones to test (default: 3)")
    args = parser.parse_args()
    asyncio.run(main_async(args.max_phones, args.headed))


if __name__ == "__main__":
    main()
