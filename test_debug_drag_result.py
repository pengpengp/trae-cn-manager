"""
Detailed debug: what state is the captcha in after our drag?
- Check if puzzle snaps back
- Check captcha server response
- Screenshot after each drag
"""
import asyncio
import logging
import random
import re
import sys

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("debug")
logging.getLogger("playwright").setLevel(logging.WARNING)
sys.stdout.reconfigure(encoding="utf-8")


async def get_verify_frame(page) -> list:
    """Get details about the verify captcha frame."""
    results = []
    for f in page.frames:
        if "verifycenter" in f.url or "captcha" in f.url:
            info = {"url": f.url[:100]}
            try:
                # Check visibility of captcha box
                box = await f.query_selector("#vc_captcha_box, .vc-captcha")
                info["box_visible"] = await box.is_visible() if box else False
            except Exception:
                info["box_visible"] = None

            # Check for slider button
            try:
                btn = await f.query_selector(".captcha-slider-btn")
                if btn:
                    bb = await btn.bounding_box()
                    info["slider_btn"] = bb
            except Exception:
                pass

            # Check for puzzle piece
            try:
                positions = await f.evaluate("""() => {
                    var els = document.querySelectorAll('.dragger-item');
                    var pts = [];
                    for (var i = 0; i < els.length; i++) {
                        var r = els[i].getBoundingClientRect();
                        pts.push({x: r.left, y: r.top, w: r.width, h: r.height});
                    }
                    return pts;
                }""")
                info["puzzle_positions"] = positions
            except Exception:
                pass

            # Get all visible text in the captcha
            try:
                all_text = await f.evaluate("""() => {
                    return document.body ? document.body.innerText : '';
                }""")
                info["text"] = all_text[:500]
            except Exception:
                info["text"] = ""

            results.append(info)
    return results


async def main():
    phone = "19604191344"  # already clean

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            proxy={"server": "http://127.0.0.1:7897"},
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        page = await context.new_page()

        await page.goto("https://www.trae.cn/login", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        # Fill phone
        phone_input = await page.query_selector("input.mobile-phone")
        assert phone_input
        await phone_input.click()
        await phone_input.fill(phone)
        await page.wait_for_timeout(500)

        # Click send code
        send_btn = await page.query_selector("div.send-code")
        await send_btn.click()
        log.info("Clicked send code, waiting for captcha...")
        await page.wait_for_timeout(5000)

        # Find captcha frame
        captcha_frame = None
        for f in page.frames:
            if "verifycenter" in f.url:
                captcha_frame = f
                break
        assert captcha_frame, "Captcha frame not found"
        log.info("Found captcha frame: %s", captcha_frame.url[:100])

        # Detect gap
        from trae_cn_manager.auto_slider import AutoSlider
        solver = AutoSlider()
        solver._page = page
        solver._captcha_frame = captcha_frame

        log.info("=== Detection ===")
        offset, conf = await solver._detect_gap(captcha_frame)
        log.info("Gap: %.1fpx (conf=%.3f)", offset, conf)

        # Get initial puzzle position
        init_pos = await captcha_frame.evaluate("""() => {
            var els = document.querySelectorAll('.dragger-item');
            var pts = [];
            for (var i = 0; i < els.length; i++) {
                var r = els[i].getBoundingClientRect();
                pts.push(r.left);
            }
            return pts;
        }""")
        log.info("Initial puzzle positions: %s", init_pos)

        # Do a VERY slow, careful drag
        slider_btn = await captcha_frame.query_selector(".captcha-slider-btn")
        assert slider_btn
        btn_box = await slider_btn.bounding_box()
        log.info("Slider button box: %s", btn_box)

        start_x = btn_box["x"] + btn_box["width"] / 2
        start_y = btn_box["y"] + btn_box["height"] / 2
        target_x = start_x + offset

        log.info("Drag: (%.0f, %.0f) -> (%.0f, %.0f)  offset=%.0fpx",
                 start_x, start_y, target_x, start_y, offset)

        # Move to button
        await page.mouse.move(start_x, start_y)
        await page.wait_for_timeout(500)
        await page.mouse.down()
        await page.wait_for_timeout(100)

        # Slow drag with human-like jitter - ~500ms total for the drag
        n_steps = 40
        for i in range(n_steps + 1):
            progress = i / n_steps
            # Ease-out curve: fast start, slow end
            eased = 1 - (1 - progress) ** 2
            x = start_x + offset * eased
            y = start_y + 3 * (progress - 0.5) * (progress - 0.5) * 4 - 3  # U-shaped y jitter
            y += random.uniform(-1, 1)
            await page.mouse.move(x, y, steps=2)
            await page.wait_for_timeout(random.randint(8, 20))

        # Final position
        await page.mouse.move(target_x, start_y + random.uniform(-1, 1))
        await page.wait_for_timeout(200)
        await page.mouse.up()

        await page.wait_for_timeout(1000)

        # Check post-drag state
        final_pos = await captcha_frame.evaluate("""() => {
            var els = document.querySelectorAll('.dragger-item');
            var pts = [];
            for (var i = 0; i < els.length; i++) {
                var r = els[i].getBoundingClientRect();
                pts.push(r.left);
            }
            return pts;
        }""")
        log.info("Final puzzle positions: %s", final_pos)

        moved = abs((final_pos[0] if final_pos else 0) - (init_pos[0] if init_pos else 0))
        log.info("Puzzle moved by: %.0fpx (target %.0fpx)", moved, offset)

        # Check frame state
        frames_state = await get_verify_frame(page)
        log.info("=== Frame state after drag ===")
        for fs in frames_state:
            log.info("  url: %s", fs.get("url", ""))
            log.info("  box_visible: %s", fs.get("box_visible"))
            log.info("  slider_btn: %s", fs.get("slider_btn"))
            log.info("  puzzle_positions: %s", fs.get("puzzle_positions"))
            text = fs.get("text", "")
            if text:
                log.info("  text: %s", text[:300].replace("\n", " | "))

        # Take screenshot
        await page.screenshot(path="debug_drag_state.png")
        log.info("Screenshot: debug_drag_state.png")

        # Keep browser open for 2min to observe
        await page.wait_for_timeout(120000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
