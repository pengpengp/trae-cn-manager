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
        """Poll *get_messages* until an OTP code appears.

        Returns the first 6-digit OTP found, or *None* on timeout.
        """
        deadline = time.time() + timeout
        known: set[str] = set()
        while time.time() < deadline:
            msgs = self.get_messages(phone)
            for m in msgs:
                if m.is_otp and m.otp_code and m.content not in known:
                    logger.info("OTP found: %s  (sender=%s)", m.otp_code, m.sender)
                    return m.otp_code
                known.add(m.content)
            remaining = int(deadline - time.time())
            logger.debug("No OTP yet, sleeping %ds (%ds left)", poll_interval, remaining)
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
        # Inside a container with country-list or similar
        for link in soup.find_all("a", href=re.compile(r"/temporary-numbers/china/\d{10,}/")):
            text = link.get_text(strip=True)
            if not text:
                continue
            phone_digits = re.sub(r"\D", "", text)
            if not phone_digits or len(phone_digits) < 10:
                continue

            # Check if the parent block says "Active"
            parent_text = link.parent.get_text() if link.parent else ""
            is_active = "active" in parent_text.lower()

            # Extract "Added: X ago" from surrounding text
            added_match = re.search(r"Added:\s*(.+?)(?:<|$)", str(link.parent.parent) if link.parent and link.parent.parent else "")
            added_str = ""
            if added_match:
                added_str = added_match.group(1).strip()
            else:
                page_text = parent_text
                m = re.search(r"Added:\s*(.+?)(?:\||$)", page_text)
                if m:
                    added_str = m.group(1).strip()

            if is_active:
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
# Factory
# ---------------------------------------------------------------------------

def create_sms_client() -> SmsProvider:
    """Create the best available SMS provider.

    Tries providers in order of reliability/number count:
      1. TextVerificationProvider  (44 numbers, most reliable)
      2. ReceiveSmsIoProvider      (9+ active numbers, good freshness)
      3. QuackrProvider            (large source, but China often empty)
      4. SuperCloudSMSProvider     (fewer numbers, least reliable)
    """
    providers: list[tuple[str, SmsProvider]] = [
        ("TextVerificationProvider", TextVerificationProvider()),
        ("ReceiveSmsIoProvider", ReceiveSmsIoProvider()),
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
