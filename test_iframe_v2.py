"""
Debug v2: verify if Playwright page.mouse events reach captcha iframe.
Uses PAGE coordinates (from bounding_box), not iframe-relative coordinates.
"""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})

        await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        phone = await page.query_selector('input[placeholder*="手机"]')
        if phone: await phone.fill("13800138000")
        send_btn = await page.query_selector(".send-code")
        if send_btn: await send_btn.click()

        captcha_frame = None
        for _ in range(30):
            for f in page.frames:
                if "verifycenter" in f.url:
                    captcha_frame = f
                    break
            if captcha_frame: break
            await page.wait_for_timeout(1000)

        if not captcha_frame:
            print("No captcha")
            await browser.close()
            return

        await page.wait_for_timeout(3000)

        # Install event listeners in iframe (using function, not arrow)
        await captcha_frame.evaluate("""
            window.__evts = [];
            window.__evtCounts = {};
            function capture(e) {
                var type = e.type;
                window.__evtCounts[type] = (window.__evtCounts[type] || 0) + 1;
                if (type.indexOf('mouse') >= 0 || type.indexOf('pointer') >= 0) {
                    window.__evts.push({
                        type: type,
                        x: e.clientX,
                        y: e.clientY,
                        trusted: e.isTrusted,
                        target: e.target ? e.target.className : '',
                        ts: Date.now()
                    });
                }
            }
            var events = ['pointerdown','pointermove','pointerup','mousedown','mousemove','mouseup'];
            for (var i = 0; i < events.length; i++) {
                document.addEventListener(events[i], capture, true);
            }
        """)

        # Get elements and their PAGE coordinates via bounding_box
        slider_btn = await captcha_frame.query_selector(".captcha-slider-btn")
        bg_img = await captcha_frame.query_selector("#captcha_verify_image")
        
        btn_box = await slider_btn.bounding_box()
        bg_box = await bg_img.bounding_box()
        
        print(f"Iframe page position: ({btn_box['x'] - 22:.0f}, {btn_box['y'] - 287:.0f})")
        print(f"Button page box: ({btn_box['x']:.0f}, {btn_box['y']:.0f}, {btn_box['width']:.0f}x{btn_box['height']:.0f})")
        print(f"Image page box: ({bg_box['x']:.0f}, {bg_box['y']:.0f}, {bg_box['width']:.0f}x{bg_box['height']:.0f})")

        # Clear event log
        await captcha_frame.evaluate("window.__evts = []; window.__evtCounts = {};")

        # Move mouse to button center (PAGE coordinates from bounding_box)
        cx = btn_box['x'] + btn_box['width'] / 2
        cy = btn_box['y'] + btn_box['height'] / 2
        print(f"\nMoving to button center (page coords): ({cx:.0f}, {cy:.0f})")
        
        await page.mouse.move(cx, cy)
        await page.wait_for_timeout(300)
        
        # Check events
        counts = await captcha_frame.evaluate("window.__evtCounts")
        evts = await captcha_frame.evaluate("window.__evts.slice(-10)")
        print(f"Events after move: {counts}")
        if evts:
            for e in evts:
                print(f"  {e['type']} at ({e['x']:.0f},{e['y']:.0f}) trusted={e['trusted']}")
        else:
            print("  NO EVENTS RECEIVED IN IFRAME!")
            print("  Trying with page.dispatchEvent...")
            
            # Try dispatching directly on the page
            await page.evaluate("""(x, y) => {
                var evt = new MouseEvent('mousemove', {clientX: x, clientY: y, bubbles: true, cancelable: true});
                document.elementFromPoint(x, y)?.dispatchEvent(evt);
            }""", cx, cy)
            await page.wait_for_timeout(200)
            counts2 = await captcha_frame.evaluate("window.__evtCounts")
            print(f"  Events after dispatchEvent: {counts2}")

        # If still no events, try clicking directly
        if not evts:
            print("\nTrying page.mouse.click...")
            await page.mouse.click(cx, cy)
            await page.wait_for_timeout(200)
            counts3 = await captcha_frame.evaluate("window.__evtCounts")
            print(f"  Events after click: {counts3}")

        # Also check: maybe the iframe uses capture phase on a specific element
        print("\nChecking captcha container element...")
        container_info = await captcha_frame.evaluate("""() => {
            var el = document.querySelector('#vc_captcha_box');
            if (!el) return 'NO CONTAINER';
            var rect = el.getBoundingClientRect();
            return {
                className: el.className,
                rect: {x: rect.left, y: rect.top, w: rect.width, h: rect.height}
            };
        }""")
        print(f"  Container: {container_info}")

        await page.wait_for_timeout(5000)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
