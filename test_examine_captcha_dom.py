"""
Examine captcha frame DOM — find actual gap position info.
"""
import asyncio
import logging
import sys

from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
sys.stdout.reconfigure(encoding="utf-8")
log = logging.getLogger("dom")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            proxy={"server": "http://127.0.0.1:7897"},
        )
        page = await browser.new_page(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )

        await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        phone = await page.query_selector("input.mobile-phone")
        await phone.fill("19604191344")
        send = await page.query_selector("div.send-code")
        await send.click()
        await page.wait_for_timeout(5000)

        # Find captcha frame
        captcha_frame = None
        for f in page.frames:
            if "verifycenter" in f.url:
                captcha_frame = f
                break
        if not captcha_frame:
            print("No captcha frame found")
            await browser.close()
            return

        print(f"\nCaptcha URL: {captcha_frame.url}")

        # Dump all element positions and attributes
        info = await captcha_frame.evaluate("""() => {
            const r = {
                elements: [],
                styles: {},
                transforms: [],
                canvasInfo: null,
            };

            // 1. All .dragger-item positions + styles
            const draggers = document.querySelectorAll('.dragger-item, [class*=dragger]');
            draggers.forEach((el, i) => {
                const rect = el.getBoundingClientRect();
                const style = el.getAttribute('style') || '';
                const cls = el.className;
                const transform = window.getComputedStyle(el).transform;
                const left = window.getComputedStyle(el).left;
                r.elements.push({
                    tag: el.tagName,
                    class: cls,
                    rect: {top: rect.top, left: rect.left, width: rect.width, height: rect.height},
                    style: style.substring(0, 200),
                    transform: transform,
                    left: left,
                });
            });

            // 2. Canvas info if present
            const canvases = document.querySelectorAll('canvas');
            canvases.forEach((c, i) => {
                r.canvasInfo = {width: c.width, height: c.height, index: i};
            });

            // 3. Any element with 'verify' in class or related to the gap
            const allDivs = document.querySelectorAll('div, img, span');
            allDivs.forEach(el => {
                const cls = el.className;
                if (typeof cls === 'string' && (
                    cls.includes('gap') || cls.includes('target') || cls.includes('verify') ||
                    cls.includes('slider') || cls.includes('puzzle') || cls.includes('piece')
                )) {
                    const rect = el.getBoundingClientRect();
                    r.elements.push({
                        tag: el.tagName,
                        class: cls,
                        rect: {top: rect.top, left: rect.left, width: rect.width, height: rect.height},
                        text: (el.textContent || '').substring(0, 100),
                    });
                }
            });

            // 4. Full HTML of captcha body
            r.bodyHTML = document.body ? document.body.innerHTML.substring(0, 8000) : '';

            return r;
        }""")

        print("\n=== Dragger elements ===")
        for el in info.get("elements", []):
            if "dragger" in el.get("class", "").lower():
                print(f"  class={el['class']}")
                print(f"  rect={el['rect']}")
                print(f"  style={el['style']}")
                print(f"  transform={el['transform']}")
                print(f"  left={el['left']}")
                print()

        print("\n=== Gap/target related elements ===")
        for el in info.get("elements", []):
            if any(k in el.get("class", "").lower() for k in ["gap", "target", "verify", "slider"]):
                if "dragger" not in el.get("class", "").lower():
                    print(f"  tag={el['tag']} class={el['class']}")
                    print(f"  rect={el['rect']}")
                    print(f"  text={el.get('text', '')}")
                    print()

        print("\n=== Canvas ===")
        print(f"  {info.get('canvasInfo')}")

        # Save HTML for inspection
        with open("captcha_body.html", "w", encoding="utf-8") as f:
            f.write(info.get("bodyHTML", ""))
        print("\nFull captcha HTML saved to captcha_body.html")
        print(f"Body HTML length: {len(info.get('bodyHTML', ''))} chars")

        await page.wait_for_timeout(5000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
