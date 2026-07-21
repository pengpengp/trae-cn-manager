"""
Debug: what happens after clicking "send code" on trae.cn/login?

Checks for errors, captcha containers, network responses.
"""
import asyncio
import logging
import sys

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("debug")
logging.getLogger("playwright").setLevel(logging.WARNING)


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

        # Log all console messages
        page.on("console", lambda msg: log.debug("PAGE: %s", msg.text))
        # Log all failed requests
        page.on("request_failed", lambda req: log.warning(
            "REQUEST FAILED: %s %s -> %s", req.method, req.url, req.failure
        ))

        try:
            await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Fill phone
            phone_input = await page.query_selector("input.mobile-phone")
            assert phone_input, "Phone input not found"
            await phone_input.click()
            await phone_input.fill("8619604191344")
            await page.wait_for_timeout(500)

            # Click send code
            send_btn = await page.query_selector("div.send-code")
            assert send_btn, "Send code button not found"

            # Check send button text before clicking
            before_text = await send_btn.inner_text()
            log.info("Send button text before click: '%s'", before_text)

            await send_btn.click()
            log.info("Clicked send code. Waiting 10s for captcha...")

            for i in range(10):
                await page.wait_for_timeout(1000)

                # Check iframe
                frames_info = []
                for f in page.frames:
                    frames_info.append(f.url[:80])
                log.info("t=%ds  frames: %s", i + 1, frames_info)

                # Check visible elements
                captcha_container = await page.query_selector("#captcha_container")
                has_container = captcha_container is not None

                phone_err = await page.query_selector(".phone-error, [class*='error']")
                err_text = await phone_err.inner_text() if phone_err else ""

                btn_text = ""
                try:
                    btn_text = await send_btn.inner_text()
                except Exception:
                    btn_text = "DETACHED"

                log.info("  captcha_container=%s  err='%s'  btn='%s'",
                         has_container, err_text, btn_text)

                # Check full page HTML for captcha-related content
                if i == 0:
                    html = await page.content()
                    if "verifycenter" in html:
                        log.info("'verifycenter' found in page HTML!")
                    if "captcha" in html.lower():
                        log.info("'captcha' found in page HTML!")

            # Check page screenshot for reference
            await page.screenshot(path="debug_captcha.png")
            log.info("Screenshot saved to debug_captcha.png")

            print("\nCheck logs above for debug info.\n")

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
