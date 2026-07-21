"""
Debug send code + network requests on trae.cn/login.
Checks what API calls happen when clicking send code.
"""
import asyncio
import json
import logging
import re
import sys

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("debug")
logging.getLogger("playwright").setLevel(logging.WARNING)

# Force UTF-8 for output
sys.stdout.reconfigure(encoding="utf-8")


async def main():
    headed = "--headed" in sys.argv

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headed,
            proxy={"server": "http://127.0.0.1:7897"},
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        page = await context.new_page()

        # Track network requests
        api_calls = []

        def on_response(resp):
            url = resp.url
            if any(k in url for k in ["passport", "send_code", "verify", "captcha", "trae.cn"]):
                api_calls.append({
                    "url": url,
                    "status": resp.status,
                    "method": resp.request.method,
                })

        page.on("response", on_response)

        try:
            await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Fill phone
            phone_input = await page.query_selector("input.mobile-phone")
            assert phone_input
            await phone_input.click()
            await phone_input.fill("8619604191344")
            await page.wait_for_timeout(500)

            # Check page HTML for captcha-related elements
            html = await page.content()

            # Find captcha iframe in HTML
            iframe_matches = re.findall(r'<iframe[^>]*src=["\']([^"\']*verify[^"\']*)["\']', html)
            log.info("Captcha-related iframes in HTML: %s", iframe_matches)

            # Find captcha container
            container_match = re.search(r'id=["\']captcha_container["\']', html)
            log.info("captcha_container in HTML: %s", container_match is not None)

            # Check for Bytedance verifycenter script
            vc_match = "verifycenter" in html
            log.info("verifycenter in HTML: %s", vc_match)

            # Click send code
            send_btn = await page.query_selector("div.send-code")
            assert send_btn
            log.info("Clicking send code...")
            await send_btn.click()
            await page.wait_for_timeout(5000)

            log.info("--- API calls after click ---")
            for c in api_calls:
                log.info("  %s %s -> %s", c["method"], c["url"][:100], c["status"])

            # Check if any new frames appeared
            log.info("--- Frames ---")
            for f in page.frames:
                log.info("  %s", f.url[:120])

            # Check button text
            try:
                btn_text = await send_btn.inner_text()
                log.info("Send button text after click: [%s]", btn_text)
            except Exception as e:
                log.info("Send button gone: %s", e)

            # Check for visible error
            err_els = await page.query_selector_all("[class*='error'], [class*='err-'], .toast, .message")
            for el in err_els:
                if await el.is_visible():
                    log.info("Visible error: [%s]", await el.inner_text())

            await page.screenshot(path="debug_send_code.png")
            log.info("Screenshot saved")

            if headed:
                await page.wait_for_timeout(120000)
            else:
                await page.wait_for_timeout(5000)

        except Exception:
            log.exception("Debug crashed")
            if headed:
                await page.wait_for_timeout(60000)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
