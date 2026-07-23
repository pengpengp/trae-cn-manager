"""Multi-phone end-to-end test: try 3 different numbers with longer wait.

Strategy:
  1. Get ALL available numbers from GoinSmsProvider + ReceiveSmsFreeProvider
  2. Filter to real-segment numbers (13x/15x/17x/18x — skip 180/181/196/197)
  3. For each of top 3 candidates:
     a. Open trae.cn/login
     b. Fill phone, click send code
     c. Auto-solve slider
     d. Wait 120s for OTP (polling every 10s)
     e. If OTP received, declare success and stop
     f. If not, try next number
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime

os.environ["TCN_PROXY"] = "none"
sys.path.insert(0, r"C:\Users\ioio\trae-cn-manager")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("multi")

from playwright.async_api import async_playwright
from trae_cn_manager.sms_client import GoinSmsProvider, ReceiveSmsFreeProvider
from trae_cn_manager.auto_slider import AutoSlider


# Real segments only (skip IoT 180/181/196/197/198/199)
REAL_SEG_PREFIXES = (
    "130","131","132","133","134","135","136","137","138","139",
    "150","151","152","153","155","156","157","158","159",
    "170","171","172","173","175","176","177","178",
    "182","183","184","185","186","187","188","189",
)

# IoT segments (to skip)
IOT_SEG_PREFIXES = ("180","181","190","191","192","193","195","196","197","198","199")


def is_real_segment(phone: str) -> bool:
    """Check if phone (with 86 prefix) has a real segment."""
    if not phone.startswith("86") or len(phone) < 5:
        return False
    seg3 = phone[2:5]
    return seg3 in REAL_SEG_PREFIXES


async def try_one_number(phone: str, sms_provider, browser_factory) -> tuple[bool, str, list]:
    """Try to register with one phone number. Returns (success, otp, network_log)."""
    phone_local = phone[2:] if phone.startswith("86") else phone
    captured = []

    async def on_response(response):
        url = response.url
        if any(kw in url for kw in ["send_code", "captcha/verify"]):
            try:
                body = await response.text()
            except Exception:
                body = "<binary>"
            captured.append({"url": url, "status": response.status, "body": body[:500]})

    log.info("--- Trying phone: %s (segment=%s) ---", phone, phone[2:5])
    # Baseline
    baseline = sms_provider.get_messages(phone)
    log.info("  Baseline: %d existing messages", len(baseline))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        )
        context.on("response", lambda r: asyncio.create_task(on_response(r)))
        page = await context.new_page()

        try:
            await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            phone_input = await page.query_selector("input.mobile-phone") or await page.query_selector("input[type='text']")
            if not phone_input:
                return False, "no phone input", captured
            await phone_input.click()
            await phone_input.fill(phone_local)
            await page.wait_for_timeout(500)

            send_btn = await page.query_selector("div.send-code")
            if not send_btn:
                return False, "no send button", captured
            await send_btn.click()
            log.info("  Send code clicked, solving slider...")

            solver = AutoSlider()
            captcha_ok = await solver.solve(page, phone_local)
            log.info("  Slider result: %s", captcha_ok)

            if not captcha_ok:
                return False, "slider failed", captured

            # Wait for OTP with 120s timeout
            log.info("  Polling SMS for OTP (120s)...")
            otp = sms_provider.wait_for_otp(phone, timeout=120, poll_interval=10)
            if otp:
                return True, otp, captured
            return False, "no OTP after 120s", captured

        except Exception as e:
            return False, f"exception: {e!r}", captured
        finally:
            await browser.close()
            # Small delay between tests
            await asyncio.sleep(2)


async def main():
    log.info("=== Collecting phone numbers from both providers ===")
    all_numbers = []
    for cls in [GoinSmsProvider, ReceiveSmsFreeProvider]:
        try:
            p = cls()
            nums = p.get_available_numbers()
            log.info("  %s: %d numbers", cls.__name__, len(nums))
            all_numbers.extend(nums)
        except Exception as e:
            log.warning("  %s failed: %s", cls.__name__, e)

    # Filter to real-segment numbers, dedupe
    seen = set()
    real_numbers = []
    for n in all_numbers:
        if n.phone in seen:
            continue
        seen.add(n.phone)
        if is_real_segment(n.phone):
            real_numbers.append(n)

    log.info("\n=== Filtered to %d real-segment numbers ===", len(real_numbers))
    for n in real_numbers[:10]:
        log.info("  %s  seg=%s  carrier=%s", n.phone, n.phone[2:5], n.carrier)

    if not real_numbers:
        log.error("No real-segment numbers available!")
        return

    # Pick the SMS provider that has each number (check by re-querying)
    # For simplicity, use the first provider that has messages for the number
    # We'll try ReceiveSmsFreeProvider first since it has more numbers
    sms_provider = ReceiveSmsFreeProvider()

    # Try top 3 real-segment numbers from ReceiveSmsFreeProvider
    target_numbers = real_numbers[:3]
    log.info("\n=== Will try %d numbers ===", len(target_numbers))

    results = []
    for i, n in enumerate(target_numbers):
        log.info("\n########## ATTEMPT %d/%d ##########", i + 1, len(target_numbers))
        ok, msg, captured = await try_one_number(n.phone, sms_provider, None)
        results.append({
            "phone": n.phone,
            "segment": n.phone[2:5],
            "carrier": n.carrier,
            "success": ok,
            "message": msg,
            "captured_count": len(captured),
        })
        if ok:
            log.info("🎉  SUCCESS on %s — OTP=%s", n.phone, msg)
            break
        else:
            log.warning("❌  FAILED on %s — %s", n.phone, msg)

    # Summary
    log.info("\n\n========== SUMMARY ==========")
    for r in results:
        status = "✅ PASS" if r["success"] else "❌ FAIL"
        log.info("  %s  %s  seg=%s  carrier=%s  msg=%s  captured_reqs=%d",
                 status, r["phone"], r["segment"], r["carrier"], r["message"], r["captured_count"])

    # Save
    with open(r"C:\Users\ioio\multi_phone_results.json", "w", encoding="utf-8") as f:
        json.dump({
            "results": results,
            "timestamp": datetime.now().isoformat(),
        }, f, indent=2, ensure_ascii=False)
    log.info("Saved to C:\\Users\\ioio\\multi_phone_results.json")


if __name__ == "__main__":
    asyncio.run(main())
