"""
Debug: check if Playwright mouse events reach the captcha iframe.
"""
import asyncio
import json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})

        # Go to login
        await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        phone = await page.query_selector('input[placeholder*="手机"]')
        if phone:
            await phone.fill("13800138000")
        send_btn = await page.query_selector(".send-code")
        if send_btn:
            await send_btn.click()

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
            print("No captcha")
            await browser.close()
            return

        await page.wait_for_timeout(3000)

        # Check iframe properties
        print("=== IFRAME PROPERTIES ===")
        iframe_el = await page.query_selector('iframe[src*="verifycenter"]')
        if iframe_el:
            props = await iframe_el.evaluate("""el => {
                return {
                    src: el.src,
                    sandbox: el.getAttribute('sandbox'),
                    allow: el.getAttribute('allow'),
                    referrerPolicy: el.getAttribute('referrerpolicy'),
                    loading: el.getAttribute('loading'),
                    width: el.width,
                    height: el.height,
                    style: el.getAttribute('style'),
                };
            }""")
            for k, v in props.items():
                print(f"  {k}: {v}")
            box = await iframe_el.bounding_box()
            print(f"  bounding_box: {box}")

        # Add pointer event listener in iframe
        print("\n=== SETTING UP EVENT MONITORING ===")
        await captcha_frame.evaluate("""
            window.__events_received = [];
            window.__event_types = {};
            
            function logEvent(e) {
                const key = e.type;
                window.__event_types[key] = (window.__event_types[key] || 0) + 1;
                if (['pointerdown', 'pointermove', 'pointerup', 'mousedown', 'mousemove', 'mouseup'].includes(e.type)) {
                    window.__events_received.push({
                        type: e.type,
                        x: e.clientX,
                        y: e.clientY,
                        trusted: e.isTrusted,
                        time: Date.now()
                    });
                }
            }
            
            document.addEventListener('pointerdown', logEvent, true);
            document.addEventListener('pointermove', logEvent, true);
            document.addEventListener('pointerup', logEvent, true);
            document.addEventListener('mousedown', logEvent, true);
            document.addEventListener('mousemove', logEvent, true);
            document.addEventListener('mouseup', logEvent, true);
            
            console.log('Event listeners installed');
            'listeners_installed';
        """)

        # Check for slider button position
        btn_data = await captcha_frame.evaluate("""() => {
            const btn = document.querySelector('.captcha-slider-btn');
            const slider = document.querySelector('.captcha-slider');
            const dragger = document.querySelector('.dragger-item');
            if (!btn || !slider) return null;
            const br = btn.getBoundingClientRect();
            const sr = slider.getBoundingClientRect();
            const dr = dragger ? dragger.getBoundingClientRect() : null;
            return {
                btn: {x: br.left, y: br.top, w: br.width, h: br.height},
                slider: {x: sr.left, y: sr.top, w: sr.width, h: sr.height},
                dragger: dr ? {x: dr.left, y: dr.top, w: dr.width, h: dr.height} : null,
                btnCenter: {x: br.left + br.width/2, y: br.top + br.height/2}
            };
        }""")
        print(f"\n  Button: {btn_data['btn']}")
        print(f"  Slider track: {btn_data['slider']}")
        print(f"  Button center: {btn_data['btnCenter']}")

        # Now use page.mouse to interact - these generate TRUSTED events
        print("\n=== TESTING PAGE.MOUSE ===")
        
        center_x = btn_data['btnCenter']['x']
        center_y = btn_data['btnCenter']['y']
        
        print(f"  Moving to button: ({center_x:.0f}, {center_y:.0f})")
        
        # Clear event log
        await captcha_frame.evaluate("window.__events_received = []; Object.keys(window.__event_types).forEach(k => delete window.__event_types[k]);")
        
        # Move mouse to button position
        await page.mouse.move(center_x, center_y)
        await page.wait_for_timeout(200)
        
        events = await captcha_frame.evaluate("window.__events_received.slice(-20)")
        event_types = await captcha_frame.evaluate("window.__event_types")
        print(f"  Events received during mouse.move: {event_types}")
        print(f"  Sample events: {json.dumps(events[-5:], indent=2)}" if events else "  No events received!")
        
        if not events:
            print("  ❌ MOUSE EVENTS NOT REACHING IFRAME!")
        else:
            # Check if trusted
            trusted = all(e['trusted'] for e in events)
            print(f"  All trusted: {trusted}")

        # Now click (mousedown + mouseup)
        print("\n  Sending mousedown...")
        await captcha_frame.evaluate("window.__events_received = [];")
        await page.mouse.down()
        await page.wait_for_timeout(100)
        
        down_events = await captcha_frame.evaluate("window.__events_received")
        down_types = await captcha_frame.evaluate("window.__event_types")
        print(f"  Events on down: {down_types}")
        
        # Move
        print("  Moving 50px right...")
        await captcha_frame.evaluate("window.__events_received = [];")
        await page.mouse.move(center_x + 50, center_y)
        await page.wait_for_timeout(100)
        
        move_events = await captcha_frame.evaluate("window.__events_received")
        move_types = await captcha_frame.evaluate("window.__event_types")
        print(f"  Events on move: {move_types}")
        print(f"  Move events count: {len(move_events)}")
        if move_events:
            print(f"  First: type={move_events[0]['type']} x={move_events[0]['x']:.0f} trusted={move_events[0]['trusted']}")
        
        # Release
        print("  mouseup...")
        await page.mouse.up()
        await page.wait_for_timeout(100)
        
        up_types = await captcha_frame.evaluate("window.__event_types")
        print(f"  Final event counts: {up_types}")

        # Also check: does the captcha respond to the interaction?
        print("\n=== CAPTCHA RESPONSE ===")
        puzzle_after = await captcha_frame.evaluate("""() => {
            const all = document.querySelectorAll('.dragger-item');
            const results = [];
            for (const e of all) {
                const r = e.getBoundingClientRect();
                results.push({
                    x: r.left, y: r.top, w: r.width, h: r.height,
                    style: e.getAttribute('style')
                });
            }
            return results;
        }""")
        print(f"  Puzzle elements: {puzzle_after}")
        
        # If events aren't reaching, try a different approach
        if not events:
            print("\n⚠️ Events NOT reaching iframe - trying alternative...")
            print("This could be due to iframe sandbox or cross-origin restrictions.")
            
            # Check the iframe element's frame
            actual_frame = captcha_frame
            print(f"  Frame URL: {actual_frame.url[:80]}")
            print(f"  Frame name: '{actual_frame.name}'")

        await page.wait_for_timeout(5000)
        await browser.close()

import json
if __name__ == "__main__":
    asyncio.run(main())
