"""
auto_slider.py — Automatic Bytedance Verifycenter slider captcha solver.

Uses captcha-recognizer (YOLO model) to detect the slider gap position,
then Playwright trusted mouse events to perform a human-like drag.

Usage:
    solver = AutoSlider()
    success = await solver.solve(page, phone_number="13800138000")
"""
import asyncio
import base64
import logging
import math
import random
import re
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
from playwright.async_api import Page, Frame

logger = logging.getLogger(__name__)

# Try importing captcha-recognizer
try:
    from captcha_recognizer.slider import Slider as _Slider
    _MODEL = _Slider()
    _MODEL_AVAILABLE = True
except ImportError:
    _MODEL_AVAILABLE = False
    logger.warning("captcha-recognizer not installed. Auto-slider disabled.")


class AutoSlider:
    """Solve Bytedance slider captcha automatically using computer vision."""

    # Selector cache (discovered from captcha frame HTML analysis)
    SELECTORS = {
        "phone_input": 'input[placeholder*="手机"], input.mobile-phone',
        "send_code_btn": ".send-code, [class*='send-code'], [class*='send_code']",
        "captcha_iframe": 'iframe[src*="verifycenter"]',
        "bg_image": "#captcha_verify_image, img[alt='basicImg'], img.captcha-verify-image, [class*='verify-image'] img",
        "slider_btn": ".captcha-slider-btn",
        "puzzle_piece_in_image": ".verify-image .dragger-item, .captcha_verify_img--wrapper .dragger-item",
        "dragged_area": ".captcha-slider-dragged-area",
        "refresh_btn": ".vc-captcha-refresh",
    }

    def __init__(self):
        self._page: Optional[Page] = None
        self._captcha_frame: Optional[Frame] = None

    async def solve(
        self,
        page: Page,
        phone_number: str = "13800138000",
        max_wait_captcha: int = 30,
    ) -> bool:
        """
        Complete flow: fill phone → trigger captcha → auto-solve.

        Returns True if captcha was successfully solved.
        """
        self._page = page

        if not _MODEL_AVAILABLE:
            logger.error("captcha-recognizer not installed. Run: pip install captcha-recognizer")
            return False

        # Apply stealth patches to ALL frames (including captcha iframe)
        logger.info("Applying stealth patches...")
        await page.add_init_script("""() => {
            // Override navigator.webdriver (key Playwright detection vector)
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // Realistic plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                    { name: 'Native Client', filename: 'internal-nacl-plugin' },
                ]
            });

            // Realistic languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['zh-CN', 'zh', 'en-US', 'en']
            });

            // chrome.runtime (some captchas check)
            if (!window.chrome) window.chrome = {};
            if (!window.chrome.runtime) window.chrome.runtime = {};

            // Override on prototype too
            try { delete navigator.__proto__.webdriver; } catch (e) {}
        }""")

        # Step 1: Fill phone number
        logger.info("Filling phone number...")
        phone_el = await page.query_selector(self.SELECTORS["phone_input"])
        if not phone_el:
            logger.error("Phone input not found")
            return False
        # Format: strip leading 86/+86/00, keep last 11 digits
        raw = re.sub(r"^86|\+86|^00", "", phone_number.strip())
        digits = re.sub(r"\D", "", raw)
        clean_phone = digits[-11:] if len(digits) > 11 else digits
        await phone_el.click()
        await page.wait_for_timeout(200)
        await phone_el.fill(clean_phone)

        # Step 2: Click send code to trigger captcha
        logger.info("Clicking send code button...")
        send_el = None
        # Try each selector
        for sel in self.SELECTORS["send_code_btn"].split(", "):
            send_el = await page.query_selector(sel)
            if send_el:
                break
        if not send_el:
            # Fallback: find by text
            for tag in ["div", "span", "button"]:
                for el in await page.query_selector_all(tag):
                    if await el.is_visible():
                        text = (await el.inner_text()).strip()
                        if "获取验证码" in text:
                            send_el = el
                            break
                if send_el:
                    break
        if not send_el:
            logger.error("Send code button not found")
            return False

        await send_el.click()
        logger.info("Send code clicked, waiting for captcha...")

        # Step 3: Wait for captcha iframe
        captcha_frame = await self._wait_for_captcha_frame(page, max_wait_captcha)
        if not captcha_frame:
            logger.error("Captcha iframe did not appear")
            return False

        self._captcha_frame = captcha_frame
        await page.wait_for_timeout(2000)  # Let it render

        # Step 4: Detect gap position (with retry on failure)
        display_offset, confidence = 0.0, 0.0
        for det_retry in range(3):
            logger.info("Detecting slider gap (attempt %d)...", det_retry + 1)
            display_offset, confidence = await self._detect_gap(captcha_frame)
            if display_offset > 0:
                break
            logger.warning("Gap detection failed (offset=%.1f, conf=%.3f), retrying...",
                           display_offset, confidence)
            # Click refresh and try again with a new image
            await self._click_refresh(captcha_frame)
            await page.wait_for_timeout(2000)

        if display_offset <= 0:
            logger.error("Gap detection failed after 3 retries")
            return False

        logger.info(f"Gap detected at {display_offset:.1f}px (confidence={confidence:.3f})")

        # Step 5-6: Drag slider with offset probing
        # Model is consistently off by ~25px; probe wider range efficiently
        base_offset = display_offset
        # Start with model, then jump +25px (calibrated error), then probe around
        deltas = [0, 25, 10, 15, 20, 30, 5, 35, -5, -10, 8, 18, 22, 28, -3, -8]
        offsets_unique: list[float] = []
        seen_offsets: set[int] = set()
        for d in deltas:
            o = base_offset + d
            key = round(o / 5) * 5
            if o > 0 and key not in seen_offsets:
                seen_offsets.add(key)
                offsets_unique.append(o)

        for attempt_idx, try_offset in enumerate(offsets_unique):
            if attempt_idx > 0:
                logger.info(f"Trying offset {try_offset:.0f}px (attempt {attempt_idx + 1})...")

            drag_ok = await self._drag_slider(captcha_frame, try_offset)
            if not drag_ok:
                logger.warning(f"Drag failed at {try_offset:.0f}px")
                if attempt_idx + 1 < len(offsets_unique):
                    await self._click_refresh(captcha_frame)
                    await page.wait_for_timeout(2000)
                continue

            # Short wait, then check if solved
            await page.wait_for_timeout(1200)
            if await self._verify_solved(captcha_frame):
                logger.info(f"Captcha SOLVED at offset {try_offset:.0f}px!")
                return True

            # Refresh for next attempt
            if attempt_idx + 1 < len(offsets_unique):
                await self._click_refresh(captcha_frame)
                await page.wait_for_timeout(2000)

        logger.warning("Captcha not solved after all offset attempts")
        return False

    async def _wait_for_captcha_frame(
        self, page: Page, timeout: int = 30
    ) -> Optional[Frame]:
        """Wait for the Bytedance Verifycenter iframe to appear."""
        for _ in range(timeout):
            for frame in page.frames:
                url = frame.url
                if "verifycenter" in url and "captcha" in url:
                    return frame
            await page.wait_for_timeout(1000)
        return None

    async def _detect_gap(
        self, frame: Frame
    ) -> Tuple[float, float]:
        """
        Download captcha background image and detect gap position.

        Returns:
            (display_offset_px, confidence)
            display_offset_px: pixels to drag the slider (in display coordinates)
            confidence: model confidence (0-1)
        """
        # 1. Get background image
        bg_img_el = await frame.query_selector(self.SELECTORS["bg_image"])
        if not bg_img_el:
            # Try alternative selector
            bg_img_el = await frame.query_selector("img.captcha-verify-image, #captcha_verify_image, [class*='verify-image']")
        if not bg_img_el:
            logger.error("Background image element not found in captcha frame")
            return (0.0, 0.0)

        bg_src = await bg_img_el.get_attribute("src")
        if not bg_src:
            logger.error("Background image has no src attribute")
            return (0.0, 0.0)
        logger.debug("Background image src: %s ...", bg_src[:80])

        # 2. Download
        bg_bytes = None
        if bg_src.startswith("http"):
            try:
                resp = await self._page.context.request.get(bg_src, timeout=15000)
                if resp.ok:
                    bg_bytes = await resp.body()
                else:
                    logger.warning("Image download returned status %d", resp.status)
            except Exception as exc:
                logger.warning("Image download exception: %s", exc)
        elif bg_src.startswith("data:"):
            try:
                _, data = bg_src.split(",", 1)
                bg_bytes = base64.b64decode(data)
            except Exception as exc:
                logger.warning("Base64 decode failed: %s", exc)

        if not bg_bytes:
            logger.error("Failed to download background image (src=%s...)", bg_src[:60])
            return (0.0, 0.0)

        logger.debug("Downloaded %d bytes", len(bg_bytes))

        # 3. Run model
        img_cv = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img_cv is None:
            logger.error("cv2.imdecode failed (bad image data)")
            return (0.0, 0.0)

        natural_h, natural_w = img_cv.shape[:2]
        logger.debug("Image dimensions: %dx%d", natural_w, natural_h)

        # Save to temp file for model (model reads from path)
        temp_path = Path("_slider_temp.jpg")
        temp_path.write_bytes(bg_bytes)
        model_offset, model_conf = _MODEL.identify_offset(source=str(temp_path), show=False)
        temp_path.unlink(missing_ok=True)

        if model_offset <= 0:
            logger.debug("Model detected no gap (offset=%s, conf=%s)", model_offset, model_conf)
            return (0.0, model_conf)

        # 4. Scale: model space (640x640 letterbox) -> display space
        bg_box = await bg_img_el.bounding_box()
        display_w = bg_box["width"] if bg_box else 340.0

        # Model uses 640x640 with letterbox (pad to maintain aspect ratio)
        model_scale = min(640.0 / natural_w, 640.0 / natural_h)
        padded_w = natural_w * model_scale
        pad_left = (640.0 - padded_w) / 2.0

        # Remove model padding and scale to natural size
        offset_natural = (model_offset - pad_left) / model_scale

        # Scale from natural to display
        display_offset = offset_natural * (display_w / natural_w)

        return (display_offset, model_conf)

    async def _drag_slider(
        self, frame: Frame, display_offset: float
    ) -> bool:
        """
        Drag the slider button by display_offset pixels using trusted mouse events.

        Returns True if the puzzle piece actually moved.
        """
        # 1. Get slider button page coordinates
        slider_btn = await frame.query_selector(self.SELECTORS["slider_btn"])
        if not slider_btn:
            logger.error("Slider button not found")
            return False

        btn_box = await slider_btn.bounding_box()
        if not btn_box:
            return False

        # Record initial puzzle position to verify movement
        initial_positions = await self._get_puzzle_positions(frame)

        start_x = btn_box["x"] + btn_box["width"] / 2.0
        start_y = btn_box["y"] + btn_box["height"] / 2.0
        end_x = start_x + display_offset

        # 2. Move to button
        await self._page.mouse.move(start_x, start_y)
        await self._page.wait_for_timeout(random.randint(200, 400))

        # 3. Press
        await self._page.mouse.down()
        await self._page.wait_for_timeout(random.randint(30, 60))

        # 4. Generate human-like trajectory and execute
        steps = self._human_track(display_offset)
        for step in steps:
            await self._page.mouse.move(
                start_x + step["x"],
                start_y + step.get("y", 0),
                steps=1,
            )
            await self._page.wait_for_timeout(step.get("_delay", 15))

        # 5. Final position and release
        await self._page.wait_for_timeout(random.randint(50, 150))
        await self._page.mouse.move(
            end_x + random.uniform(-0.5, 0.5),
            start_y + random.uniform(-0.5, 0.5),
        )
        await self._page.wait_for_timeout(random.randint(80, 200))
        await self._page.mouse.up()

        # 6. Verify movement
        await self._page.wait_for_timeout(300)
        final_positions = await self._get_puzzle_positions(frame)

        moved = False
        for init, final in zip(initial_positions, final_positions):
            if abs(final["x"] - init["x"]) > 5:
                moved = True
                break

        if not moved:
            logger.warning("Puzzle piece did not move after drag")
            return False

        actual_dx = final_positions[0]["x"] - initial_positions[0]["x"]
        logger.info(f"Puzzle moved by {actual_dx:.0f}px (target {display_offset:.0f}px)")
        return True

    def _human_track(self, distance: float) -> list:
        """
        Generate a human-like drag trajectory.

        Velocity profile (non-linear position mapping):
          Slow start  →  Fast middle  →  Slow end
        Combined with variable per-step delays (8-40ms).

        Y-axis: natural U-shaped drift + noise.
        """
        if distance <= 0:
            return []

        n_steps = random.randint(30, 50)
        steps = []
        prev_x = 0.0

        for i in range(1, n_steps + 1):
            # Non-linear position using cubic ease-in-out
            t = i / n_steps
            if t < 0.5:
                pos_t = 2 * t * t
            else:
                pos_t = 1 - (-2 * t + 2) ** 2 / 2

            x_pos = distance * pos_t
            dx = x_pos - prev_x
            prev_x = x_pos

            # Skip if no meaningful movement
            if dx < 0.3 and t < 1.0:
                continue

            # Delay: inversely proportional to step size (smaller step = longer wait)
            step_ratio = max(dx / distance, 0.001) if distance > 0 else 0.01
            delay = int(8 + (1 - step_ratio * 2) * random.uniform(5, 20))
            delay = max(6, min(delay, 50))

            # Y jitter: U-shaped drift + noise
            y_drift = -3 * math.sin(t * math.pi)  # 0 → -3 → 0
            y_noise = random.uniform(-1.5, 1.5)
            y = y_drift + y_noise

            steps.append({
                "x": round(x_pos, 1),
                "y": round(y, 1),
                "_delay": delay,
            })

        # Optional: overshoot + settle (20% chance)
        if distance > 40 and random.random() < 0.2:
            overshoot = min(distance * 0.02, 5)
            steps.append({"x": round(distance + overshoot, 1), "y": 0, "_delay": 45})
            steps.append({"x": round(distance, 1), "y": 0, "_delay": 60})

        return steps

    async def _nudge_slider(self, frame: Frame, delta_px: float) -> None:
        """Nudge the slider by delta_px and release."""
        slider_btn = await frame.query_selector(self.SELECTORS["slider_btn"])
        if not slider_btn:
            return
        btn_box = await slider_btn.bounding_box()
        if not btn_box:
            return

        cx = btn_box["x"] + btn_box["width"] / 2
        cy = btn_box["y"] + btn_box["height"] / 2

        # Move to center of button, press, nudge, release
        await self._page.mouse.move(cx, cy)
        await self._page.wait_for_timeout(random.randint(100, 200))
        await self._page.mouse.down()
        await self._page.wait_for_timeout(random.randint(30, 60))

        steps = 5 if abs(delta_px) > 5 else 3
        for i in range(1, steps + 1):
            partial = cx + delta_px * (i / steps)
            await self._page.mouse.move(partial, cy + random.uniform(-0.5, 0.5), steps=1)
            await self._page.wait_for_timeout(random.randint(20, 40))

        await self._page.wait_for_timeout(random.randint(80, 150))
        await self._page.mouse.up()

    async def _get_puzzle_positions(self, frame: Frame) -> list:
        """Get current positions of all dragger-item elements."""
        return await frame.evaluate("""() => {
            var all = document.querySelectorAll('.dragger-item');
            var result = [];
            for (var i = 0; i < all.length; i++) {
                var r = all[i].getBoundingClientRect();
                var style = all[i].getAttribute('style') || '';
                result.push({x: r.left, y: r.top, w: r.width, h: r.height, style: style});
            }
            return result;
        }""")

    async def _verify_solved(self, frame: Frame) -> bool:
        """Check if the captcha was solved (overlay gone or no verify frames)."""
        # Check if verify frames still exist
        for f in self._page.frames:
            if "verifycenter" in f.url:
                # Frame still exists, check if visible
                try:
                    visible = await f.is_visible("#vc_captcha_box")
                    if not visible:
                        return True
                except Exception:
                    return True  # Frame detached
                return False
        return True  # No verify frames = solved

    async def has_captcha(self, page: Page) -> bool:
        """Check if a captcha is currently visible on the page."""
        for frame in page.frames:
            if "verifycenter" in frame.url:
                return True
        return False

    async def _get_captcha_error(self, frame: Frame) -> str:
        """Check if the captcha frame shows an error message."""
        try:
            for sel in [
                ".captcha-verify-failed",
                ".vc-captcha-error",
                "[class*='error']",
                "[class*='tips']",
                "[class*='message']",
            ]:
                el = await frame.query_selector(sel)
                if el and await el.is_visible():
                    text = await el.inner_text()
                    if text.strip():
                        return text.strip()
        except Exception:
            pass
        return ""

    async def _click_refresh(self, frame: Frame) -> None:
        """Click the refresh button on the captcha (for retry)."""
        for sel in [".vc-captcha-refresh", ".captcha-refresh", "[class*='refresh']"]:
            refresh_btn = await frame.query_selector(sel)
            if refresh_btn and await refresh_btn.is_visible():
                await refresh_btn.click()
                await self._page.wait_for_timeout(2000)
                return
        # Fallback: find by title/aria-label
        for el in await frame.query_selector_all("div, span, button"):
            try:
                title = await el.get_attribute("title") or ""
                aria = await el.get_attribute("aria-label") or ""
                text = await el.inner_text()
                if any(k in title.lower() or k in aria.lower() or k in text.lower()
                       for k in ["refresh", "reload", "刷新"]):
                    await el.click()
                    await self._page.wait_for_timeout(2000)
                    return
            except Exception:
                pass
