"""
Test captcha-recognizer on Bytedance captcha - version 2.
Better selectors, more robust.
"""
import asyncio
import base64
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

OUTPUT = Path("captcha_samples")
OUTPUT.mkdir(exist_ok=True)

async def main():
    print("=" * 60)
    print("TEST V2: captcha-recognizer on Bytedance captcha")
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

        # Intercept captcha image requests
        async def intercept_response(response):
            url = response.url
            ct = response.headers.get("content-type", "")
            if "verify" in url.lower() and ("image" in ct or "png" in url or "jpg" in url):
                try:
                    body = await response.body()
                    fname = f"net_{url.split('/')[-1].split('?')[0]}"
                    (OUTPUT / fname).write_bytes(body)
                    print(f"  [captured] {fname}")
                except:
                    pass

        page.on("response", intercept_response)

        # ---- Step 1: Load page ----
        print("\n[1] Loading https://www.trae.cn/login ...")
        await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)
        print(f"  URL: {page.url}")

        # ---- Step 2: Find phone login elements ----
        print("\n[2] Looking for phone input...")

        # Try various selectors
        phone_input = None
        selectors = [
            'input[placeholder*="手机"]',
            'input[type="tel"]',
            'input[type="text"]',
            'input:not([type="checkbox"]):not([type="password"])',
        ]
        for sel in selectors:
            els = await page.query_selector_all(sel)
            for el in els:
                ph = (await el.get_attribute("placeholder")) or ""
                if "手机" in ph or "phone" in ph or "mobile" in ph:
                    phone_input = el
                    print(f"  Found via '{sel}': placeholder='{ph}'")
                    break
            if phone_input:
                break

        if not phone_input:
            # Just use first visible text input
            for el in await page.query_selector_all("input"):
                if await el.is_visible():
                    phone_input = el
                    print(f"  Fallback to first visible input: placeholder='{await el.get_attribute('placeholder')}'")
                    break

        if not phone_input:
            print("  ERROR: No input found!")
            await page.screenshot(path=str(OUTPUT / "error.png"))
            await browser.close()
            return

        await phone_input.click()
        await page.wait_for_timeout(300)
        await phone_input.fill("13800138000")
        print(f"  Filled: {await phone_input.input_value()}")

        # ---- Step 3: Click send code button ----
        print("\n[3] Looking for send-code button...")
        await page.screenshot(path=str(OUTPUT / "before_click.png"))

        send_btn = None
        btn_selectors = [
            'button:has-text("验证码")',
            'button:has-text("获取")',
            'button:has-text("发送")',
            '[class*="send"] button', '[class*="code"] button',
            'button:not([aria-label])',
        ]
        for sel in btn_selectors:
            try:
                els = await page.query_selector_all(sel)
                for el in els:
                    if await el.is_visible():
                        text = (await el.inner_text()).strip()
                        print(f"  Try '{sel}': '{text[:50]}'")
                        if "验证码" in text or "获取" in text or "发送" in text:
                            send_btn = el
                            break
            except:
                pass
            if send_btn:
                break

        # Fallback: find any element with class containing "send-code" or text matching
        if not send_btn:
            print("  Trying send-code div...")
            send_divs = await page.query_selector_all('.send-code, [class*="send"]')
            for el in send_divs:
                if await el.is_visible():
                    text = (await el.inner_text()).strip()
                    print(f"    send-code element: '{text[:60]}'")
                    if text:
                        send_btn = el
                        break

        if not send_btn:
            print("  Trying all visible buttons/divs with text...")
            for tag in ["button", "div", "span", "a"]:
                for el in await page.query_selector_all(tag):
                    if await el.is_visible():
                        text = (await el.inner_text()).strip()
                        if text and any(kw in text for kw in ["验证码", "获取", "发送", "code", "send"]):
                            send_btn = el
                            print(f"  -> MATCH {tag}: '{text[:60]}'")
                            break
                if send_btn:
                    break

        if send_btn:
            await send_btn.click()
            print("  Clicked send code button")
        else:
            print("  No send-code button found - checking full HTML...")
            html = await page.content()
            (OUTPUT / "page.html").write_text(html, encoding="utf-8")
            print("  Saved full HTML")

        # ---- Step 4: Watch for captcha frame ----
        print("\n[4] Waiting for captcha (45s)...")
        captcha_frame = None
        all_frames_logged = False

        for sec in range(45):
            # Log frames periodically
            if sec % 5 == 0:
                frame_count = len(page.frames)
                verify_frames = [f for f in page.frames if "verify" in f.url.lower() or "captcha" in f.url.lower()]
                if verify_frames:
                    for f in verify_frames:
                        print(f"  [{sec}s] CAPTCHA FRAME: {f.url[:120]}")
                        captcha_frame = f
                    break

                if not all_frames_logged:
                    for i, f in enumerate(page.frames):
                        if f.url != "about:blank":
                            print(f"    frame[{i}]: {f.url[:100]}")
                    if sec >= 3:
                        all_frames_logged = True

            await page.wait_for_timeout(1000)

        if not captcha_frame:
            print("  No captcha frame found after 45s")
            await page.screenshot(path=str(OUTPUT / "final.png"))
            await browser.close()
            return

        # ---- Step 5: Extract captcha images ----
        print("\n[5] Extracting captcha content...")
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(OUTPUT / "captcha_visible.png"))

        # Get all images from captcha frame
        try:
            imgs = await captcha_frame.query_selector_all("img")
            print(f"  {len(imgs)} images in captcha frame")
            for idx, img in enumerate(imgs):
                src = await img.get_attribute("src") or ""
                print(f"    [{idx}] {src[:100]}")
                await save_image(page, src, OUTPUT / f"frame_img_{idx}.png")

            # Try canvas
            canvases = await captcha_frame.query_selector_all("canvas")
            print(f"  {len(canvases)} canvases in captcha frame")
            for idx, c in enumerate(canvases):
                box = await c.bounding_box()
                if box:
                    await page.screenshot(
                        path=str(OUTPUT / f"canvas_{idx}.png"),
                        clip={"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}
                    )
                    print(f"    [{idx}] captured canvas ({box['width']}x{box['height']})")

            # Save frame HTML
            html = await captcha_frame.content()
            (OUTPUT / "captcha_frame.html").write_text(html, encoding="utf-8")
            print("  Saved frame HTML")
        except Exception as e:
            print(f"  Error: {e}")
            import traceback
            traceback.print_exc()

        # Also screenshot all frames
        page.screenshot(path=str(OUTPUT / "final_state.png"))

        # ---- Step 6: Test captcha-recognizer ----
        print("\n[6] Testing captcha-recognizer...")
        try:
            from captcha_recognizer.slider import Slider
            model = Slider()
            print("  Model loaded OK")

            pngs = sorted(OUTPUT.glob("*.png"))
            print(f"  Testing on {len(pngs)} images...")
            for p in pngs:
                try:
                    offset, conf = model.identify_offset(source=str(p), show=False)
                    if offset > 0:
                        print(f"    ✅ {p.name}: GAP at x={offset:.0f}px  confidence={conf:.3f}")
                    else:
                        print(f"    ❌ {p.name}: no gap (0, conf={conf:.3f})")
                except Exception as e:
                    print(f"    ❌ {p.name}: error - {e}")
        except ImportError:
            print("  captcha-recognizer.slider not importable")
            # Try from __init__
            try:
                from captcha_recognizer import Slider
                model = Slider()
                for p in sorted(OUTPUT.glob("*.png")):
                    offset, conf = model.identify_offset(source=str(p), show=False)
                    if offset > 0:
                        print(f"    ✅ {p.name}: GAP at x={offset:.0f}px  conf={conf:.3f}")
                    else:
                        print(f"    ❌ {p.name}: no gap (0, conf={conf:.3f})")
            except Exception as e2:
                print(f"  Still can't import: {e2}")

        print(f"\nSamples saved to {OUTPUT.resolve()}")
        input("\nPress Enter to close browser...")
        await browser.close()


async def save_image(page, src, path):
    if not src:
        return
    try:
        if src.startswith("data:"):
            header, data = src.split(",", 1)
            path.write_bytes(base64.b64decode(data))
            print(f"    -> saved {path.name}")
        elif src.startswith("http"):
            resp = await page.context.request.get(src)
            if resp.ok:
                path.write_bytes(await resp.body())
                print(f"    -> saved {path.name}")
    except Exception as e:
        print(f"    -> error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
