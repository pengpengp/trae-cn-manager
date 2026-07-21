"""
Complete drag test: puzzle piece should move when we drag the slider button.
"""
import asyncio
import base64
import random
import json
from pathlib import Path
import cv2
import numpy as np
from playwright.async_api import async_playwright

OUTPUT = Path("drag_test")
OUTPUT.mkdir(exist_ok=True)

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

        # ---- Detect gap ----
        print("Detecting gap...")
        bg_img = await captcha_frame.query_selector("#captcha_verify_image")
        bg_src = await bg_img.get_attribute("src")
        
        resp = await page.context.request.get(bg_src)
        bg_bytes = await resp.body()
        (OUTPUT / "bg.jpg").write_bytes(bg_bytes)

        img = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
        natural_w, natural_h = img.shape[1], img.shape[0]

        from captcha_recognizer.slider import Slider
        model = Slider()
        model_offset, model_conf = model.identify_offset(source=str(OUTPUT / "bg.jpg"), show=False)
        print(f"  Model offset={model_offset:.1f} conf={model_conf:.3f}")

        bg_box = await bg_img.bounding_box()
        display_w = bg_box["width"]
        model_scale = min(640 / natural_w, 640 / natural_h)
        offset_natural = (model_offset) / model_scale  # pad_left is ~0 since width fills 640
        display_offset = offset_natural * (display_w / natural_w)
        print(f"  Display offset: {display_offset:.1f}px")

        # ---- Get elements ----
        slider_btn = await captcha_frame.query_selector(".captcha-slider-btn")
        btn_box = await slider_btn.bounding_box()
        
        # Record initial puzzle position
        initial_state = await captcha_frame.evaluate("""() => {
            var all = document.querySelectorAll('.dragger-item');
            var result = [];
            for (var i = 0; i < all.length; i++) {
                var r = all[i].getBoundingClientRect();
                var style = all[i].getAttribute('style') || '';
                result.push({x: r.left, y: r.top, w: r.width, h: r.height, style: style});
            }
            return result;
        }""")
        print(f"\nInitial dragger positions:")
        for i, s in enumerate(initial_state):
            print(f"  [{i}] ({s['x']:.0f},{s['y']:.0f}) {s['style'][:60]}")

        # ---- DRAG using page.mouse (trusted events, page coordinates) ----
        start_x = btn_box['x'] + btn_box['width'] / 2
        start_y = btn_box['y'] + btn_box['height'] / 2
        end_x = start_x + display_offset

        print(f"\nDrag: ({start_x:.0f},{start_y:.0f}) -> ({end_x:.0f},{start_y:.0f}) = {display_offset:.1f}px")

        # 1. Move to button
        await page.mouse.move(start_x, start_y)
        await page.wait_for_timeout(300)

        # 2. Press
        await page.mouse.down()
        await page.wait_for_timeout(50)

        # 3. Move in steps (human-like)
        steps = []
        dist = display_offset
        # Accelerate
        p1 = dist * 0.55
        x = 0
        while x < p1:
            step = max(2, int((p1 - x) * random.uniform(0.08, 0.18)))
            x += step
            steps.append(x)
        # Decelerate
        while x < dist * 0.92:
            step = max(1, int((dist * 0.92 - x) * random.uniform(0.03, 0.10)))
            x += step
            steps.append(x)
        # Fine
        while x < dist:
            step = max(1, int((dist - x) * random.uniform(0.01, 0.04)))
            x += step
            steps.append(x)
        # Overshoot maybe
        if random.random() < 0.2:
            for _ in range(3):
                steps.append(dist + random.uniform(2, 6))
            for _ in range(3):
                steps.append(dist + random.uniform(-2, 1))
        # Settle
        for _ in range(3):
            steps.append(dist + random.uniform(-1, 1))

        for x_offset in steps:
            await page.mouse.move(
                start_x + x_offset + random.uniform(-0.3, 0.3),
                start_y + random.choice([-1, 0, 0, 1]) + random.uniform(-0.3, 0.3),
                steps=1,
            )
            await page.wait_for_timeout(random.randint(8, 25))

        # Final position
        await page.wait_for_timeout(50)
        await page.mouse.move(end_x, start_y + random.uniform(-0.5, 0.5))
        await page.wait_for_timeout(100)
        
        # 4. Release
        await page.mouse.up()
        await page.wait_for_timeout(500)

        # ---- Check result ----
        await page.screenshot(path=str(OUTPUT / "after_drag.png"))

        final_state = await captcha_frame.evaluate("""() => {
            var all = document.querySelectorAll('.dragger-item');
            var result = [];
            for (var i = 0; i < all.length; i++) {
                var r = all[i].getBoundingClientRect();
                var style = all[i].getAttribute('style') || '';
                result.push({x: r.left, y: r.top, w: r.width, h: r.height, style: style});
            }
            return result;
        }""")
        print(f"\nFinal dragger positions:")
        for i, s in enumerate(final_state):
            print(f"  [{i}] ({s['x']:.0f},{s['y']:.0f}) {s['style'][:60]}")

        # Check if any position changed
        moved = False
        for i, (init, final) in enumerate(zip(initial_state, final_state)):
            if abs(init['x'] - final['x']) > 5:
                print(f"\n  Element [{i}] moved! dx = {final['x'] - init['x']:.0f}px")
                moved = True

        if not moved:
            print("\n  ❌ NO ELEMENT MOVED after drag!")
            
            # Try alternative: maybe listen needs to be on captcha container not document
            print("  Trying drag on captcha-slider element directly...")
            slider_track = await captcha_frame.query_selector(".captcha-slider")
            st_box = await slider_track.bounding_box()
            
            # Drag from slider track center
            st_cx = st_box['x'] + st_box['width'] / 2
            st_cy = st_box['y'] + st_box['height'] / 2
            
            await page.mouse.move(st_cx, st_cy)
            await page.wait_for_timeout(200)
            await page.mouse.down()
            await page.wait_for_timeout(50)
            
            for x_offset in [10, 25, 45, 70, 100, 120]:
                await page.mouse.move(st_cx + x_offset, st_cy, steps=1)
                await page.wait_for_timeout(random.randint(10, 20))
            
            await page.mouse.up()
            await page.wait_for_timeout(500)
            
            final_state2 = await captcha_frame.evaluate("""() => {
                var all = document.querySelectorAll('.dragger-item');
                var result = [];
                for (var i = 0; i < all.length; i++) {
                    var r = all[i].getBoundingClientRect();
                    var style = all[i].getAttribute('style') || '';
                    result.push({x: r.left, y: r.top, w: r.width, h: r.height, style: style});
                }
                return result;
            }""")
            print(f"After alternative drag:")
            for i, s in enumerate(final_state2):
                print(f"  [{i}] ({s['x']:.0f},{s['y']:.0f}) {s['style'][:60]}")
        else:
            print(f"\n  ✅ DRAG WORKED! Target distance: {display_offset:.1f}px")

        # Also check captcha status
        verify_frames = [f for f in page.frames if "verifycenter" in f.url]
        print(f"\n  Verify frames remaining: {len(verify_frames)}")
        if not verify_frames:
            print("  ✅ Captcha solved!")
        else:
            tips = await captcha_frame.evaluate("""() => {
                var els = document.querySelectorAll('[class*=\"tip\"], [class*=\"error\"]');
                return Array.from(els).map(function(e) { return e.textContent.trim(); }).filter(function(t) { return t; });
            }""")
            if tips:
                print(f"  Tips: {tips}")

        await page.wait_for_timeout(8000)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
