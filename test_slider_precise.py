"""
Precision test: verify exact slider position before/after drag.
Captures and measures:
  1. Gap position detected by captcha-recognizer (model coords)
  2. Natural image dimensions → calculated display offset
  3. Puzzle piece actual position (via getBoundingClientRect) before drag
  4. Puzzle piece actual position after drag
  5. Error between target and actual
"""
import asyncio
import base64
import random
import json
from pathlib import Path

import cv2
import numpy as np
from playwright.async_api import async_playwright

OUTPUT = Path("slider_v3_results")
OUTPUT.mkdir(exist_ok=True)

# -------------------- trajectory --------------------
def human_track(distance: float) -> list[dict]:
    """Generate human-like drag with bell-shaped velocity profile."""
    if distance <= 0:
        return [{"x": 0, "y": 0, "t": 30}]

    steps = []
    # Phase 1: accelerate (0->60% distance in bigger jumps)
    phase1_end = distance * random.uniform(0.5, 0.65)
    x = 0
    max_iter = 200  # safety
    iter_count = 0
    while x < phase1_end and iter_count < max_iter:
        remaining = phase1_end - x
        step = max(1, int(remaining * random.uniform(0.08, 0.20)))
        step = min(step, int(remaining))
        x += step
        dy = random.choice([-1, 0, 0, 1])
        delay = random.randint(8, 16)
        steps.append({"x": int(x), "y": dy, "t": delay})
        iter_count += 1

    # Phase 2: decelerate (60%->95% distance in smaller jumps)
    iter_count = 0
    while x < distance * 0.95 and iter_count < max_iter:
        remaining = distance * 0.95 - x
        step = max(1, int(remaining * random.uniform(0.03, 0.10)))
        step = min(step, int(remaining))
        x += step
        dy = random.choice([-1, 0, 0, 0, 1])
        delay = random.randint(12, 28)
        steps.append({"x": int(x), "y": dy, "t": delay})
        iter_count += 1

    # Phase 3: fine approach to target
    iter_count = 0
    while x < distance and iter_count < max_iter:
        remaining = distance - x
        step = max(1, int(remaining * random.uniform(0.01, 0.05)))
        step = min(step, int(remaining))
        x += step
        dy = random.choice([0, 0, -1])
        delay = random.randint(20, 45)
        steps.append({"x": int(x), "y": dy, "t": delay})
        iter_count += 1

    # Overshoot (20% chance)
    if random.random() < 0.2:
        overshoot = random.uniform(3, 10)
        x_final = int(distance + overshoot)
        for _ in range(random.randint(2, 5)):
            x_final -= random.randint(1, 3)
            steps.append({"x": max(int(distance), x_final), "y": 0, "t": random.randint(30, 70)})

    # Settle jitter
    for _ in range(random.randint(2, 4)):
        jitter = random.randint(-1, 1)
        delay = random.randint(50, 120)
        steps.append({"x": int(distance + jitter), "y": 0, "t": delay})

    return steps


async def main():
    print("=" * 60)
    print("PRECISION SLIDER TEST")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        # ---- Setup ----
        await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

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
            print("FAIL: no captcha")
            await browser.close()
            return

        await page.wait_for_timeout(2000)
        print("\nCaptcha loaded.")

        # ---- Measure: initial state ----
        print("\n=== MEASURING INITIAL STATE ===")

        # Puzzle piece position (in image area)
        puzzle_div = await captcha_frame.query_selector(".verify-image .dragger-item, .verify-image [class*='dragger-item']")
        if not puzzle_div:
            puzzle_div = await captcha_frame.query_selector('[class*="dragger-item"]')

        puzzle_els = await captcha_frame.query_selector_all(".dragger-item")
        # Filter: the one in the IMAGE area (y ~ 386, NOT the one in slider at y~493)
        image_puzzle = None
        slider_puzzle = None
        for el in puzzle_els:
            box = await el.bounding_box()
            if box:
                if box["y"] < 450:  # Image area
                    image_puzzle = el
                else:  # Slider area
                    slider_puzzle = el

        print(f"  Image puzzle element: {'found' if image_puzzle else 'NOT FOUND'}")
        print(f"  Slider dragger element: {'found' if slider_puzzle else 'NOT FOUND'}")

        if image_puzzle:
            ip_box = await image_puzzle.bounding_box()
            ip_transform = await image_puzzle.get_attribute("style") or ""
            print(f"  Image puzzle box: ({ip_box['x']:.0f}, {ip_box['y']:.0f}, {ip_box['width']:.0f}x{ip_box['height']:.0f})")
            print(f"  Image puzzle style: {ip_transform[:80]}")

        if slider_puzzle:
            sp_box = await slider_puzzle.bounding_box()
            sp_transform = await slider_puzzle.get_attribute("style") or ""
            print(f"  Slider dragger box: ({sp_box['x']:.0f}, {sp_box['y']:.0f}, {sp_box['width']:.0f}x{sp_box['height']:.0f})")
            print(f"  Slider dragger style: {sp_transform[:80]}")

        # Slider button
        slider_btn = await captcha_frame.query_selector(".captcha-slider-btn")
        if slider_btn:
            sb_box = await slider_btn.bounding_box()
            print(f"  Slider button box: ({sb_box['x']:.0f}, {sb_box['y']:.0f}, {sb_box['width']:.0f}x{sb_box['height']:.0f})")
        else:
            print("  Slider button: NOT FOUND")

        # Dragged area (the filled track portion)
        dragged_area = await captcha_frame.query_selector(".captcha-slider-dragged-area")
        if dragged_area:
            da_box = await dragged_area.bounding_box()
            da_style = await dragged_area.get_attribute("style") or ""
            print(f"  Dragged area: ({da_box['x']:.0f}, {da_box['y']:.0f}, w={da_box['width']:.0f}) style='{da_style[:60]}'")

        # ---- Detect gap ----
        print("\n=== GAP DETECTION ===")
        bg_img = await captcha_frame.query_selector("#captcha_verify_image")
        bg_src = await bg_img.get_attribute("src") if bg_img else None

        bg_bytes = None
        if bg_src and bg_src.startswith("http"):
            resp = await page.context.request.get(bg_src)
            if resp.ok:
                bg_bytes = await resp.body()
        elif bg_src and bg_src.startswith("data:"):
            _, data = bg_src.split(",", 1)
            bg_bytes = base64.b64decode(data)

        if not bg_bytes:
            print("FAIL: cannot download bg image")
            await browser.close()
            return

        (OUTPUT / "bg.jpg").write_bytes(bg_bytes)

        # Run captcha-recognizer
        from captcha_recognizer.slider import Slider
        model = Slider()
        model_offset, model_conf = model.identify_offset(source=str(OUTPUT / "bg.jpg"), show=False)
        print(f"  Model: offset={model_offset:.1f}px, confidence={model_conf:.3f}")

        # Get natural image dimensions
        img_cv = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
        natural_h, natural_w = img_cv.shape[:2]
        print(f"  Natural image: {natural_w}x{natural_h}")

        # Get display size
        bg_box = await bg_img.bounding_box()
        display_w = bg_box["width"] if bg_box else 340
        print(f"  Display: {display_w:.0f}px wide")

        # Calculate display offset
        model_scale = min(640 / natural_w, 640 / natural_h)
        padded_w = natural_w * model_scale
        pad_left = (640 - padded_w) / 2
        offset_natural = (model_offset - pad_left) / model_scale
        display_offset = offset_natural * (display_w / natural_w)
        print(f"  Scale: model={model_scale:.4f}, pad_left={pad_left:.2f}")
        print(f"  Natural offset: {offset_natural:.1f}px")
        print(f"  => DISPLAY OFFSET: {display_offset:.1f}px")

        # Expected: where puzzle piece left edge should be
        # Image starts at bg_box.x (page coords)
        expected_puzzle_left = bg_box["x"] + display_offset
        expected_puzzle_center = expected_puzzle_left + (ip_box["width"] / 2 if ip_box else 34)
        print(f"  Expected puzzle left: {expected_puzzle_left:.1f}px (page coords)")

        # ---- Execute drag ----
        print("\n=== EXECUTING DRAG ===")

        btn_center_x = sb_box["x"] + sb_box["width"] / 2
        btn_center_y = sb_box["y"] + sb_box["height"] / 2

        print(f"  Drag from: ({btn_center_x:.0f}, {btn_center_y:.0f})")
        print(f"  Drag to:   ({btn_center_x + display_offset:.0f}, {btn_center_y:.0f})")
        print(f"  Distance:  {display_offset:.1f}px")

        # Generate trajectory
        track = human_track(display_offset)

        # Execute
        await page.mouse.move(btn_center_x, btn_center_y)
        await page.wait_for_timeout(random.randint(200, 400))
        await page.mouse.down()
        await page.wait_for_timeout(random.randint(40, 80))

        for step in track:
            await page.mouse.move(
                btn_center_x + step["x"] + random.uniform(-0.3, 0.3),
                btn_center_y + step["y"] + random.uniform(-0.3, 0.3),
                steps=1,
            )
            await page.wait_for_timeout(step["t"])

        # Final adjust
        await page.wait_for_timeout(random.randint(80, 200))
        await page.mouse.move(
            btn_center_x + display_offset + random.uniform(-0.5, 0.5),
            btn_center_y + random.uniform(-0.5, 0.5),
        )
        await page.wait_for_timeout(random.randint(100, 300))
        await page.mouse.up()
        print("  Drag DONE")

        # ---- Measure after drag ----
        print("\n=== MEASURING AFTER DRAG ===")
        await page.wait_for_timeout(1500)

        # Snapshot
        await page.screenshot(path=str(OUTPUT / "after_drag.png"))

        # Read puzzle piece positions
        if image_puzzle:
            ip_box_after = await image_puzzle.bounding_box()
            ip_transform_after = await image_puzzle.get_attribute("style") or ""
            print(f"  Image puzzle: ({ip_box_after['x']:.0f}, {ip_box_after['y']:.0f}) w={ip_box_after['width']:.0f}")
            print(f"    style: {ip_transform_after[:80]}")
            delta = ip_box_after["x"] - expected_puzzle_left
            print(f"    vs expected: delta = {delta:.1f}px {'✅' if abs(delta) < 5 else '❌'}")

        if slider_puzzle:
            sp_box_after = await slider_puzzle.bounding_box()
            sp_transform_after = await slider_puzzle.get_attribute("style") or ""
            print(f"  Slider dragger: ({sp_box_after['x']:.0f}, {sp_box_after['y']:.0f}) w={sp_box_after['width']:.0f}")
            print(f"    style: {sp_transform_after[:80]}")

        if dragged_area:
            da_box_after = await dragged_area.bounding_box()
            da_style_after = await dragged_area.get_attribute("style") or ""
            print(f"  Dragged area: ({da_box_after['x']:.0f}, {da_box_after['y']:.0f}) w={da_box_after['width']:.0f}")
            print(f"    style: {da_style_after[:80]}")

        # Check captcha status
        frames_now = [f for f in page.frames if "verifycenter" in f.url]
        if not frames_now:
            print("\n  *** CAPTCHA IFRAME GONE - SOLVED ***")
        else:
            print(f"\n  Captcha still present ({len(frames_now)} verify frames)")
            try:
                # Check for error text
                tips = await captcha_frame.query_selector_all(
                    ".captcha-slider-tips, [class*='error'], [class*='message'], [class*='tip']"
                )
                for t in tips:
                    text = (await t.inner_text()).strip()
                    if text:
                        print(f"  Tip: '{text[:80]}'")

                # Check for retry button
                retry = await captcha_frame.query_selector('[class*="refresh"], [class*="retry"]')
                if retry:
                    print("  RETRY button visible - captcha REJECTED")
            except:
                pass

        print(f"\nLogs saved to {OUTPUT.resolve()}")
        # Keep browser open briefly for visual inspection
        await page.wait_for_timeout(5000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
