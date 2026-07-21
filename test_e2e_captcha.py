"""
E2E test: AutoSlider full flow on trae.cn/login.

Run:  python test_e2e_captcha.py [--headed]
"""
import asyncio
import logging
import sys

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("e2e")
logging.getLogger("playwright").setLevel(logging.WARNING)


async def main():
    headed = "--headed" in sys.argv
    phone = "8619604191344"

    from trae_cn_manager.auto_slider import AutoSlider

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headed,
            proxy={"server": "http://127.0.0.1:7897"},
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

        try:
            log.info("Navigating to trae.cn/login...")
            await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # AutoSlider.solve() does: fill phone → click send → wait captcha → detect → drag → verify
            log.info("=== AutoSlider.solve() ===")
            solver = AutoSlider()
            captcha_ok = await solver.solve(page, phone)
            log.info("Result: captcha_ok=%s", captcha_ok)

            if captcha_ok:
                print("\n*** CAPTCHA SOLVED ***\n")
            else:
                print("\n*** CAPTCHA FAILED ***\n")

            if headed:
                await page.wait_for_timeout(120000)  # keep browser open for observation
            else:
                await page.wait_for_timeout(5000)

        except Exception:
            log.exception("E2E test crashed")
            if headed:
                await page.wait_for_timeout(60000)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
