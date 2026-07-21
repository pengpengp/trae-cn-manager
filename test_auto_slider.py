"""
E2E test: Automatically solve Bytedance slider captcha using captcha-recognizer.

Flow:
1. Open trae.cn/login, fill phone, click send code to trigger captcha
2. When captcha iframe appears, download background image
3. Run captcha-recognizer to detect gap offset
4. Scale offset to match element display size
5. Find slider handle, generate human-like trajectory, drag it
6. Verify if captcha passed
"""
import asyncio
import base64
import math
import random
import time
from pathlib import Path

from playwright.async_api import async_playwright

OUTPUT = Path("slider_test_results")
OUTPUT.mkdir(exist_ok=True)


def human_track(distance: float) -> list[tuple[int, int, int]]:
    """
    Generate a human-like drag trajectory.
    Returns list of (dx, dy, delay_ms) tuples.
    """
    track = []
    current = 0
    mid = distance * 0.6 + random.uniform(-5, 5)
    t = 0

    # Phase 1: fast acceleration (0 -> ~60% of distance)
    while current < mid:
        step = random.randint(
            max(1, int(distance * 0.08)),
            max(2, int(distance * 0.15)),
        )
        current = min(current + step, mid)
        dy = random.randint(-1, 1)  # slight vertical wobble
        delay = random.randint(8, 18)  # ~60-120fps pace
        track.append((current, dy, delay))
        t += 1

    # Phase 2: deceleration (60% -> ~90%)
    while current < distance * 0.9:
        step = random.randint(
            max(1, int(distance * 0.03)),
            max(2, int(distance * 0.08)),
        )
        current = min(current + step, distance * 0.9)
        dy = random.randint(-1, 1)
        delay = random.randint(12, 25)
        track.append((current, dy, delay))
        t += 1

    # Phase 3: fine-tuning (90% -> target, with possible overshoot+correct)
    # Decide randomly whether to overshoot
    overshoot = random.random() < 0.3  # 30% chance of overshoot
    overshoot_amount = random.uniform(5, 12) if overshoot else 0

    if overshoot_amount > 0:
        target = distance + overshoot_amount
    else:
        target = distance

    while current < target:
        step = random.randint(1, max(2, int(distance * 0.02)))
        current = min(current + step, target)
        dy = random.randint(-1, 1)
        delay = random.randint(15, 35)
        track.append((current, dy, delay))
        t += 1

    # If overshot, correct back
    if overshoot_amount > 0:
        correction_steps = random.randint(3, 7)
        for _ in range(correction_steps):
            current = max(distance, current - random.uniform(1, 3))
            dy = random.randint(0, 1)
            delay = random.randint(20, 50)
            track.append((current, dy, delay))

    # Final small jitter at target position (simulate "settling")
    for _ in range(random.randint(2, 5)):
        jitter = random.uniform(-0.5, 0.5)
        track.append((current + jitter, 0, random.randint(30, 80)))

    return track


async def main():
    print("=" * 60)
    print("E2E AUTO-SLIDER TEST")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # ---- Step 1: Navigate ----
        print("\n[1/7] Loading trae.cn/login...")
        await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        # ---- Step 2: Fill phone ----
        print("\n[2/7] Filling phone number...")
        phone_input = await page.query_selector('input[placeholder*="手机"]')
        if not phone_input:
            phone_input = await page.query_selector('input.mobile-phone')
        if not phone_input:
            inputs = await page.query_selector_all("input")
            for inp in inputs:
                if await inp.is_visible():
                    phone_input = inp
                    break
        if phone_input:
            await phone_input.click()
            await page.wait_for_timeout(200)
            await phone_input.fill("13800138000")
            print(f"  Phone filled: {await phone_input.input_value()}")
        else:
            print("  FAIL: No phone input")
            await page.screenshot(path=str(OUTPUT / "fail_no_input.png"))
            await browser.close()
            return

        # ---- Step 3: Click send code to trigger captcha ----
        print("\n[3/7] Clicking 'send code' to trigger captcha...")
        send_el = await page.query_selector(".send-code, [class*='send']")
        if not send_el:
            for tag in ["div", "span", "button"]:
                for el in await page.query_selector_all(tag):
                    if await el.is_visible():
                        text = (await el.inner_text()).strip()
                        if "获取验证码" in text or "发送" in text:
                            send_el = el
                            break
                if send_el:
                    break
        if send_el:
            await send_el.click()
            print("  Send code clicked!")
        else:
            print("  FAIL: No send-code element")
            await browser.close()
            return

        # ---- Step 4: Wait for captcha iframe ----
        print("\n[4/7] Waiting for captcha iframe (max 30s)...")
        captcha_frame = None
        for sec in range(30):
            for frame in page.frames:
                if "verifycenter" in frame.url and "captcha" in frame.url:
                    captcha_frame = frame
                    print(f"  Captcha iframe found at {sec}s: {frame.url[:80]}...")
                    break
            if captcha_frame:
                break
            await page.wait_for_timeout(1000)

        if not captcha_frame:
            print("  FAIL: No captcha appeared")
            await page.screenshot(path=str(OUTPUT / "fail_no_captcha.png"))
            await browser.close()
            return

        await page.wait_for_timeout(2000)  # Let captcha render fully
        await page.screenshot(path=str(OUTPUT / "captcha_appeared.png"))

        # ---- Step 5: Download background image and detect gap ----
        print("\n[5/7] Detecting slider gap position...")

        # Get the background image (the one with the gap)
        bg_img = await captcha_frame.query_selector("#captcha_verify_image")
        if not bg_img:
            bg_img = await captcha_frame.query_selector('img.captcha-verify-image[alt="basicImg"]')
        if not bg_img:
            bg_img = await captcha_frame.query_selector('img[alt="basicImg"]')

        if not bg_img:
            print("  FAIL: Could not find captcha background image")
            await page.screenshot(path=str(OUTPUT / "fail_no_bg.png"))
            await browser.close()
            return

        # Get actual display size
        bg_box = await bg_img.bounding_box()
        if not bg_box:
            print("  FAIL: Background image has no bounding box")
            await browser.close()
            return

        print(f"  Background image display size: {bg_box['width']}x{bg_box['height']}")

        # Download the image
        bg_src = await bg_img.get_attribute("src")
        if not bg_src:
            print("  FAIL: No src on background image")
            await browser.close()
            return

        bg_bytes = None
        if bg_src.startswith("data:"):
            header, data = bg_src.split(",", 1)
            bg_bytes = base64.b64decode(data)
        elif bg_src.startswith("http"):
            resp = await page.context.request.get(bg_src)
            if resp.ok:
                bg_bytes = await resp.body()

        if not bg_bytes:
            print("  FAIL: Could not download background image")
            await browser.close()
            return

        (OUTPUT / "bg_image.jpg").write_bytes(bg_bytes)
        print(f"  Image saved ({len(bg_bytes)} bytes)")

        # Run captcha-recognizer
        try:
            from captcha_recognizer.slider import Slider
            model = Slider()
        except ImportError:
            from captcha_recognizer import Slider as Slider2
            model = Slider2()

        # Try on the raw image first
        offset, confidence = model.identify_offset(source=str(OUTPUT / "bg_image.jpg"), show=False)
        print(f"  Raw image: offset={offset:.1f}px, confidence={confidence:.3f}")

        if offset == 0:
            # Try on the full-page screenshot (captcha container clipped)
            print("  No gap on raw image, trying from screenshot...")
            # Clip the captcha area from full screenshot
            captcha_container = await captcha_frame.query_selector("#vc_captcha_box")
            if captcha_container:
                cbox = await captcha_container.bounding_box()
                if cbox:
                    await page.screenshot(
                        path=str(OUTPUT / "captcha_region.png"),
                        clip={"x": cbox["x"], "y": cbox["y"], "width": cbox["width"], "height": cbox["height"]}
                    )
                    offset, confidence = model.identify_offset(source=str(OUTPUT / "captcha_region.png"), show=False)
                    print(f"  Screenshot clip: offset={offset:.1f}px, confidence={confidence:.3f}")

        if offset == 0:
            print("  FAIL: captcha-recognizer could not detect gap")
            await browser.close()
            return

        # ---- Step 6: Scale offset and drag ----
        print("\n[6/7] Executing slider drag...")

        # Find the slider handle
        slider_handle = await captcha_frame.query_selector(".dragger-item, .captcha_verify_slide--button [slot='dragger'], #captcha-verify_img_slide")
        if not slider_handle:
            slider_handle = await captcha_frame.query_selector(".vc-captcha-verify .captcha-slider")
        if not slider_handle:
            # Find the clickable slider button div
            slider_handle = await captcha_frame.query_selector('[class*="captcha_verify_slide"]')
        if not slider_handle:
            # Last resort: find the slider container and click-drag from its left edge
            slider_handle = await captcha_frame.query_selector(".captcha-slider")
        if not slider_handle:
            print("  FAIL: Could not find slider handle element")
            await captcha_frame.screenshot(path=str(OUTPUT / "fail_no_slider.png"))
            await browser.close()
            return

        handle_box = await slider_handle.bounding_box()
        if not handle_box:
            print("  FAIL: Slider handle has no bounding box")
            await browser.close()
            return

        print(f"  Slider handle: x={handle_box['x']:.0f}, y={handle_box['y']:.0f}, w={handle_box['width']:.0f}, h={handle_box['height']:.0f}")

        # Scale the offset
        # captcha-recognizer runs at 640x640 input. The display image might be a different size.
        # The original image natural size might not be 640. We need to scale:
        #   natural_size of bg image vs display size vs model input size (640)
        # Let's figure out the natural size from the image

        import cv2
        import numpy as np

        img_cv = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img_cv is not None:
            natural_h, natural_w = img_cv.shape[:2]
            print(f"  Image natural size: {natural_w}x{natural_h}")

            # The model resizes to 640x640 with letterbox padding
            # offset from model is relative to the model's coordinate space
            # We need to: model_offset -> relative position -> scale to display

            # Model calculates offset by padding to 640x640 (letterbox)
            # The offset is in model coordinates (0-640)
            # Need to convert to display coordinates

            # If image is w x h, letterboxed to 640x640:
            # scale = min(640/w, 640/h)
            scale = min(640 / natural_w, 640 / natural_h)
            padded_w = int(natural_w * scale)
            # padding added to left = (640 - padded_w) / 2
            pad_left = (640 - padded_w) / 2

            # Model offset is already in the letterboxed space
            # Convert back: remove padding, then scale to display
            offset_no_pad = offset - pad_left
            # Scale from natural size to display size
            display_scale = bg_box["width"] / natural_w
            scaled_offset = offset_no_pad / scale * display_scale
            print(f"  Model offset={offset:.1f} -> no_pad={offset_no_pad:.1f} -> scaled={scaled_offset:.1f} (display px)")
        else:
            # Fallback: assume natural=display and model used 640-based
            display_scale = bg_box["width"] / 640
            scaled_offset = offset * display_scale
            print(f"  Model offset={offset:.1f} -> scaled={scaled_offset:.1f} (display px, naive scaling)")

        # Generate human trajectory
        track = human_track(scaled_offset)
        print(f"  Human track generated: {len(track)} steps, total distance ~{scaled_offset:.0f}px")

        # Get the slider button center
        slider_btn = await captcha_frame.query_selector(
            ".captcha_verify_slide--button, .vc-captcha-btn, "
            ".vc-captcha-verify .captcha-slider div[role='button'], "
            ".captcha-slider > div"
        )
        if not slider_btn:
            slider_btn = slider_handle

        btn_box = await slider_btn.bounding_box()
        if not btn_box:
            print("  Using handle box for drag start")
            btn_box = handle_box

        start_x = btn_box["x"] + btn_box["width"] / 2
        start_y = btn_box["y"] + btn_box["height"] / 2
        print(f"  Drag start: ({start_x:.0f}, {start_y:.0f})")

        # Execute the drag using mouse movements
        await page.mouse.move(start_x, start_y)
        await page.wait_for_timeout(random.randint(100, 300))
        await page.mouse.down()
        await page.wait_for_timeout(random.randint(30, 80))

        prev_x = start_x
        for dx, dy, delay_ms in track:
            target_x = start_x + dx
            target_y = start_y + dy
            # Add micro-jitter to points
            jitter_x = random.uniform(-0.3, 0.3)
            jitter_y = random.uniform(-0.3, 0.3)
            await page.mouse.move(
                target_x + jitter_x,
                target_y + jitter_y,
                steps=1,
            )
            await page.wait_for_timeout(delay_ms)

        # Final micro-adjustments
        await page.wait_for_timeout(random.randint(50, 150))
        final_x = start_x + scaled_offset
        await page.mouse.move(final_x, start_y + random.uniform(-0.5, 0.5))
        await page.wait_for_timeout(random.randint(100, 300))

        # Release
        await page.mouse.up()

        print("  Drag completed")

        # ---- Step 7: Check result ----
        print("\n[7/7] Checking result...")
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUTPUT / "after_drag.png"))

        # Check if captcha is still visible
        still_visible = await captcha_frame.is_visible("#vc_captcha_box")
        if not still_visible:
            print("\n  *** CAPTCHA SOLVED SUCCESSFULLY! ***")
            print("  The captcha overlay has disappeared!")
        else:
            print("\n  Captcha still visible - need to check more...")
            # Check for error message
            error_el = await captcha_frame.query_selector('[class*="error"], [class*="fail"], [class*="tip"]')
            if error_el:
                error_text = await error_el.inner_text()
                print(f"  Error message: '{error_text.strip()}'")

            # Check if slider has "retry" button
            retry = await captcha_frame.query_selector('[class*="refresh"], [class*="retry"]')
            if retry:
                print("  RETRY button found - captcha was NOT solved")

            # Print all visible text in the captcha frame
            print("  Visible text elements in captcha frame:")
            for el in await captcha_frame.query_selector_all("[class*='tip'], [class*='message'], [class*='title'], [class*='text']"):
                if await el.is_visible():
                    t = (await el.inner_text()).strip()
                    if t:
                        print(f"    '{t}'")

        # Take one more screenshot after short wait
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(OUTPUT / "result_final.png"))

        print(f"\n{'='*60}")
        print(f"Test complete. Results saved to {OUTPUT.resolve()}")
        print(f"{'='*60}")
        input("\nPress Enter to close browser...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
