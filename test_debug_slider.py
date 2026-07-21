"""
Debug: dump ALL elements in the captcha slider area to find the correct drag handle.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT = Path("slider_debug")
OUTPUT.mkdir(exist_ok=True)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        # Fill phone and trigger captcha
        phone = await page.query_selector('input[placeholder*="手机"]')
        if phone:
            await phone.fill("13800138000")

        send_btn = await page.query_selector(".send-code")
        if send_btn:
            await send_btn.click()

        # Wait for captcha
        captcha_frame = None
        for _ in range(30):
            for f in page.frames:
                if "verifycenter" in f.url:
                    captcha_frame = f
                    break
            if captcha_frame:
                break
            await page.wait_for_timeout(1000)

        if not captcha_frame:
            print("No captcha frame found")
            await browser.close()
            return

        await page.wait_for_timeout(3000)

        print("=== CAPTCHA FRAME URL ===")
        print(captcha_frame.url)

        print("\n=== ALL ELEMENTS IN CAPTCHA FRAME ===")
        all_elements = await captcha_frame.query_selector_all("*")
        print(f"Total elements: {len(all_elements)}")

        for el in all_elements:
            try:
                tag = await el.evaluate("e => e.tagName.toLowerCase()")
                cls = (await el.get_attribute("class")) or ""
                role = (await el.get_attribute("role")) or ""
                text = (await el.inner_text()).strip()[:80]
                visible = await el.is_visible()
                box = await el.bounding_box()
                box_str = f"box=({box['x']:.0f},{box['y']:.0f},{box['width']:.0f}x{box['height']:.0f})" if box else "no-box"
                aria = (await el.get_attribute("aria-label")) or ""
                
                # Filter to meaningful elements
                if visible and (role or text or "slide" in cls or "drag" in cls or "btn" in cls or "captcha" in cls):
                    print(f"  <{tag}> class='{cls[:60]}' role='{role}' text='{text[:50]}' {box_str} aria='{aria}'")
            except:
                pass

        # Specifically dump slider area
        print("\n=== SLIDER TRACK ELEMENTS ===")
        slider_track = await captcha_frame.query_selector(".captcha-slider")
        if slider_track:
            slider_els = await slider_track.query_selector_all("*")
            print(f"Elements in slider track: {len(slider_els)}")
            for el in slider_els:
                try:
                    tag = await el.evaluate("e => e.tagName.toLowerCase()")
                    cls = (await el.get_attribute("class")) or ""
                    text = (await el.inner_text()).strip()[:60]
                    box = await el.bounding_box()
                    role = (await el.get_attribute("role")) or ""
                    style = (await el.get_attribute("style")) or ""
                    box_str = f"box=({box['x']:.0f},{box['y']:.0f},{box['width']:.0f}x{box['height']:.0f})" if box else "no-box"
                    print(f"  <{tag}> class='{cls[:50]}' role='{role}' style='{style[:60]}' text='{text}' {box_str}")
                except:
                    pass

        # Check dragger-box
        print("\n=== DRAGGER BOX ===")
        dragger = await captcha_frame.query_selector(".dragger-box")
        if dragger:
            db = await dragger.bounding_box()
            print(f"dragger-box: {db}")
            children = await dragger.query_selector_all("*")
            for el in children:
                tag = await el.evaluate("e => e.tagName.toLowerCase()")
                cls = (await el.get_attribute("class")) or ""
                style = (await el.get_attribute("style")) or ""
                box = await el.bounding_box()
                print(f"  <{tag}> class='{cls[:50]}' style='{style[:60]}' box={box}")

        # Try to find anything with role="slider" or aria-valuenow
        print("\n=== ROLE=SLIDER / ARIA ELEMENTS ===")
        for el in all_elements:
            try:
                role = await el.get_attribute("role")
                aria_now = await el.get_attribute("aria-valuenow")
                if role or aria_now:
                    tag = await el.evaluate("e => e.tagName.toLowerCase()")
                    cls = (await el.get_attribute("class")) or ""
                    box = await el.bounding_box()
                    print(f"  <{tag}> class='{cls[:50]}' role='{role}' aria-valuenow='{aria_now}' box={box}")
            except:
                pass

        # Dump partial innerHTML of captcha slider area for analysis
        print("\n=== SLIDER TRACK INNER HTML ===")
        if slider_track:
            html = await slider_track.inner_html()
            print(html[:2000])

        await page.screenshot(path=str(OUTPUT / "debug.png"))
        print(f"\nScreenshots saved to {OUTPUT.resolve()}")

        await page.wait_for_timeout(30000)  # Keep alive 30s for manual inspection
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
