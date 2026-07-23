"""SMS provider abstraction + text-verification.net implementation.

Provides an abstract ``SmsProvider`` base and concrete providers that
scrape free online SMS sites using ``httpx`` + ``BeautifulSoup``.
"""
from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from .config import get_proxy

logger = logging.getLogger(__name__)

_OTP_RE = re.compile(r"\b(\d{6})\b")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PhoneNumber:
    phone: str = ""           # e.g. "8619604191344"
    country: str = "CN"
    carrier: str = ""         # CT / CM / CU
    recent_count: int = 0     # how many SMS already received (proxy for "dirtiness")
    last_active: str = ""     # human-readable relative time
    raw_label: str = ""       # full carrier label from page


@dataclass
class SmsMessage:
    sender: str = ""
    content: str = ""
    timestamp: str = ""
    is_otp: bool = False
    otp_code: str = ""
    raw: str = ""


# ---------------------------------------------------------------------------
# Abstract provider
# ---------------------------------------------------------------------------

class SmsProvider(ABC):
    """Abstract SMS provider."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def get_available_numbers(self) -> list[PhoneNumber]: ...

    @abstractmethod
    def get_messages(self, phone: str) -> list[SmsMessage]: ...

    def wait_for_otp(
        self,
        phone: str,
        timeout: int = 180,
        poll_interval: int = 10,
    ) -> Optional[str]:
        """Poll *get_messages* until a NEW OTP code appears.

        First poll establishes a baseline of existing messages (old messages
        on public SMS receiving sites). Subsequent polls only look for
        NEW messages containing a 6-digit OTP code.

        Returns the OTP code, or *None* on timeout.
        """
        deadline = time.time() + timeout
        known: set[str] = set()
        first_poll = True
        phone_digits = re.sub(r"\D", "", phone)

        while time.time() < deadline:
            msgs = self.get_messages(phone)

            if first_poll:
                for m in msgs:
                    known.add(m.content)
                logger.info("Baseline: %d existing messages recorded for %s", len(msgs), phone)
                first_poll = False
                time.sleep(poll_interval)
                continue

            new_msgs = [m for m in msgs if m.content not in known]
            for m in new_msgs:
                known.add(m.content)
                if m.is_otp and m.otp_code:
                    if m.otp_code in phone_digits:
                        logger.warning("Skipping OTP %s (matches phone number)", m.otp_code)
                        continue
                    logger.info("OTP found: %s  (sender=%s)", m.otp_code, m.sender)
                    return m.otp_code

            remaining = int(deadline - time.time())
            logger.debug("No new OTP yet, sleeping %ds (%ds left)", poll_interval, remaining)
            time.sleep(poll_interval)

        logger.warning("wait_for_otp timed out after %ds", timeout)
        return None


def _build_client() -> httpx.Client:
    proxy = get_proxy()
    client_kw: Dict = {
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        },
        "timeout": 30,
    }
    if proxy:
        client_kw["proxy"] = proxy
    return httpx.Client(**client_kw)


# ---------------------------------------------------------------------------
# TextVerificationProvider  (primary)
# ---------------------------------------------------------------------------

class TextVerificationProvider(SmsProvider):
    """Scrapes text-verification.net for +86 numbers and SMS messages.

    * 44 × +86 numbers
    * Pure HTML, no Cloudflare, no JS needed
    """

    BASE_URL = "https://text-verification.net"

    @property
    def name(self) -> str:
        return "text-verification"

    def get_available_numbers(self) -> list[PhoneNumber]:
        """Fetch +86 numbers from the CN country page."""
        url = f"{self.BASE_URL}/country/CN"
        logger.info("Fetching numbers from %s", url)
        with _build_client() as client:
            resp = client.get(url)
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        numbers: list[PhoneNumber] = []

        # The numbers are listed in div.number-item or similar elements.
        # From the observed HTML structure, look for number blocks.
        # Each block contains: phone number, carrier, last activity.
        for block in soup.select("div.number-item, div[class*=number]"):
            text = block.get_text(separator=" ", strip=True)
            if not text:
                continue

            # Extract phone (starts with +86)
            phone_match = re.search(r"\+86\s*([\d\s]{8,15})", text)
            if not phone_match:
                continue
            phone_digits = re.sub(r"\s+", "", phone_match.group(0))
            phone = phone_digits.replace("+", "").replace(" ", "")  # "8619604191344"

            # Carrier
            carrier = ""
            for abbr, full in [("CT", "China Telecom"), ("CM", "China Mobile"), ("CU", "China Unicom")]:
                if abbr in text or full in text:
                    carrier = abbr
                    break

            # Last activity
            last_active = ""
            m = re.search(r"last\s*activity[:\s]*(.+?)(?:\||$)", text, re.IGNORECASE)
            if m:
                last_active = m.group(1).strip()

            numbers.append(PhoneNumber(
                phone=phone,
                country="CN",
                carrier=carrier,
                recent_count=0,  # not easily counted from this page
                last_active=last_active,
                raw_label=text[:100],
            ))

        if not numbers:
            # Fallback: try a more generic selector
            for section in soup.find_all("div", class_=lambda c: c and "number" in c):
                links = section.find_all("a")
                for link in links:
                    href = link.get("href", "")
                    if "/number/" not in href:
                        continue
                    lbl = link.get_text(strip=True)
                    if not lbl:
                        continue
                    phone_digits = re.sub(r"\D", "", href.replace("/number/", ""))
                    if not phone_digits:
                        continue
                    carrier = ""
                    parent_text = section.get_text()
                    for abbr, full in [("CT", "China Telecom"), ("CM", "China Mobile"), ("CU", "China Unicom")]:
                        if abbr in parent_text or full in parent_text:
                            carrier = abbr
                            break
                    numbers.append(PhoneNumber(
                        phone=phone_digits,
                        country="CN",
                        carrier=carrier,
                        recent_count=0,
                        last_active="",
                        raw_label=lbl,
                    ))

        # Deduplicate by phone
        seen: set[str] = set()
        unique: list[PhoneNumber] = []
        for n in numbers:
            if n.phone not in seen:
                seen.add(n.phone)
                unique.append(n)

        logger.info("Found %d +86 numbers from text-verification.net", len(unique))
        return unique

    def get_messages(self, phone: str) -> list[SmsMessage]:
        """Fetch SMS messages for a given phone number."""
        # Normalise: strip leading + or 00, ensure starts with country code
        phone_clean = re.sub(r"^00|\+", "", phone.strip())
        url = f"{self.BASE_URL}/number/{phone_clean}"
        logger.info("Fetching messages from %s", url)
        with _build_client() as client:
            resp = client.get(url)
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        messages: list[SmsMessage] = []

        # From observation, messages are in a container with class "incoming-sms"
        # or simply divs with sender/time/content
        msg_container = soup.find(class_=lambda c: c and "incoming" in c.lower())
        if not msg_container:
            # Try finding all message-like divs
            msg_container = soup

        # Each message appears to be a div or block with sender/time/content
        # Look for consecutive text blocks
        sender = ""
        timestamp = ""
        content = ""

        for elem in msg_container.find_all(["div", "p", "span"], recursive=True):
            text = elem.get_text(strip=True)
            if not text or len(text) < 2:
                continue

            # Skip non-message elements
            if elem.find_parent(class_=lambda c: c and "header" in c.lower()):
                continue
            if elem.find_parent(class_=lambda c: c and "footer" in c.lower()):
                continue

            # Check if this looks like a message block
            cls = " ".join(elem.get("class", []))
            if "message" in cls.lower() or "sms" in cls.lower() or "incoming" in cls.lower():
                # Parse structured message block
                sender_el = elem.find(class_=lambda c: c and "sender" in c.lower())
                time_el = elem.find(class_=lambda c: c and ("time" in c.lower() or "date" in c.lower()))
                content_el = elem.find(class_=lambda c: c and "content" in c.lower())

                msg_sender = sender_el.get_text(strip=True) if sender_el else ""
                msg_time = time_el.get_text(strip=True) if time_el else ""
                msg_content = content_el.get_text(strip=True) if content_el else ""

                if msg_content:
                    otp_match = _OTP_RE.search(msg_content) if msg_content else None
                    messages.append(SmsMessage(
                        sender=msg_sender,
                        content=msg_content,
                        timestamp=msg_time,
                        is_otp=bool(otp_match),
                        otp_code=otp_match.group(1) if otp_match else "",
                    ))

        if not messages:
            # Last resort: parse by scanning text blocks for known patterns
            all_text = soup.get_text(separator="\n")
            lines = [l.strip() for l in all_text.split("\n") if l.strip()]

            # Try to find message blocks in the raw text
            # Pattern: sender \n time \n content
            i = 0
            while i < len(lines):
                line = lines[i]
                # Skip non-message lines
                if len(line) < 3:
                    i += 1
                    continue
                if any(skip in line.lower() for skip in ["incoming sms", "how to use", "faq", "cookie", "privacy"]):
                    i += 1
                    continue

                # Check if this line looks like a sender (short, no numbers)
                if len(line) < 30 and not re.search(r"\d{4,}", line):
                    msg_sender = line
                    msg_time = lines[i + 1] if i + 1 < len(lines) else ""
                    msg_content = lines[i + 2] if i + 2 < len(lines) else ""

                    # Verify it looks like a real message block
                    if msg_content and len(msg_content) > 5:
                        otp_match = _OTP_RE.search(msg_content) if msg_content else None
                        messages.append(SmsMessage(
                            sender=msg_sender,
                            content=msg_content,
                            timestamp=msg_time,
                            is_otp=bool(otp_match),
                            otp_code=otp_match.group(1) if otp_match else "",
                        ))
                        i += 3
                        continue
                i += 1

        logger.info("Found %d messages for %s", len(messages), phone)
        return messages


# ---------------------------------------------------------------------------
# ReceiveSmsIoProvider  (9+ active +86 numbers)
# ---------------------------------------------------------------------------

class ReceiveSmsIoProvider(SmsProvider):
    """Scrapes receive-sms.io for +86 numbers and messages.

    * 9+ active Chinese numbers, updated weekly
    * Public inbox with real-time message display
    * Easy HTML parsing, no Cloudflare
    """

    BASE_URL = "https://receive-sms.io"
    COUNTRY_URL = "/temporary-numbers/china/"

    @property
    def name(self) -> str:
        return "receive-sms-io"

    def get_available_numbers(self) -> list[PhoneNumber]:
        url = f"{self.BASE_URL}{self.COUNTRY_URL}"
        logger.info("Fetching numbers from %s", url)
        with _build_client() as client:
            resp = client.get(url)
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        numbers: list[PhoneNumber] = []

        # Numbers are listed as <a> tags with href="/temporary-numbers/china/8612345678901/"
        # Page is localised: "活跃" (zh) or "active" (en).
        for link in soup.find_all("a", href=re.compile(r"/temporary-numbers/china/\d{10,}/")):
            text = link.get_text(strip=True)
            if not text:
                continue
            phone_digits = re.sub(r"\D", "", text)
            if not phone_digits or len(phone_digits) < 10:
                continue

            # receive-sms.io country page only lists live numbers, so we treat
            # every listed number as active (no extra status filter needed).
            parent_text = link.parent.get_text() if link.parent else ""

            # Extract "已添加: X ago" / "Added: X ago" from surrounding text.
            added_str = ""
            m = re.search(r"(?:Added|已添加)[:\s]*([^\n|<]+)", parent_text)
            if m:
                added_str = m.group(1).strip()

            numbers.append(PhoneNumber(
                phone=phone_digits,
                country="CN",
                carrier="",
                recent_count=0,
                last_active=added_str,
                raw_label=text,
            ))

        # Deduplicate
        seen: set[str] = set()
        unique: list[PhoneNumber] = []
        for n in numbers:
            if n.phone not in seen:
                seen.add(n.phone)
                unique.append(n)

        logger.info("Found %d +86 numbers from receive-sms.io", len(unique))
        return unique

    def get_messages(self, phone: str) -> list[SmsMessage]:
        phone_clean = re.sub(r"^00|\+|^86", "", phone.strip())
        if not phone_clean.startswith("86"):
            phone_clean = "86" + phone_clean
        url = f"{self.BASE_URL}/temporary-numbers/china/{phone_clean}/"
        logger.info("Fetching messages from %s", url)
        try:
            with _build_client() as client:
                resp = client.get(url, timeout=30)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("receive-sms.io fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        messages: list[SmsMessage] = []

        # The message body area typically contains <p> or <div> elements
        # with sender, "recently" / timestamp, and content.
        # Extract all block-level text and parse 3-line groups.

        # Strategy: get the main content div and extract all text blocks
        main = soup.find(class_=lambda c: c and ("content" in c.lower() or "main" in c.lower() or "message" in c.lower()))
        if not main:
            main = soup

        # Get all substantial text elements
        text_blocks: list[str] = []
        for tag in main.find_all(["p", "div", "span"], recursive=True):
            t = tag.get_text(strip=True)
            if not t or len(t) < 2:
                continue
            # Skip nav, header, footer elements
            cls = " ".join(tag.get("class", []))
            if any(skip in cls.lower() for skip in ["header", "footer", "nav", "menu"]):
                continue
            # Skip known non-message text
            if any(skip in t.lower() for skip in [
                "faq", "how to use", "cookies", "privacy policy", "terms",
                "receive sms free", "choose another number", "vip numbers",
                "does not come sms", "number unavailable", "possible reasons",
            ]):
                continue
            text_blocks.append(t)

        # Parse 3-line message groups: sender, timestamp, content
        # Messages often look like:
        #   CloudSigma
        #   recently
        #   Your CloudSigma verification code is 090444
        i = 0
        while i < len(text_blocks) - 2:
            sender = text_blocks[i]
            ts = text_blocks[i + 1]
            content = text_blocks[i + 2]

            # Skip if sender looks like a timestamp/nav
            if re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}", sender):
                i += 1
                continue
            if sender in ("Active", "Online", "Offline", "China"):
                i += 1
                continue
            # Skip if sender has "http" or is very long
            if "http" in sender or len(sender) > 40:
                i += 1
                continue

            # Validate that content looks like SMS content (has some substance)
            if content and len(content) > 5:
                otp_match = _OTP_RE.search(content)
                messages.append(SmsMessage(
                    sender=sender,
                    content=content,
                    timestamp=ts if not ts.startswith("recent") else "",
                    is_otp=bool(otp_match),
                    otp_code=otp_match.group(1) if otp_match else "",
                ))
                i += 3
                continue
            i += 1

        logger.info("Found %d messages for %s from receive-sms.io", len(messages), phone)
        return messages


# ---------------------------------------------------------------------------
# QuackrProvider  (when free China numbers are available)
# ---------------------------------------------------------------------------

class QuackrProvider(SmsProvider):
    """Scrapes quackr.io for +86 numbers (when available).

    * Large provider but often has "No numbers currently available for China"
    * Falls through gracefully when empty
    """

    BASE_URL = "https://quackr.io"
    COUNTRY_URL = "/temporary-numbers/china"

    @property
    def name(self) -> str:
        return "quackr"

    def get_available_numbers(self) -> list[PhoneNumber]:
        url = f"{self.BASE_URL}{self.COUNTRY_URL}"
        logger.info("Fetching numbers from %s", url)
        try:
            with _build_client() as client:
                resp = client.get(url, timeout=30)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("quackr.io fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        page_text = soup.get_text()

        # Check for "No numbers currently available"
        if "no numbers currently available" in page_text.lower():
            logger.info("quackr.io has no China numbers currently available")
            return []

        # Numbers appear as clickable elements with the phone number
        numbers: list[PhoneNumber] = []
        for link in soup.find_all("a", href=re.compile(r"/temporary-numbers/china/[\w-]+")):
            text = link.get_text(strip=True)
            if not text:
                continue
            phone_match = re.search(r"(\+?86[\d\s\-]{6,15})", text)
            if not phone_match:
                continue
            phone = re.sub(r"\D", "", phone_match.group(1))
            if len(phone) < 10:
                continue
            numbers.append(PhoneNumber(
                phone=phone,
                country="CN",
                carrier="",
                recent_count=0,
                last_active="",
                raw_label=text[:100],
            ))

        # Deduplicate
        seen: set[str] = set()
        unique: list[PhoneNumber] = []
        for n in numbers:
            if n.phone not in seen:
                seen.add(n.phone)
                unique.append(n)

        logger.info("Found %d +86 numbers from quackr.io", len(unique))
        return unique

    def get_messages(self, phone: str) -> list[SmsMessage]:
        # quackr.io uses a different URL scheme per number, often UUID-based
        # rather than phone-based, so we attempt to find the number page
        # via the listing page first.
        logger.warning("quackr.io message fetch not implemented (dynamic per-number UUID scheme)")
        return []


# ---------------------------------------------------------------------------
# ReceiveSmsFreeProvider  (36+ +86 numbers, normal phone segments)
# ---------------------------------------------------------------------------

class ReceiveSmsFreeProvider(SmsProvider):
    """Scrapes receivesms-free.com for +86 numbers and messages.

    * 36+ active Chinese numbers, normal segments (13x/15x/17x/18x)
    * Carrier info included (China Telecom/Mobile/Unicom)
    * Pure HTML, no JS, real-time message display
    """

    BASE_URL = "https://www.receivesms-free.com"
    COUNTRY_URL = "/cn/"

    @property
    def name(self) -> str:
        return "receivesms-free"

    def get_available_numbers(self) -> list[PhoneNumber]:
        url = f"{self.BASE_URL}{self.COUNTRY_URL}"
        logger.info("Fetching numbers from %s", url)
        try:
            with _build_client() as client:
                resp = client.get(url, timeout=30)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("receivesms-free.com fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        numbers: list[PhoneNumber] = []

        # Links look like: <a href="/cn/8617942231299/">+86 179 4223 1299 China Unicom</a>
        for link in soup.find_all("a", href=re.compile(r"/cn/\d{10,}/?")):
            text = link.get_text(separator=" ", strip=True)
            if not text:
                continue
            # Extract phone digits from href (more reliable than text).
            href = link.get("href", "")
            phone_digits = re.sub(r"\D", "", href.replace("/cn/", ""))
            if len(phone_digits) < 10:
                continue

            # Carrier from link text (e.g. "China Unicom", "China Mobile", "China Telecom").
            carrier = ""
            for abbr, full in [("CT", "China Telecom"), ("CM", "China Mobile"), ("CU", "China Unicom")]:
                if full.lower() in text.lower():
                    carrier = abbr
                    break

            numbers.append(PhoneNumber(
                phone=phone_digits,
                country="CN",
                carrier=carrier,
                recent_count=0,
                last_active="",
                raw_label=text[:100],
            ))

        # Deduplicate
        seen: set[str] = set()
        unique: list[PhoneNumber] = []
        for n in numbers:
            if n.phone not in seen:
                seen.add(n.phone)
                unique.append(n)

        logger.info("Found %d +86 numbers from receivesms-free.com", len(unique))
        return unique

    def get_messages(self, phone: str) -> list[SmsMessage]:
        """Fetch messages for a phone number.

        Page structure:
            <ul class="sms-list">
              <li class="sms-item">
                <div class="sms-top">{sender}{relative_time}</div>
                <div class="sms-text clamp-2">{content}</div>
              </li>
              ...
            </ul>
        """
        phone_clean = re.sub(r"^00|\+|^86", "", phone.strip())
        if not phone_clean.startswith("86"):
            phone_clean = "86" + phone_clean
        url = f"{self.BASE_URL}/cn/{phone_clean}/"
        logger.info("Fetching messages from %s", url)
        try:
            with _build_client() as client:
                resp = client.get(url, timeout=30)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("receivesms-free.com fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        messages: list[SmsMessage] = []

        # Each message is <li class="sms-item"> with .sms-top and .sms-text.
        for item in soup.select("li.sms-item, .sms-list li"):
            top_el = item.select_one(".sms-top")
            text_el = item.select_one(".sms-text")
            if not top_el or not text_el:
                continue
            top_text = top_el.get_text(separator=" ", strip=True)
            content = text_el.get_text(separator=" ", strip=True)
            if not content:
                continue

            # sms-top = "{sender}{relative_time}" e.g. "PAYSET 20 minutes ago"
            # Split on the trailing "N minutes/hours/days ago".
            sender = top_text
            timestamp = ""
            ts_match = re.search(
                r"(\d+\s*(?:minutes?|hours?|days?|weeks?|months?)\s*ago)$",
                top_text, re.IGNORECASE,
            )
            if ts_match:
                timestamp = ts_match.group(1).strip()
                sender = top_text[:ts_match.start()].strip()

            otp_match = _OTP_RE.search(content)
            messages.append(SmsMessage(
                sender=sender,
                content=content,
                timestamp=timestamp,
                is_otp=bool(otp_match),
                otp_code=otp_match.group(1) if otp_match else "",
            ))

        logger.info("Found %d messages for %s from receivesms-free.com", len(messages), phone)
        return messages


# ---------------------------------------------------------------------------
# SuperCloudSMSProvider  (fallback)
# ---------------------------------------------------------------------------

class SuperCloudSMSProvider(SmsProvider):
    """Scrapes supercloudsms.com for +86 numbers (fewer numbers, fallback)."""

    BASE_URL = "https://supercloudsms.com"

    @property
    def name(self) -> str:
        return "supercloudsms"

    def get_available_numbers(self) -> list[PhoneNumber]:
        url = f"{self.BASE_URL}/en/country/china/1.html"
        logger.info("Fetching numbers from %s", url)
        try:
            resp = _build_client().get(url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("SuperCloudSMS unavailable: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        numbers: list[PhoneNumber] = []

        # Adapt selectors based on observed page structure
        for block in soup.find_all("div", class_=lambda c: c and "number" in c):
            text = block.get_text(strip=True)
            phone_match = re.search(r"(\+?86[\d\s\-]{6,15})", text)
            if not phone_match:
                continue
            phone = re.sub(r"\D", "", phone_match.group(1))
            numbers.append(PhoneNumber(
                phone=phone,
                country="CN",
                carrier="",
                recent_count=0,
                last_active="",
                raw_label=text[:100],
            ))

        logger.info("Found %d +86 numbers from supercloudsms", len(numbers))
        return numbers

    def get_messages(self, phone: str) -> list[SmsMessage]:
        """SuperCloudSMS per-number page."""
        phone_clean = re.sub(r"^00|\+", "", phone.strip())
        url = f"{self.BASE_URL}/en/number/{phone_clean}.html"
        try:
            resp = _build_client().get(url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("SuperCloudSMS message fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        messages: list[SmsMessage] = []
        # Adapt per observed structure
        for msg_div in soup.find_all("div", class_=lambda c: c and ("msg" in c.lower() or "sms" in c.lower())):
            text = msg_div.get_text(strip=True)
            otp_match = _OTP_RE.search(text)
            messages.append(SmsMessage(
                sender="",
                content=text,
                timestamp="",
                is_otp=bool(otp_match),
                otp_code=otp_match.group(1) if otp_match else "",
                raw=text[:200],
            ))

        logger.info("Found %d messages from supercloudsms", len(messages))
        return messages


# ---------------------------------------------------------------------------
# Sms24MeProvider  (1,116 +86 numbers, mixed real + IoT segments)
# ---------------------------------------------------------------------------

class Sms24MeProvider(SmsProvider):
    """Scrapes sms24.me for +86 numbers and messages.

    * Largest free pool: 1,116 active Chinese numbers across 20 pages
    * Mix of real segments (13x/15x/17x/18x) and IoT (180/181)
    * Pure static HTML, no JS, no Cloudflare
    * Per-number URL: /en/numbers/{phone}, paginated
    """

    BASE_URL = "https://sms24.me"
    COUNTRY_URL = "/en/countries/cn"

    @property
    def name(self) -> str:
        return "sms24-me"

    def get_available_numbers(self) -> list[PhoneNumber]:
        """Fetch +86 numbers from the first page of sms24.me/cn.

        Page 1 lists the 15 newest numbers. Pagination available at
        /en/countries/cn/{N} but for our use case (need a fresh, clean number),
        the first page is sufficient.
        """
        url = f"{self.BASE_URL}{self.COUNTRY_URL}"
        logger.info("Fetching numbers from %s", url)
        try:
            with _build_client() as client:
                resp = client.get(url, timeout=30)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("sms24.me fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        numbers: list[PhoneNumber] = []

        # Number links: <a href="/en/numbers/8613197115843">+8613197115843</a>
        # Page also shows "X SMS received" near each link.
        for link in soup.find_all("a", href=re.compile(r"/en/numbers/86\d{9,12}")):
            href = link.get("href", "")
            phone_digits = re.sub(r"\D", "", href.replace("/en/numbers/", ""))
            if not phone_digits or not phone_digits.startswith("86"):
                continue
            # Skip extra-short or test numbers (e.g. 8612345678910 is a placeholder)
            if len(phone_digits) < 11:
                continue

            # Try to extract "X SMS received" from sibling text
            parent_text = link.parent.get_text(separator=" ", strip=True) if link.parent else ""
            sms_count = 0
            m = re.search(r"(\d+)\s*SMS\s*received", parent_text, re.IGNORECASE)
            if m:
                try:
                    sms_count = int(m.group(1))
                except ValueError:
                    pass

            # Carrier not shown on sms24.me listing page
            carrier = ""
            # Infer carrier from number prefix (3-digit segment)
            seg = phone_digits[2:5]  # e.g. "131"
            if seg.startswith(("133", "153", "180", "181", "189")):
                carrier = "CT"  # China Telecom
            elif seg.startswith(("134", "135", "136", "137", "138", "139",
                                  "150", "151", "152", "157", "158", "159",
                                  "182", "183", "184", "187", "188")):
                carrier = "CM"  # China Mobile
            elif seg.startswith(("130", "131", "132", "155", "156",
                                  "185", "186", "176")):
                carrier = "CU"  # China Unicom

            numbers.append(PhoneNumber(
                phone=phone_digits,
                country="CN",
                carrier=carrier,
                recent_count=sms_count,
                last_active="",
                raw_label=link.get_text(strip=True)[:100],
            ))

        # Deduplicate
        seen: set[str] = set()
        unique: list[PhoneNumber] = []
        for n in numbers:
            if n.phone not in seen:
                seen.add(n.phone)
                unique.append(n)

        logger.info("Found %d +86 numbers from sms24.me (page 1)", len(unique))
        return unique

    def get_messages(self, phone: str) -> list[SmsMessage]:
        """Fetch SMS messages for a given phone number from sms24.me.

        Page format (plain-text rendered, but parseable by anchor + sibling):
            [From: SENDER](link)RELATIVE_TIME

            CONTENT_TEXT

            [From: SENDER2](link)RELATIVE_TIME2

            CONTENT_TEXT2

        We use BeautifulSoup to find "From:" anchors, then walk forward
        in DOM order to grab the content that follows each anchor.
        """
        phone_clean = re.sub(r"^00|\+", "", phone.strip())
        if not phone_clean.startswith("86"):
            phone_clean = "86" + phone_clean
        url = f"{self.BASE_URL}/en/numbers/{phone_clean}"
        logger.info("Fetching messages from %s", url)
        try:
            with _build_client() as client:
                resp = client.get(url, timeout=30)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("sms24.me fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        messages: list[SmsMessage] = []

        # Find all anchors that link to /en/messages/{sender_slug}
        from_anchors = soup.find_all("a", href=re.compile(r"/en/messages/"))

        for anchor in from_anchors:
            # Sender text is "[From: SENDER_NAME]" — strip prefix
            sender_text = anchor.get_text(strip=True)
            sender = re.sub(r"^\[?From:?\s*", "", sender_text, flags=re.IGNORECASE).rstrip("]").strip()

            # The anchor's parent contains the time text after it
            parent = anchor.parent
            if not parent:
                continue
            # Time text = the trailing text after the anchor inside the parent
            # Use .find(string=True, recursive=False) — but simpler: get the
            # whole parent text minus the anchor text.
            parent_text = parent.get_text(separator=" ", strip=True)
            # Strip the sender label, remainder is relative time like "36 minutes ago"
            time_str = parent_text.replace(sender_text, "", 1).strip()

            # Content = the next sibling block of text (could be a <p> or
            # plain text node after the parent)
            content = ""
            # Walk up to the next text-bearing block
            current = parent
            for _ in range(5):
                nxt = current.find_next_sibling()
                if nxt is None:
                    break
                # Get text content if it's a real block
                t = nxt.get_text(separator=" ", strip=True) if hasattr(nxt, "get_text") else ""
                if t:
                    content = t
                    break
                current = nxt

            if not content:
                # Fallback: look at the parent's next sibling's children
                nxt = parent.find_next_sibling() if parent else None
                if nxt:
                    content = nxt.get_text(separator=" ", strip=True)

            if not content:
                continue

            otp_match = _OTP_RE.search(content)
            messages.append(SmsMessage(
                sender=sender,
                content=content,
                timestamp=time_str,
                is_otp=bool(otp_match),
                otp_code=otp_match.group(1) if otp_match else "",
            ))

        # Deduplicate by (sender, content, timestamp)
        seen_keys: set[tuple[str, str, str]] = set()
        unique_msgs: list[SmsMessage] = []
        for m in messages:
            key = (m.sender, m.content, m.timestamp)
            if key not in seen_keys:
                seen_keys.add(key)
                unique_msgs.append(m)

        logger.info("Found %d messages for %s from sms24.me", len(unique_msgs), phone)
        return unique_msgs


# ---------------------------------------------------------------------------
# GoinSmsProvider  (99 +86 numbers, all real segments)
# ---------------------------------------------------------------------------

class GoinSmsProvider(SmsProvider):
    """Scrapes goinsms.xyz for +86 numbers and messages.

    * 9 numbers per page × 11 pages = ~99 numbers
    * All numbers are real segments (135/136/138/152/154/159/182/183/188)
    * Per-number URL: /sms.php?p={phone_without_86}
    * Table format: From | Messages | Time
    """

    BASE_URL = "https://www.goinsms.xyz"
    COUNTRY_URL = "/cn.php"

    @property
    def name(self) -> str:
        return "goinsms"

    def get_available_numbers(self) -> list[PhoneNumber]:
        url = f"{self.BASE_URL}{self.COUNTRY_URL}"
        logger.info("Fetching numbers from %s", url)
        try:
            with _build_client() as client:
                resp = client.get(url, timeout=30)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("goinsms.xyz fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        numbers: list[PhoneNumber] = []

        # Numbers appear as: +8615012863450 (in plain text, often inside <h3> or <p>)
        # Find all elements containing "+86" followed by 11 digits
        for el in soup.find_all(["a", "h3", "p", "span", "div"]):
            text = el.get_text(strip=True)
            if not text:
                continue
            # Match +86XXXXXXXXXXX (full international format)
            m = re.search(r"\+?86(1[3-9]\d{9})", text)
            if not m:
                continue
            local_phone = m.group(1)  # 11-digit CN mobile, no 86 prefix
            phone_full = "86" + local_phone

            # Carrier inference (3-digit segment)
            seg = local_phone[:3]
            if seg in ("133", "153", "180", "181", "189", "199"):
                carrier = "CT"
            elif seg in ("134", "135", "136", "137", "138", "139",
                         "150", "151", "152", "157", "158", "159",
                         "178", "182", "183", "184", "187", "188", "198"):
                carrier = "CM"
            elif seg in ("130", "131", "132", "155", "156", "166",
                         "175", "176", "185", "186"):
                carrier = "CU"
            else:
                carrier = ""

            numbers.append(PhoneNumber(
                phone=phone_full,
                country="CN",
                carrier=carrier,
                recent_count=0,
                last_active="",
                raw_label=text[:100],
            ))

        # Deduplicate
        seen: set[str] = set()
        unique: list[PhoneNumber] = []
        for n in numbers:
            if n.phone not in seen:
                seen.add(n.phone)
                unique.append(n)

        logger.info("Found %d +86 numbers from goinsms.xyz (page 1)", len(unique))
        return unique

    def get_messages(self, phone: str) -> list[SmsMessage]:
        """Fetch SMS messages for a given phone number from goinsms.xyz.

        Page structure is a simple table:
            <table>
              <tr>
                <td>{sender}</td>
                <td>{content}</td>
                <td>{timestamp}</td>
              </tr>
            </table>
        """
        # goinsms.xyz uses local 11-digit phone (without 86 prefix) in URL
        phone_local = re.sub(r"^00|\+|^86", "", phone.strip())
        if phone_local.startswith("86"):
            phone_local = phone_local[2:]
        if not phone_local.isdigit() or len(phone_local) != 11:
            logger.warning("Invalid phone format for goinsms.xyz: %s", phone)
            return []

        url = f"{self.BASE_URL}/sms.php?p={phone_local}"
        logger.info("Fetching messages from %s", url)
        try:
            with _build_client() as client:
                resp = client.get(url, timeout=30)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("goinsms.xyz fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        messages: list[SmsMessage] = []

        # Find the message table — goinsms.xyz renders From | Messages | Time
        # in a simple 3-column table. Locate the first table with rows.
        msg_table = None
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) >= 2:
                # Check header row
                header_cells = rows[0].find_all(["th", "td"])
                header_text = " ".join(c.get_text(strip=True).lower() for c in header_cells)
                if "from" in header_text and ("message" in header_text or "time" in header_text):
                    msg_table = table
                    break

        # If no proper table found, fall back to first table with rows
        if msg_table is None:
            for table in soup.find_all("table"):
                if len(table.find_all("tr")) >= 2:
                    msg_table = table
                    break

        if msg_table is not None:
            rows = msg_table.find_all("tr")
            for row in rows[1:]:  # skip header
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                sender = cells[0].get_text(strip=True)
                content = cells[1].get_text(strip=True)
                timestamp = cells[2].get_text(strip=True)

                if not content:
                    continue
                otp_match = _OTP_RE.search(content)
                messages.append(SmsMessage(
                    sender=sender,
                    content=content,
                    timestamp=timestamp,
                    is_otp=bool(otp_match),
                    otp_code=otp_match.group(1) if otp_match else "",
                ))

        # Fallback: parse the page text for "From / Messages / Time" blocks
        if not messages:
            # The page has 3-column rows rendered as plain text. Try parsing.
            all_text = soup.get_text(separator="\n")
            lines = [l.strip() for l in all_text.split("\n") if l.strip()]

            # Look for timestamp patterns like "2026-07-22 23:43:44"
            i = 0
            while i < len(lines) - 2:
                # Heuristic: if line[i+2] is a timestamp, then line[i] is sender
                # and line[i+1] is content
                if re.match(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}$", lines[i + 2] if i + 2 < len(lines) else ""):
                    sender = lines[i]
                    content = lines[i + 1]
                    timestamp = lines[i + 2]
                    otp_match = _OTP_RE.search(content)
                    messages.append(SmsMessage(
                        sender=sender,
                        content=content,
                        timestamp=timestamp,
                        is_otp=bool(otp_match),
                        otp_code=otp_match.group(1) if otp_match else "",
                    ))
                    i += 3
                    continue
                i += 1

        logger.info("Found %d messages for %s from goinsms.xyz", len(messages), phone)
        return messages


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_sms_client() -> SmsProvider:
    """Create the best available SMS provider.

    Tries providers in order of likelihood to bypass Trae's number blocking.
    Real-segment providers (13x/15x/17x/18x) come first; IoT-segment
    providers (196/197/181) come last.

      1. GoinSmsProvider           (~99 numbers, all real segments 13x/15x/17x/18x)
      2. Sms24MeProvider           (1,116 numbers, mixed real + IoT)
      3. ReceiveSmsFreeProvider    (36+ numbers, real segments 13x/15x/17x/18x)
      4. ReceiveSmsIoProvider      (9 numbers, segments 130/132)
      5. TextVerificationProvider  (44 numbers, IoT segments 196/197/181 — often blocked)
      6. QuackrProvider            (large source, but China often empty)
      7. SuperCloudSMSProvider     (fewer numbers, least reliable)

    Note: Text-verification.net's IoT segments (196/197/181) are blocked by
    Trae. Normal segments (13x/15x/17x/18x) from goinsms.xyz, sms24.me,
    receivesms-free.com and receive-sms.io have a much better chance of
    receiving Trae OTPs.
    """
    providers: list[tuple[str, SmsProvider]] = [
        ("GoinSmsProvider", GoinSmsProvider()),
        ("Sms24MeProvider", Sms24MeProvider()),
        ("ReceiveSmsFreeProvider", ReceiveSmsFreeProvider()),
        ("ReceiveSmsIoProvider", ReceiveSmsIoProvider()),
        ("TextVerificationProvider", TextVerificationProvider()),
        ("QuackrProvider", QuackrProvider()),
        ("SuperCloudSMSProvider", SuperCloudSMSProvider()),
    ]

    last_error = ""
    for name, provider in providers:
        try:
            nums = provider.get_available_numbers()
            if nums:
                logger.info("Using %s (%d +86 numbers available)", name, len(nums))
                return provider
            logger.info("%s returned 0 numbers, trying next provider", name)
        except Exception as exc:
            last_error = str(exc)
            logger.warning("%s failed: %s", name, exc)

    logger.error("All SMS providers exhausted! Last error: %s", last_error)
    raise RuntimeError(f"No SMS provider available — last error: {last_error}")
