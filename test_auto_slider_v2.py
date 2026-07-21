"""
E2E test v2: Auto-solve Bytedance captcha with CORRECT element targeting.

Key coordinates from debug:
  - captcha container: parent element
  - bg image: #captcha_verify_image at x=470,y=277, 340x212
  - slider BUTTON: .captcha-slider-btn at x=472,y=495, 64x40
  - slider track: .captcha-slider at x=470,y=493, 340x44
  - puzzle piece: .dragger-item (in image area) at x=470,y=386, 68x68
"""
import asyncio
import base64
import math
import random
from pathlib import Path

from playwright.async_api import async_playwright

OUTPUT = Path("slider_v2_results")
OUTPUT.mkdir(exist_ok=True)


def human_track(distance: float) -> list[tuple[int, int, int]]:
    """Generate human-like drag trajectory. Returns list of (x, dy, delay_ms)."""
    track = []
    dist_remain = distance
    t = 0
    current = 0

    # Phase 1: fast accelerate (0 -> ~70%)
    phase1_end = distance * random.uniform(0.6, 0.75)
    while current < phase1_end:
        step = max(1, int(dist_remain * random.uniform(0.09, 0.18)))
        step = min(step, int(phase1_end - current))
        if step <= 0:
            break
        current += step
        dy = random.randint(-1, 1)
        delay = random.randint(8, 16)
        track.append((int(current), dy, delay))
        t += 1

    # Phase 2: decelerate (70% -> ~92%)
    phase2_end = distance * random.uniform(0.88, 0.95)
    while current < phase2_end:
        step = max(1, int(dist_remain * random.uniform(0.03, 0.09)))
        step = min(step, int(phase2_end - current))
        if step <= 0:
            break
        current += step
        dy = random.randint(-1, 1)
        delay = random.randint(12, 25)
        track.append((int(current), dy, delay))
        t += 1

    # Phase 3: fine approach to target
    overshoot = random.random() < 0.25
    target = distance + random.uniform(2, 8) if overshoot else distance

    while current < target:
        step = max(1, int(dist_remain * random.uniform(0.01, 0.04)))
        step = min(step, int(target - current))
        if step <= 0:
            step = 1
        current += step
        dy = random.choice([0, 0, 0, 0, -1, 1])
        delay = random.randint(18, 40)
        track.append((int(current), dy, delay))
        t += 1

    # If overshot, correct back
    if overshoot:
        for _ in range(random.randint(3, 6)):
            current = max(int(distance), current - random.randint(1, 3))
            dy = random.choice([0, 0, 0, -1])
            delay = random.randint(25, 60)
            track.append((current, dy, delay))

    # Final settle jitter
    for _ in range(random.randint(2, 4)):
        jitter = random.randint(-1, 1)
        delay = random.randint(40, 100)
        track.append((int(distance + jitter), 0, delay))

    return track


async def main():
    print("=" * 60)
    print("E2E AUTO-SLIDER TEST V2")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        # ---- Step 1-3: Navigate, fill, trigger captcha ----
        print("\n[1] Loading trae.cn/login...")
        await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        phone = await page.query_selector('input[placeholder*="手机"]')
        if phone:
            await phone.fill("13800138000")
            print("  Phone filled")

        send_btn = await page.query_selector(".send-code")
        if send_btn:
            await send_btn.click()
            print("  Send code clicked - waiting for captcha...")

        # ---- Step 4: Wait for captcha ----
        captcha_frame = None
        for sec in range(30):
            for f in page.frames:
                if "verifycenter" in f.url:
                    captcha_frame = f
                    print(f"  Captcha frame found at {sec}s")
                    break
            if captcha_frame:
                break
            await page.wait_for_timeout(1000)

        if not captcha_frame:
            print("FAIL: No captcha")
            await browser.close()
            return

        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(OUTPUT / "01_captcha_shown.png"))

        # ---- Step 5: Get background image offset ----
        print("\n[2] Detecting gap position...")

        # Download background image
        bg_img = await captcha_frame.query_selector("#captcha_verify_image")
        if not bg_img:
            bg_img = await captcha_frame.query_selector('img[alt="basicImg"], img.captcha-verify-image')

        bg_src = await bg_img.get_attribute("src") if bg_img else None
        if not bg_src:
            print("FAIL: No bg image src")
            await browser.close()
            return

        bg_bytes = None
        if bg_src.startswith("data:"):
            _, data = bg_src.split(",", 1)
            bg_bytes = base64.b64decode(data)
        else:
            resp = await page.context.request.get(bg_src)
            if resp.ok:
                bg_bytes = await resp.body()

        if not bg_bytes:
            print("FAIL: Could not download bg image")
            await browser.close()
            return

        (OUTPUT / "bg.jpg").write_bytes(bg_bytes)

        # Get display size
        bg_box = await bg_img.bounding_box()
        display_w = bg_box["width"] if bg_box else 340
        print(f"  Display size: {display_w:.0f}px wide")

        # Run captcha-recognizer
        try:
            from captcha_recognizer.slider import Slider
            model = Slider()
        except ImportError:
            from captcha_recognizer import Slider as S2
            model = S2()

        offset, confidence = model.identify_offset(source=str(OUTPUT / "bg.jpg"), show=False)
        print(f"  Model detected: offset={offset:.1f}px, confidence={confidence:.3f}")

        if offset == 0:
            # Try on full page screenshot clip
            await page.screenshot(path=str(OUTPUT / "page.png"))
            offset, confidence = model.identify_offset(source=str(OUTPUT / "page.png"), show=False)
            print(f"  Page screenshot: offset={offset:.1f}px, confidence={confidence:.3f}")

        if offset == 0:
            print("FAIL: captcha-recognizer cannot detect gap")
            await browser.close()
            return

        # ---- Scale offset ----
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
        natural_w = img.shape[1] if img is not None else display_w
        print(f"  Natural size: {natural_w}px wide")

        # Model works at 640x640 with letterbox
        # Natural -> Model: scale = min(640/natural_w, 640/natural_h)
        if img is not None:
            natural_h = img.shape[0]
            model_scale = min(640 / natural_w, 640 / natural_h)
            # Offset is in model space (640x640). Remove letterbox padding.
            padded_w = natural_w * model_scale
            pad_left = (640 - padded_w) / 2
            offset_natural = (offset - pad_left) / model_scale
        else:
            offset_natural = offset * natural_w / 640

        # Natural -> Display
        display_offset = offset_natural * (display_w / natural_w)
        print(f"  => Display offset: {display_offset:.1f}px")

        # ---- Step 6: Find slider button and drag ----
        print("\n[3] Finding slider button and dragging...")

        # Find the actual slider button
        slider_btn = await captcha_frame.query_selector(".captcha-slider-btn")
        if not slider_btn:
            # Fallback: find the dragger-item in the SLIDER (not image area)
            for el in await captcha_frame.query_selector_all(".dragger-item"):
                box = await el.bounding_box()
                if box and box["y"] > 450:  # In slider area (y~493)
                    slider_btn = el
                    break

        if not slider_btn:
            print("FAIL: Cannot find slider button")
            await browser.close()
            return

        btn_box = await slider_btn.bounding_box()
        if not btn_box:
            print("FAIL: Slider button has no bounding box")
            await browser.close()
            return

        # Calculate drag coordinates
        start_x = btn_box["x"] + btn_box["width"] / 2
        start_y = btn_box["y"] + btn_box["height"] / 2
        end_x = start_x + display_offset

        print(f"  Button box: ({btn_box['x']:.0f}, {btn_box['y']:.0f}, {btn_box['width']:.0f}x{btn_box['height']:.0f})")
        print(f"  Drag: ({start_x:.0f}, {start_y:.0f}) -> ({end_x:.0f}, {start_y:.0f})")
        print(f"  Distance: {display_offset:.1f}px")

        # Generate human-like trajectory
        track = human_track(display_offset)
        print(f"  Trajectory: {len(track)} steps")

        # ---- Execute drag ----
        # 1. Move to button
        await page.mouse.move(start_x, start_y)
        await page.wait_for_timeout(random.randint(200, 500))

        # 2. Press
        await page.mouse.down()
        await page.wait_for_timeout(random.randint(30, 80))

        # 3. Execute trajectory
        for dx, dy, delay in track:
            await page.mouse.move(
                start_x + dx + random.uniform(-0.3, 0.3),
                start_y + dy + random.uniform(-0.3, 0.3),
                steps=1,
            )
            await page.wait_for_timeout(delay)

        # 4. Final position
        await page.wait_for_timeout(random.randint(80, 200))
        await page.mouse.move(
            end_x + random.uniform(-0.5, 0.5),
            start_y + random.uniform(-0.5, 0.5),
        )
        await page.wait_for_timeout(random.randint(100, 300))

        # 5. Release
        await page.mouse.up()
        print("  Drag completed!")

        # ---- Step 7: Check result ----
        print("\n[4] Checking result...")
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUTPUT / "02_after_drag.png"))

        # Check if captcha is gone
        still_visible = False
        try:
            still_visible = await captcha_frame.is_visible("#vc_captcha_box", timeout=3000)
        except:
            pass

        # Also check on main page level
        frames_now = len(page.frames)
        verify_frames = [f for f in page.frames if "verifycenter" in f.url]

        if not verify_frames:
            print("\n  *** SUCCESS! Captcha iframe is GONE! ***")
            print("  The captcha was automatically solved!")
        elif not still_visible:
            print("\n  *** SUCCESS! Captcha box not visible! ***")
        else:
            print("\n  Captcha still visible")
            # Check error
            try:
                tips = await captcha_frame.query_selector_all(
                    ".captcha-slider-tips, [class*='tip'], [class*='error'], [class*='message']"
                )
                for t in tips:
                    text = (await t.inner_text()).strip()
                    if text:
                        print(f"  Tip: '{text[:60]}'")
            except:
                pass

            # Check the slider position
            try:
                dragged = await captcha_frame.query_selector(".captcha-slider-dragged-area")
                if dragged:
                    style = await dragged.get_attribute("style") or ""
                    box = await dragged.bounding_box()
                    print(f"  Dragged area: style='{style[:60]}' box={box}")
            except:
                pass

        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(OUTPUT / "03_final.png"))

        print(f"\nResults saved to {OUTPUT.resolve()}")
        await page.wait_for_timeout(10000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
