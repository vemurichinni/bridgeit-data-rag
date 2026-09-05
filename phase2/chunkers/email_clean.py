"""
email_clean.py — turn a raw email body into the text that is worth indexing.

Strips quoted history ("On ... wrote:", "-----Original Message-----", "> " lines,
Outlook "From:/Sent:/To:" headers in the body), signatures ("-- ", "Regards," blocks,
mobile footers), legal disclaimers, and collapses whitespace. HTML-only messages are
converted to text first.

Each message keeps only what its author typed, so a 40-message thread is indexed
once, not 40 times with growing quoted tails.
"""
from __future__ import annotations

import html
import re

QUOTE_HEADERS = [
    re.compile(r"^\s*On .{3,200}?wrote:\s*$", re.M | re.S),
    re.compile(r"^\s*-{2,}\s*(Original Message|Forwarded message|Original Appointment)\s*-{2,}\s*$", re.M | re.I),
    re.compile(r"^\s*_{5,}\s*$", re.M),                                   # Outlook separator line
    re.compile(r"^\s*From:\s.+\n(\s*Sent:|\s*Date:)\s.+\n\s*To:\s.+", re.M),  # Outlook reply header in body
    re.compile(r"^\s*Le .{3,120} a écrit\s*:\s*$", re.M),
    re.compile(r"^\s*Am .{3,120} schrieb .{1,80}:\s*$", re.M),
    re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}.{0,40}<.+@.+>\s*:?\s*$", re.M),
]
SIGNATURE_STARTS = [
    re.compile(r"^--\s*$", re.M),
    re.compile(r"^\s*(Thanks|Thank you|Regards|Best regards|Kind regards|Warm regards|Cheers|Sincerely|Best|Thanks & Regards|Thanks and Regards)[,.!]?\s*$", re.M | re.I),
    re.compile(r"^\s*Sent from my (iPhone|iPad|Android|Galaxy|BlackBerry|Windows Phone|Samsung).*$", re.M | re.I),
    re.compile(r"^\s*Get Outlook for (iOS|Android)\s*$", re.M | re.I),
]
DISCLAIMER = re.compile(
    r"^\s*(This (e-?mail|message|communication)[^\n]{0,80}(confidential|intended|privileged)|"
    r"CONFIDENTIALITY NOTICE|DISCLAIMER:|The information (contained|transmitted) in this)", re.M | re.I)
TAG = re.compile(r"<[^>]+>")
SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
BR = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d)\s*/?>", re.I)


def html_to_text(s: str) -> str:
    s = SCRIPT_STYLE.sub("", s)
    s = BR.sub("\n", s)
    s = TAG.sub("", s)
    s = html.unescape(s)
    return s


def strip_quotes(text: str) -> tuple[str, int]:
    """Return (own_text, quoted_chars_removed)."""
    cut = len(text)
    for rx in QUOTE_HEADERS:
        m = rx.search(text)
        if m and m.start() < cut:
            cut = m.start()
    own = text[:cut]
    # remove any remaining '>' quoted lines, and blank lines they leave behind
    lines = [l for l in own.splitlines() if not l.lstrip().startswith(">")]
    own = "\n".join(lines)
    return own, len(text) - len(own)


def strip_signature(text: str) -> str:
    cut = len(text)
    # only consider a signature marker in the last 60% of the message, so a "Thanks," on line 1 survives
    floor = int(len(text) * 0.4)
    for rx in SIGNATURE_STARTS:
        for m in rx.finditer(text):
            if m.start() >= floor and m.start() < cut:
                cut = m.start()
                break
    m = DISCLAIMER.search(text)
    if m and m.start() < cut:
        cut = m.start()
    return text[:cut]


def clean_body(text: str, is_html: bool = False) -> dict:
    if is_html:
        text = html_to_text(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    own, quoted = strip_quotes(text)
    own = strip_signature(own)
    own = re.sub(r"[ \t]+\n", "\n", own)
    own = re.sub(r"\n{3,}", "\n\n", own).strip()
    return {"text": own, "quoted_chars": quoted, "original_chars": len(text)}
