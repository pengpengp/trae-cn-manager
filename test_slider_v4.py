"""
Slider test v4: Use direct PointerEvent dispatch on the captcha slider elements.
This ensures the captcha's JS event listeners are properly triggered.
"""
import asyncio
import base64
import random
import json
from pathlib import Path

import cv2
import numpy as np
from playwright.async_api import async_playwright

OUTPUT = Path("slider_v4_results")
OUTPUT.mkdir(exist_ok=True)


def human_track(distance):
    if distance <= 0:
        return []
    steps = []
    x = 0
    # Phase 1: accelerate (0->60%)
    p1 = distance * random.uniform(0.5, 0.65)
    while x < p1:
        step = max(1, int((p1 - x) * random.uniform(0.08, 0.20)))
        x += step
        delay = random.randint(10, 20)
        steps.append((int(x), delay))
    # Phase 2: decelerate (60%->95%)
    while x < distance * 0.95:
        step = max(1, int((distance * 0.95 - x) * random.uniform(0.03, 0.10)))
        x += step
        delay = random.randint(15, 30)
        steps.append((int(x), delay))
    # Phase 3: fine approach
    while x < distance:
        step = max(1, int((distance - x) * random.uniform(0.01, 0.04)))
        x += step
        delay = random.randint(20, 45)
        steps.append((int(x), delay))
    # Overshoot & correct (20% chance)
    if random.random() < 0.2:
        overshoot = random.uniform(3, 10)
        for _ in range(random.randint(3, 6)):
            x = max(int(distance), int(x) - random.randint(1, 3))
            steps.append((x, random.randint(30, 60)))
    # Settle
    for _ in range(random.randint(2, 3)):
        steps.append((int(distance + random.uniform(-1, 1)), random.randint(40, 100)))
    return steps


async def dispatch_pointer_event(frame, selector, event_type, client_x, client_y, **extra):
    """Dispatch a PointerEvent on the element matching selector, using client coordinates."""
    await frame.evaluate(f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) {{ console.warn('el not found: {selector}'); return false; }}
            const rect = el.getBoundingClientRect();
            const evt = new PointerEvent({json.dumps(event_type)}, {{
                bubbles: true,
                cancelable: true,
                composed: true,
                clientX: {client_x},
                clientY: {client_y},
                pointerType: 'mouse',
                isPrimary: true,
                pointerId: 1,
                width: 1,
                height: 1,
                pressure: {0.5 if event_type == 'pointermove' else 0.5 if 'down' in event_type else 0},
                ...{json.dumps(extra)}
            }});
            return el.dispatchEvent(evt);
        }})()
    """)


async def main():
    print("=" * 60)
    print("SLIDER TEST V4 - PointerEvent Dispatch")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        # ---- Navigate and trigger captcha ----
        await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

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
            print("FAIL: no captcha frame")
            await browser.close()
            return

        await page.wait_for_timeout(2000)
        print("\nCaptcha loaded.")

        # ---- Detect gap ----
        print("\n=== GAP DETECTION ===")
        bg_img = await captcha_frame.query_selector("#captcha_verify_image")
        bg_src = await bg_img.get_attribute("src")
        bg_bytes = None
        if bg_src.startswith("http"):
            resp = await page.context.request.get(bg_src)
            if resp.ok:
                bg_bytes = await resp.body()
        elif bg_src.startswith("data:"):
            _, data = bg_src.split(",", 1)
            bg_bytes = base64.b64decode(data)
        if not bg_bytes:
            print("FAIL: no bg image")
            await browser.close()
            return

        (OUTPUT / "bg.jpg").write_bytes(bg_bytes)
        img_cv = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
        natural_w, natural_h = img_cv.shape[1], img_cv.shape[0]

        from captcha_recognizer.slider import Slider
        model = Slider()
        model_offset, model_conf = model.identify_offset(source=str(OUTPUT / "bg.jpg"), show=False)
        print(f"  Model: offset={model_offset:.1f}px, conf={model_conf:.3f}")

        bg_box = await bg_img.bounding_box()
        display_w = bg_box["width"]
        model_scale = min(640 / natural_w, 640 / natural_h)
        offset_natural = model_offset / model_scale  # pad_left ~ 0 (width fills 640)
        display_offset = offset_natural * (display_w / natural_w)
        print(f"  Natural: {natural_w}x{natural_h}, Display: {display_w:.0f}px")
        print(f"  => Display offset: {display_offset:.1f}px")

        # ---- Initial positions (captcha frame coords) ----
        print("\n=== BEFORE DRAG ===")
        puzzle_rect = await captcha_frame.evaluate("""() => {
            const el = document.querySelector('[class*="dragger-item"]');
            if (!el) return null;
            // The image-area dragger (first one with y < 450)
            const all = document.querySelectorAll('.dragger-item');
            for (const e of all) {
                const r = e.getBoundingClientRect();
                if (r.top < 450) return {x: r.left, y: r.top, w: r.width, h: r.height, style: e.getAttribute('style')};
            }
            return null;
        }""")
        print(f"  Image puzzle: {puzzle_rect}")

        slider_btn_rect = await captcha_frame.evaluate("""() => {
            const el = document.querySelector('.captcha-slider-btn');
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: r.left, y: r.top, w: r.width, h: r.height};
        }""")
        print(f"  Slider btn: {slider_btn_rect}")

        if not slider_btn_rect:
            print("FAIL: no slider button")
            await browser.close()
            return

        # ---- Execute drag via PointerEvents inside the iframe ----
        print("\n=== DRAG (PointerEvent dispatch) ===")

        start_x = slider_btn_rect["x"] + slider_btn_rect["w"] / 2
        start_y = slider_btn_rect["y"] + slider_btn_rect["h"] / 2
        end_x = start_x + display_offset
        print(f"  Start: ({start_x:.0f}, {start_y:.0f}) -> ({end_x:.0f}, {start_y:.0f})")

        # Wait for captcha to be ready
        await page.wait_for_timeout(500)

        # 1. pointerdown on slider button
        print("  pointerdown...")
        result = await dispatch_pointer_event(
            captcha_frame, ".captcha-slider-btn", "pointerdown",
            start_x, start_y,
            button=0, buttons=1,
        )
        print(f"    dispatch result: {result}")

        await page.wait_for_timeout(50)

        # 2. pointermove trajectory
        track = human_track(display_offset)
        print(f"  pointermove ({len(track)} steps)...")
        for dx, delay in track:
            await dispatch_pointer_event(
                captcha_frame, ".captcha-slider-btn", "pointermove",
                start_x + dx, start_y,
                button=0, buttons=1,
                movementX=dx, movementY=0,
            )
            await page.wait_for_timeout(delay)

        # Final position
        await page.wait_for_timeout(100)
        await dispatch_pointer_event(
            captcha_frame, ".captcha-slider-btn", "pointermove",
            end_x, start_y,
            button=0, buttons=1,
            movementX=int(end_x - start_x), movementY=0,
        )

        # 3. pointerup
        await page.wait_for_timeout(80)
        print("  pointerup...")
        await dispatch_pointer_event(
            captcha_frame, ".captcha-slider-btn", "pointerup",
            end_x, start_y,
            button=0, buttons=0,
        )
        print("  Drag DONE")

        # ---- Check result ----
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(OUTPUT / "after_drag.png"))

        # Check puzzle position
        puzzle_after = await captcha_frame.evaluate("""() => {
            const all = document.querySelectorAll('.dragger-item');
            for (const e of all) {
                const r = e.getBoundingClientRect();
                if (r.top < 450) return {x: r.left, y: r.top, w: r.width, h: r.height, style: e.getAttribute('style')};
            }
            return null;
        }""")
        print(f"\n  Image puzzle AFTER: {puzzle_after}")

        # Also check slider position
        slider_after = await captcha_frame.evaluate("""() => {
            const s = document.querySelector('.captcha-slider-btn');
            if (!s) return null;
            const r = s.getBoundingClientRect();
            return {x: r.left, y: r.top, w: r.width, h: r.height};
        }""")
        print(f"  Slider btn AFTER: {slider_after}")

        # Check filled area
        area_after = await captcha_frame.evaluate("""() => {
            const a = document.querySelector('.captcha-slider-dragged-area');
            if (!a) return null;
            const r = a.getBoundingClientRect();
            return {x: r.left, y: r.top, w: r.width, h: r.height, style: a.getAttribute('style')};
        }""")
        print(f"  Dragged area AFTER: {area_after}")

        # Check captcha status
        tips = await captcha_frame.evaluate("""() => {
            const els = document.querySelectorAll('[class*="tip"], [class*="error"], [class*="message"]');
            return Array.from(els).map(e => e.textContent.trim()).filter(t => t);
        }""")
        if tips:
            print(f"  Tips: {tips}")

        retry = await captcha_frame.query_selector('[class*="refresh"]')
        if retry:
            print("  RETRY visible - captcha REJECTED")

        # Check if captcha frame is gone from parent
        verify_frames = [f for f in page.frames if "verifycenter" in f.url]
        print(f"\n  Verify frames remaining: {len(verify_frames)}")

        if not verify_frames or not await captcha_frame.is_visible("#vc_captcha_box", timeout=2000):
            print("  *** CAPTCHA SOLVED ***")
        else:
            print("  Captcha still present")

        print(f"\nResults: {OUTPUT.resolve()}")
        await page.wait_for_timeout(5000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
