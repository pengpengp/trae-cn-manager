"""
Test script: verify if captcha-recognizer can identify Bytedance Verifycenter slider gap.
1. Launches Playwright, goes to trae.cn/login
2. Fills in a phone number, clicks "get verification code"
3. When slider captcha appears, capture the background image
4. Run captcha-recognizer on it to detect gap position
"""
import asyncio
import time
import json
from pathlib import Path

from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # Watch what happens
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

        # Intercept captcha images
        captcha_dir = Path("captcha_samples")
        captcha_dir.mkdir(exist_ok=True)
        img_counter = [0]

        # Listen for captcha-related network requests
        page.on("response", lambda response: asyncio.ensure_future(
            handle_captcha_response(response, captcha_dir, img_counter)
        ))

        print("1. Navigating to trae.cn/login...")
        try:
            await page.goto("https://www.trae.cn/login", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  Navigation warning: {e}")

        await page.wait_for_timeout(3000)

        # Find phone input and fill
        print("2. Looking for phone input...")
        phone_input = await page.query_selector('input[type="text"], input[placeholder*="手机"], input[placeholder*="phone"]')
        if phone_input:
            await phone_input.fill("13800138000")
            print("  Filled phone number")
        else:
            # Try finding any input
            inputs = await page.query_selector_all("input")
            print(f"  Found {len(inputs)} inputs")
            for i, inp in enumerate(inputs):
                ph = await inp.get_attribute("placeholder")
                print(f"    input[{i}]: placeholder={ph}")

        await page.wait_for_timeout(1000)

        # Find and click "get verification code" button
        print("3. Looking for send_code button...")
        btns = await page.query_selector_all("button, a, div[role=button]")
        send_btn = None
        for btn in btns:
            text = await btn.inner_text()
            if "验证码" in text or "发送" in text or "获取" in text:
                print(f"  Found button: '{text[:30]}'")
                send_btn = btn
                break

        if send_btn:
            await send_btn.click()
            print("  Clicked send code button")
        else:
            print("  No send code button found")

        # Wait for captcha to appear
        print("4. Waiting for captcha iframe/overlay (30s)...")
        for i in range(30):
            # Check for Bytedance captcha iframe
            frames = page.frames
            for frame in frames:
                url = frame.url
                if "verify" in url.lower() or "captcha" in url.lower() or "sec_sdk" in url:
                    print(f"  Found captcha frame: {url[:100]}")
                    # Screenshot the captcha
                    await page.screenshot(path=str(captcha_dir / "captcha_full.png"))

                    # Try to find the captcha element in this frame
                    try:
                        # Look for the captcha background image
                        imgs = await frame.query_selector_all("img")
                        for img in imgs:
                            src = await img.get_attribute("src") or ""
                            if "background" in src.lower() or "bg" in src.lower() or "captcha" in src.lower():
                                print(f"  Captcha bg image: {src[:100]}")
                                # Save the image URL for later download
                    except Exception as e:
                        print(f"  Error querying frame: {e}")

                    print(f"  Captcha appeared after ~{i+1}s")
                    await page.screenshot(path=str(captcha_dir / f"captcha_{i+1}s.png"))
                    break
            else:
                await page.wait_for_timeout(1000)
                continue
            break
        else:
            print("  No captcha detected after 30s")
            await page.screenshot(path=str(captcha_dir / "no_captcha.png"))

        # Wait a bit more to let the captcha fully render
        print("5. Waiting 10s for captcha to render...")
        await page.wait_for_timeout(10000)

        # Take final screenshot
        await page.screenshot(path=str(captcha_dir / "captcha_final.png"))
        print(f"6. Screenshots saved to {captcha_dir.resolve()}")

        # Save page HTML for analysis
        html = await page.content()
        with open(captcha_dir / "page.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("7. Page HTML saved")

        input("Press Enter to close browser...")
        await browser.close()


async def handle_captcha_response(response, save_dir, counter):
    """Save captcha-related images."""
    url = response.url
    if "verify" in url.lower() or "captcha" in url.lower():
        if "image" in response.headers.get("content-type", ""):
            idx = counter[0]
            counter[0] += 1
            try:
                body = await response.body()
                ext = "png" if "png" in url else "jpg"
                path = save_dir / f"captcha_img_{idx}.{ext}"
                path.write_bytes(body)
                print(f"  [saved] captcha image: {path.name}")
            except Exception as e:
                print(f"  [error] saving image: {e}")


if __name__ == "__main__":
    asyncio.run(main())
