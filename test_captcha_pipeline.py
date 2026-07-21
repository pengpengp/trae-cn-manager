"""
Test captcha-recognizer on real Bytedance captcha from trae.cn.

Pipeline:
1. Launch Playwright (headed) — navigate to trae.cn login
2. Fill phone, click send code → trigger captcha
3. When captcha appears, screenshot + download captcha images
4. Run captcha-recognizer on the images
5. Report if gap was detected
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

OUTPUT = Path("captcha_samples")
OUTPUT.mkdir(exist_ok=True)

async def main():
    print("=" * 60)
    print("TEST: captcha-recognizer on Bytedance/Volcengine captcha")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # needed for captcha to load
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        # ---- Step 1: Navigate to trae.cn login ----
        print("\n[1/6] Navigating to https://www.trae.cn/login ...")
        await page.goto("https://www.trae.cn/login", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        print(f"  URL: {page.url}")
        print(f"  Title: {await page.title()}")

        # ---- Step 2: Find and fill phone input ----
        print("\n[2/6] Looking for phone input...")
        phone_input = None
        inputs = await page.query_selector_all("input")
        for inp in inputs:
            ph = (await inp.get_attribute("placeholder") or "").lower()
            tp = (await inp.get_attribute("type") or "").lower()
            if "手机" in ph or "phone" in ph or "mobile" in ph or tp == "tel":
                phone_input = inp
                print(f"  Found: placeholder='{ph}' type='{tp}'")
                break

        if not phone_input:
            # Try first text input
            for inp in inputs:
                tp = await inp.get_attribute("type")
                if tp in ("text", "tel", None, ""):
                    phone_input = inp
                    print(f"  Fallback to first input: placeholder='{await inp.get_attribute('placeholder')}'")
                    break

        if phone_input:
            await phone_input.click()
            await page.wait_for_timeout(300)
            await phone_input.fill("13800138000")
            print(f"  Filled: '{await phone_input.input_value()}'")
        else:
            print("  ERROR: No phone input found!")
            await page.screenshot(path=str(OUTPUT / "error_no_input.png"))
            await browser.close()
            return

        # ---- Step 3: Click send code button ----
        print("\n[3/6] Clicking 'send verification code' button...")
        buttons = await page.query_selector_all("button")
        send_btn = None
        for btn in buttons:
            text = (await btn.inner_text()).strip()
            if "验证码" in text or "获取" in text or "发送" in text:
                send_btn = btn
                print(f"  Found: '{text[:50]}'")
                break

        if send_btn:
            await send_btn.click()
            print("  Clicked!")
        else:
            print("  No send-code button found, trying HTML search...")
            await page.screenshot(path=str(OUTPUT / "no_button.png"))

        # ---- Step 4: Wait and detect captcha ----
        print("\n[4/6] Monitoring for captcha (up to 30s)...")
        captcha_detected = False
        captcha_frame = None

        for sec in range(1, 31):
            # Check all frames for captcha
            for frame in page.frames:
                url = frame.url
                if any(kw in url.lower() for kw in ["verify", "captcha", "sec_sdk", "vc_captcha"]):
                    if not captcha_detected:
                        print(f"  [{sec}s] CAPTCHA FRAME: {url[:120]}")
                    captcha_detected = True
                    captcha_frame = frame
                    break
            if captcha_detected:
                break
            await page.wait_for_timeout(1000)

        if not captcha_detected:
            print("\n  No captcha detected after 30s.")
            await page.screenshot(path=str(OUTPUT / "no_captcha.png"))
            # Save HTML for debugging
            html = await page.content()
            (OUTPUT / "page_no_captcha.html").write_text(html, encoding="utf-8")
            print("  Saved page HTML and screenshot")
        else:
            print(f"\n  Captcha detected at ~{sec}s!")
            await page.wait_for_timeout(3000)  # Let it fully render

            # Take full page screenshot
            await page.screenshot(path=str(OUTPUT / "captcha_full_page.png"))

            # ---- Step 5: Extract captcha images ----
            print("\n[5/6] Extracting captcha images from frame...")
            try:
                # Get all images in the captcha frame
                imgs = await captcha_frame.query_selector_all("img")
                print(f"  Found {len(imgs)} images in captcha frame")

                captcha_bg = None
                captcha_piece = None

                for idx, img in enumerate(imgs):
                    src = (await img.get_attribute("src")) or ""
                    cls = (await img.get_attribute("class")) or ""
                    alt = (await img.get_attribute("alt")) or ""

                    # Try to determine if this is bg or piece
                    is_bg = any(kw in cls.lower() or kw in alt.lower()
                                for kw in ["bg", "background", "back", "缺口", "背景"])
                    is_piece = any(kw in cls.lower() or kw in alt.lower()
                                   for kw in ["piece", "slider", "滑块", "front", "front"])

                    if src:
                        if is_bg or (not is_piece and captcha_bg is None):
                            captcha_bg = (src, f"bg_{idx}")
                        if is_piece or (is_piece and captcha_piece is None):
                            captcha_piece = (src, f"piece_{idx}")

                        # Always save
                        print(f"  img[{idx}]: class='{cls}' alt='{alt}' src={src[:80]}...")
                        await save_image(page, src, OUTPUT / f"captcha_img_{idx}.png")

                # Try to find the canvas element (some captchas use canvas)
                canvases = await captcha_frame.query_selector_all("canvas")
                for idx, canvas in enumerate(canvases):
                    print(f"  canvas[{idx}]: size={await canvas.get_attribute('width')}x{await canvas.get_attribute('height')}")
                    # Screenshot just the canvas area
                    box = await canvas.bounding_box()
                    if box:
                        await page.screenshot(
                            path=str(OUTPUT / f"canvas_{idx}.png"),
                            clip={"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}
                        )

                # Also screenshot just the captcha overlay area
                # Find the captcha container
                captcha_container = await captcha_frame.query_selector('[class*="verify"], [class*="captcha"], [class*="vc"], .captcha-panel')
                if captcha_container:
                    box = await captcha_container.bounding_box()
                    if box:
                        await page.screenshot(
                            path=str(OUTPUT / "captcha_container.png"),
                            clip={"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}
                        )

                # Dump captcha frame HTML
                frame_html = await captcha_frame.content()
                (OUTPUT / "captcha_frame.html").write_text(frame_html, encoding="utf-8")
                print("  Saved captcha frame HTML")

            except Exception as e:
                print(f"  Error extracting captcha: {e}")
                import traceback
                traceback.print_exc()

        # ---- Step 6: Test captcha-recognizer ----
        print("\n[6/6] Running captcha-recognizer on captured images...")
        await test_recognizer()

        print("\n" + "=" * 60)
        print("TEST COMPLETE")
        print(f"Samples saved to: {OUTPUT.resolve()}")
        print("=" * 60)

        input("\nPress Enter to close browser...")
        await browser.close()


async def save_image(page, src, path):
    """Download an image from src (data:uri or http url) and save to path."""
    try:
        if src.startswith("data:"):
            import base64
            header, data = src.split(",", 1)
            path.write_bytes(base64.b64decode(data))
            print(f"    -> saved {path.name}")
        else:
            resp = await page.context.request.get(src)
            if resp.ok:
                path.write_bytes(await resp.body())
                print(f"    -> saved {path.name}")
            else:
                print(f"    -> HTTP {resp.status} for {src[:60]}")
    except Exception as e:
        print(f"    -> error: {e}")


async def test_recognizer():
    """Run captcha-recognizer on saved images."""
    try:
        from captcha_recognizer import Slider
        model = Slider()

        png_files = sorted(OUTPUT.glob("*.png"))
        if not png_files:
            print("  No PNG files found to test")
            return

        for img_path in png_files:
            print(f"\n  Testing: {img_path.name}")
            try:
                offset, confidence = model.identify_offset(source=str(img_path), show=False)
                if offset > 0:
                    print(f"    ✅ GAP DETECTED! offset={offset:.1f}px  confidence={confidence:.3f}")
                else:
                    print(f"    ❌ No gap detected (offset=0, conf={confidence:.3f})")
            except Exception as e:
                print(f"    ❌ Error: {e}")

    except ImportError as e:
        print(f"  captcha-recognizer not importable: {e}")
    except Exception as e:
        print(f"  Error in recognizer test: {e}")


if __name__ == "__main__":
    asyncio.run(main())
