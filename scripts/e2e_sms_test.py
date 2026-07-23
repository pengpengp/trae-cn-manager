"""End-to-end test: use new GoinSmsProvider number + intercept send_code.

Strategy:
  1. Get a number from GoinSmsProvider (or ReceiveSmsFreeProvider)
  2. Open trae.cn/login with Playwright (visible)
  3. Fill phone, click send code
  4. Auto-solve slider (using existing AutoSlider)
  5. Intercept ALL network requests related to send_code/captcha
  6. Wait 60s for OTP on the SMS provider
  7. Print captured requests so we can see:
     - First send_code response (should be 1105)
     - Slider verify response (should sign mobile_ticket)
     - Second send_code request payload (must include mobile_ticket)
     - Second send_code response (should be success)
"""
import asyncio
import json
import logging
import os
import sys
from datetime import datetime

os.environ["TCN_PROXY"] = "none"  # direct connection (DNS already fixed via hosts)
sys.path.insert(0, r"C:\Users\ioio\trae-cn-manager")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("e2e")

from playwright.async_api import async_playwright
from trae_cn_manager.sms_client import GoinSmsProvider, ReceiveSmsFreeProvider
from trae_cn_manager.auto_slider import AutoSlider


# Captured network data
captured_requests: list[dict] = []
captured_responses: list[dict] = []


async def on_request(request):
    url = request.url
    if any(kw in url for kw in ["send_code", "passport/web", "captcha", "verify"]):
        try:
            post_data = request.post_data
        except Exception:
            post_data = None
        captured_requests.append({
            "url": url,
            "method": request.method,
            "post_data": post_data[:500] if post_data else None,
            "headers": dict(request.headers),
        })
        log.info("REQ  %s %s  post=%s", request.method, url[:100],
                 post_data[:200] if post_data else "(no body)")


async def on_response(response):
    url = response.url
    if any(kw in url for kw in ["send_code", "passport/web", "captcha", "verify"]):
        try:
            body = await response.text()
        except Exception:
            body = "<binary>"
        captured_responses.append({
            "url": url,
            "status": response.status,
            "body": body[:1000],
        })
        log.info("RESP %d %s  body=%s", response.status, url[:100], body[:300])


async def main():
    # Step 1: Get a number from GoinSmsProvider (or ReceiveSmsFreeProvider as fallback)
    log.info("=== Step 1: Get a number from SMS providers ===")
    sms = GoinSmsProvider()
    numbers = sms.get_available_numbers()
    if not numbers:
        log.warning("GoinSmsProvider returned 0, falling back to ReceiveSmsFreeProvider")
        sms = ReceiveSmsFreeProvider()
        numbers = sms.get_available_numbers()
    if not numbers:
        log.error("No numbers available from any provider!")
        return

    # Pick first number (already used in test, OK for slider debugging)
    target = numbers[0]
    phone = target.phone
    log.info("Selected phone: %s  carrier=%s", phone, target.carrier)
    # Get baseline messages so we know what was already there before
    baseline = sms.get_messages(phone)
    log.info("Baseline: %d existing messages on %s", len(baseline), phone)

    # Step 2: Open trae.cn/login with Playwright
    log.info("\n=== Step 2: Open trae.cn/login ===")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,  # visible for debugging
            args=["--disable-blink-features=AutomationControlled"],
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
        # Attach network listeners
        context.on("request", lambda r: asyncio.create_task(on_request(r)))
        context.on("response", lambda r: asyncio.create_task(on_response(r)))

        page = await context.new_page()

        try:
            await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Step 3: Fill phone, click send code
            log.info("\n=== Step 3: Fill phone + click send code ===")
            phone_input = await page.query_selector("input.mobile-phone")
            if not phone_input:
                phone_input = await page.query_selector("input[type='text']")
            if not phone_input:
                log.error("Phone input not found")
                return
            await phone_input.click()
            # Format phone for input: strip leading 86 (Trae CN form expects 11-digit local)
            phone_local = phone[2:] if phone.startswith("86") else phone
            await phone_input.fill(phone_local)
            await page.wait_for_timeout(500)

            send_btn = await page.query_selector("div.send-code")
            if not send_btn:
                log.error("Send code button not found")
                return
            await send_btn.click()
            log.info("Send code clicked — waiting for slider...")

            # Step 4: Auto-solve slider
            log.info("\n=== Step 4: Auto-solve slider ===")
            solver = AutoSlider()
            captcha_ok = await solver.solve(page, phone_local)
            log.info("AutoSlider result: %s", captcha_ok)

            # Step 5: Wait 60s for OTP
            log.info("\n=== Step 5: Poll SMS for OTP (60s) ===")
            otp = sms.wait_for_otp(phone, timeout=60, poll_interval=5)
            if otp:
                log.info("GOT OTP: %s", otp)
            else:
                log.warning("No OTP received within 60s")

        except Exception as e:
            log.error("Error during e2e test: %s", e)
            import traceback
            traceback.print_exc()
        finally:
            # Always print captured network info
            log.info("\n\n=== CAPTURED NETWORK LOG ===")
            log.info("\n--- Requests (%d) ---", len(captured_requests))
            for i, r in enumerate(captured_requests):
                log.info("[%d] %s %s", i, r["method"], r["url"])
                if r["post_data"]:
                    log.info("    post: %s", r["post_data"])
            log.info("\n--- Responses (%d) ---", len(captured_responses))
            for i, r in enumerate(captured_responses):
                log.info("[%d] %d %s", i, r["status"], r["url"])
                log.info("    body: %s", r["body"][:500])

            # Save full capture to file
            with open(r"C:\Users\ioio\captured_network.json", "w", encoding="utf-8") as f:
                json.dump({
                    "requests": captured_requests,
                    "responses": captured_responses,
                    "phone": phone,
                    "baseline_msgs": len(baseline),
                    "otp_received": otp,
                    "timestamp": datetime.now().isoformat(),
                }, f, indent=2, ensure_ascii=False)
            log.info("Saved to C:\\Users\\ioio\\captured_network.json")

            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
