"""End-to-end test: Trae CN send_code → free-otp-receive.com OTP reception.

Tests whether free-otp-receive.com numbers (minute-level active, TikTok OTP
confirmed) can actually receive Trae CN OTP after slider captcha is solved.

Usage:
    python scripts/test_freeotp_otp.py [--headed] [--max-phones N] [--poll N]

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
from trae_cn_manager.sms_client import FreeOtpReceiveProvider, SmsMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("freeotp_e2e.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("e2e")

# ByteDance series keywords
BD_KEYWORDS = (
    "TikTok", "抖音", "Trae", "doubao", "豆包", "火山", "Volcengine",
    "飞书", "Lark", "Feishu", "Bytedance", "字节",
)


async def on_response(response, captured: list):
    """Intercept Trae/Passport API responses for diagnosis."""
    url = response.url
    if any(kw in url for kw in ["send_code", "passport/web", "captcha/verify"]):
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
        log.info("[RESP %d] %s %s", response.status, response.request.method, url[:120])
        if "send_code" in url:
            log.info("  → send_code response: %s", body[:500])


def parse_messages_via_regex(html: str):
    """Parse messages from free-otp-receive.com number page using regex.

    Structure (Tailwind CSS):
        <span class="text-sm font-semibold...">SENDER</span>
        <span class="text-xs...">TIME ago</span>
        </div>
        <p class="text-sm...">CONTENT</p>
    """
    msgs = []
    msg_re = re.compile(
        r'<span class="text-sm font-semibold[^"]*">([^<]+)</span>\s*'
        r'<span class="text-xs[^"]*">([^<]*\bago\b[^<]*)</span>\s*'
        r'</div>\s*'
        r'<p class="text-sm[^"]*">(.*?)</p>',
        re.DOTALL,
    )
    otp_re = re.compile(r"\b(\d{6})\b")
    for m in msg_re.finditer(html):
        sender = m.group(1).strip()
        timestamp = m.group(2).strip()
        content = re.sub(r"<[^>]+>", "", m.group(3))
        content = (content.replace("&#x27;", "'").replace("&amp;", "&")
                   .replace("&lt;", "<").replace("&gt;", ">")
                   .replace("&quot;", '"'))
        content = re.sub(r"\s+", " ", content).strip()
        if not content:
            continue
        otp_m = otp_re.search(content)
        msgs.append({
            "sender": sender,
            "content": content,
            "timestamp": timestamp,
            "otp": otp_m.group(1) if otp_m else "",
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
                 "Accept-Language": "en-US,en"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read().decode("utf-8", errors="replace")


def baseline_messages(html: str):
    """Extract baseline message signatures to detect new arrivals."""
    msgs = parse_messages_via_regex(html)
    sigs = set((m["sender"], m["content"][:80], m["timestamp"]) for m in msgs)
    return msgs, sigs


async def poll_for_new_otp(provider: FreeOtpReceiveProvider, phone: str,
                           page_id: str, baseline_sigs: set, timeout: int = 600):
    """Poll free-otp-receive.com number page for new messages carrying Trae OTP."""
    url = f"https://free-otp-receive.com/en/number/cn-{page_id}/"
    deadline = time.time() + timeout
    poll_interval = 8  # seconds (faster since platform is very active)
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
                # Return any new 6-digit OTP (Trae uses 6-digit)
                for m in new_msgs:
                    if m["otp"]:
                        log.info("  🎉 OTP found: %s (sender=%s)", m["otp"], m["sender"])
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


async def try_one_phone(phone: str, page_id: str, headed: bool, poll_timeout: int) -> dict:
    """Test one phone number end-to-end."""
    log.info("=" * 70)
    log.info("Testing phone %s (cn-%s)", phone, page_id)
    captured: list = []

    # Step 1: capture baseline messages
    msg_url = f"https://free-otp-receive.com/en/number/cn-{page_id}/"
    try:
        baseline_html = await fetch_page_html(msg_url)
        baseline_msgs, baseline_sigs = baseline_messages(baseline_html)
        log.info("Baseline: %d existing messages on free-otp-receive.com (cn-%s)",
                 len(baseline_msgs), page_id)
    except Exception as exc:
        log.error("Failed to fetch baseline: %s", exc)
        return {"phone": phone, "page_id": page_id, "error": f"baseline failed: {exc}"}

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
            await page.goto("https://www.trae.cn/login", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            # Fill phone (strip +86, take last 11 digits)
            phone_input = await page.query_selector("input.mobile-phone")
            if not phone_input:
                phone_input = await page.query_selector("input[type='text']")
            if not phone_input:
                return {"phone": phone, "page_id": page_id, "error": "Phone input not found"}

            raw = re.sub(r"^86|\+86|^00", "", phone.strip())
            digits = re.sub(r"\D", "", raw)
            clean_phone = digits[-11:] if len(digits) > 11 else digits
            await phone_input.click()
            await phone_input.fill(clean_phone)
            await page.wait_for_timeout(500)

            # Click send code button → may trigger captcha
            send_btn = await page.query_selector("div.send-code, [class*='send-code']")
            if not send_btn:
                for tag in ["div", "span", "button"]:
                    for el in await page.query_selector_all(tag):
                        if await el.is_visible():
                            text = (await el.inner_text()).strip()
                            if "获取验证码" in text:
                                send_btn = el
                                break
                    if send_btn:
                        break
            if not send_btn:
                return {"phone": phone, "page_id": page_id, "error": "Send code button not found"}

            await send_btn.click()
            log.info("Send code clicked. Waiting for captcha or response...")
            await page.wait_for_timeout(3000)

            # Try to solve captcha if present
            solver = AutoSlider()
            captcha_solved = False
            try:
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
                    captcha_solved = await solver.solve_existing(page)
                    log.info("Captcha solved: %s", captcha_solved)
                else:
                    log.info("No captcha iframe detected, send_code may have succeeded directly")
                    captcha_solved = True
            except Exception as exc:
                log.warning("Captcha solve error: %s", exc)

            # Step 3: Poll free-otp-receive.com for OTP
            otp = await poll_for_new_otp(None, phone, page_id, baseline_sigs, timeout=poll_timeout)

            # Step 4: collect captured responses
            send_code_resp = None
            for c in captured:
                if "send_code" in c["url"]:
                    send_code_resp = c
                    break

            return {
                "phone": phone,
                "page_id": page_id,
                "captcha_solved": captcha_solved,
                "send_code_response": send_code_resp,
                "otp_received": otp,
                "baseline_msg_count": len(baseline_msgs),
            }

        except Exception as exc:
            return {"phone": phone, "page_id": page_id, "error": str(exc)}
        finally:
            await browser.close()


async def main_async(max_phones: int, headed: bool, poll_timeout: int):
    log.info("=" * 70)
    log.info("Trae CN → free-otp-receive.com OTP delivery test")
    log.info("=" * 70)

    # Get numbers from FreeOtpReceiveProvider
    provider = FreeOtpReceiveProvider()
    numbers = provider.get_available_numbers()
    if not numbers:
        log.error("No +86 numbers available from free-otp-receive.com")
        return

    test_pool = numbers[:max_phones]
    log.info("Testing %d numbers: %s", len(test_pool),
             [f"{n.phone}({n.raw_label})" for n in test_pool])

    results = []
    for i, number in enumerate(test_pool):
        log.info("\n[%d/%d] Phone %s (%s, active=%s)", i + 1, len(test_pool),
                 number.phone, number.raw_label, number.last_active)
        result = await try_one_phone(number.phone, number.raw_label.replace("cn-", ""),
                                     headed, poll_timeout)
        results.append(result)

        if result.get("otp_received"):
            log.info("🎉🎉🎉 SUCCESS! OTP received for %s: %s",
                     result["phone"], result["otp"])
            break

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
    out = Path("freeotp_e2e_results.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Full results saved to %s", out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument("--max-phones", type=int, default=3,
                        help="Max number of phones to test (default: 3)")
    parser.add_argument("--poll", type=int, default=600,
                        help="Poll timeout per phone in seconds (default: 600)")
    args = parser.parse_args()
    asyncio.run(main_async(args.max_phones, args.headed, args.poll))


if __name__ == "__main__":
    main()
