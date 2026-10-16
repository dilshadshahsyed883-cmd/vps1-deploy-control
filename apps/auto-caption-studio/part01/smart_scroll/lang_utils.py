"""Lightweight language/direction helpers."""
from __future__ import annotations


def looks_rtl(text: str) -> bool:
    """Detect Arabic/Urdu-style RTL characters."""
    rtl_count = 0
    ltr_count = 0
    for ch in text[:2000]:
        code = ord(ch)
        if 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0x08A0 <= code <= 0x08FF:
            rtl_count += 1
        elif ("A" <= ch <= "Z") or ("a" <= ch <= "z"):
            ltr_count += 1
    return rtl_count > max(3, ltr_count)
