"""
Quick test: capture Bytedance captcha from trae.cn and test captcha-recognizer on it.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

async def main():
    # First, try to find a demo Bytedance captcha page
    # Option A: Try the Bytedance captcha demo/test page
    # Option B: Go directly to trae.cn login page
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # headed so we can see the captcha
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        save_dir = Path("captcha_test_samples")
        save_dir.mkdir(exist_ok=True)

        print("=== Step 1: Navigate to trae.cn/login ===")
        try:
            await page.goto("https://www.trae.cn/login", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  Navigation warning: {e}")

        await page.wait_for_timeout(3000)

        # Save page state info
        print("\n=== Page Info ===")
        print(f"URL: {page.url}")
        print(f"Title: {await page.title()}")

        # Find all inputs
        inputs = await page.query_selector_all("input")
        print(f"\nFound {len(inputs)} inputs:")
        for i, inp in enumerate(inputs):
            ph = await inp.get_attribute("placeholder") or "(no placeholder)"
            _type = await inp.get_attribute("type") or "(no type)"
            _id = await inp.get_attribute("id") or "(no id)"
            print(f"  [{i}] type={_type} placeholder={ph} id={_id}")

        # Find all buttons
        buttons = await page.query_selector_all("button")
        print(f"\nFound {len(buttons)} buttons:")
        for i, btn in enumerate(buttons):
            text = (await btn.inner_text()).strip()[:50] or "(empty)"
            print(f"  [{i}] '{text}'")

        # Find all iframes
        frames = page.frames
        print(f"\nFound {len(frames)} frames:")
        for i, f in enumerate(frames):
            url = f.url
            if url != "about:blank":
                print(f"  [{i}] {url[:120]}")

        # Fill phone number
        print("\n=== Step 2: Fill phone number ===")
        phone_input = None
        for inp in inputs:
            ph = (await inp.get_attribute("placeholder") or "").lower()
            if "手机" in ph or "phone" in ph or "mobile" in ph:
                phone_input = inp
                break
        if not phone_input and inputs:
            phone_input = inputs[0]

        if phone_input:
            await phone_input.click()
            await page.wait_for_timeout(500)
            await phone_input.fill("13800138000")
            # Verify fill
            val = await phone_input.input_value()
            print(f"  Filled phone input: value='{val}'")
        else:
            print("  No phone input found!")

        # Click send code button
        print("\n=== Step 3: Click send code ===")
        send_btn = None
        for btn in buttons:
            text = (await btn.inner_text()).strip()
            if "验证码" in text or "发送" in text or "获取" in text:
                send_btn = btn
                print(f"  Found button: '{text}'")
                break

        if send_btn:
            await send_btn.click()
            print("  Clicked!")
        else:
            print("  No send code button found")

        # Wait for captcha
        print("\n=== Step 4: Monitor for captcha (max 30s) ===")
        for sec in range(30):
            # Check frames
            for f in page.frames:
                url = f.url
                if any(kw in url.lower() for kw in ["verify", "captcha", "sec_sdk", "vc_captcha"]):
                    print(f"  [{sec}s] CAPTCHA FRAME FOUND!")
                    print(f"    URL: {url[:150]}")
                    
                    # Screenshot
                    await page.screenshot(path=str(save_dir / f"captcha_{sec}s.png"))
                    print(f"    Screenshot saved: captcha_{sec}s.png")
                    
                    # Try to get captcha elements from this frame
                    try:
                        # Check for images inside captcha frame
                        imgs = await f.query_selector_all("img")
                        print(f"    Found {len(imgs)} images in captcha frame:")
                        for j, img in enumerate(imgs):
                            src = await img.get_attribute("src") or ""
                            alt = await img.get_attribute("alt") or ""
                            cls = await img.get_attribute("class") or ""
                            print(f"      [{j}] src={src[:100]}")
                            print(f"           alt={alt} class={cls}")
                            
                            # Download captcha images
                            if src and ("data:image" in src or "http" in src):
                                try:
                                    if src.startswith("data:"):
                                        import base64
                                        header, data = src.split(",", 1)
                                        img_bytes = base64.b64decode(data)
                                        ext = "png" if "png" in header else "jpg"
                                        (save_dir / f"captcha_img_{j}.{ext}").write_bytes(img_bytes)
                                        print(f"      -> saved captcha_img_{j}.{ext}")
                                    else:
                                        resp = await page.context.request.get(src)
                                        body = await resp.body()
                                        ext = "png" if "png" in src else "jpg"
                                        (save_dir / f"captcha_img_{j}.{ext}").write_bytes(body)
                                        print(f"      -> saved captcha_img_{j}.{ext}")
                                except Exception as e:
                                    print(f"      -> error saving: {e}")
                    except Exception as e:
                        print(f"    Error querying frame: {e}")
                    
                    # Also dump the html of this frame
                    try:
                        html = await f.content()
                        (save_dir / f"captcha_frame_{sec}s.html").write_text(html, encoding="utf-8")
                        print(f"    Saved frame HTML: captcha_frame_{sec}s.html")
                    except Exception as e:
                        print(f"    Error saving frame HTML: {e}")
                    
                    # Get all element handles
                    try:
                        all_els = await f.query_selector_all("*")
                        print(f"    Total elements in frame: {len(all_els)}")
                    except:
                        pass
                        
                    await page.wait_for_timeout(5000)  # Wait for full render
                    await page.screenshot(path=str(save_dir / "captcha_rendered.png"))
                    break

            # Check for captcha div on main page too
            captcha_els = await page.query_selector_all('[class*="captcha"], [class*="verify"], [id*="captcha"], [id*="verify"]')
            for el in captcha_els:
                outer = await el.get_attribute("outerHTML") or ""
                if len(outer) > 100:
                    outer = outer[:100] + "..."
                print(f"    Captcha element on main: {outer}")
            
            await page.wait_for_timeout(1000)

        # Final state
        print(f"\n=== Final State ===")
        print(f"URL: {page.url}")
        await page.screenshot(path=str(save_dir / "final.png"))

        # Dump all frames
        print(f"\n=== All Frames ({len(page.frames)}) ===")
        for i, f in enumerate(page.frames):
            url = f.url
            if url != "about:blank":
                name = f.name
                print(f"  [{i}] name='{name}' url={url[:100]}")

        print(f"\nDone! Samples saved to: {save_dir.resolve()}")
        input("\nPress Enter to close browser...")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
