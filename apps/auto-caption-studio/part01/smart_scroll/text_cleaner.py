"""Text cleaning helpers for Auto Caption Video Studio."""
from __future__ import annotations

import re


def clean_text(text: str, *, single_paragraph: bool = False) -> str:
    """Clean pasted narration text.

    Rules:
    - Normalize tabs/non-breaking spaces.
    - Collapse repeated spaces.
    - Treat blank lines as paragraph breaks unless single_paragraph=True.
    - Merge broken single line breaks within a paragraph.
    - Fix common punctuation spacing.
    """
    if not text:
        return ""

    text = text.replace("\ufeff", "")
    text = text.replace("\u00a0", " ")
    text = text.replace("\t", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]

    paragraphs: list[str] = []
    current: list[str] = []

    for line in lines:
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line)

    if current:
        paragraphs.append(" ".join(current))

    cleaned_paragraphs: list[str] = []
    for para in paragraphs:
        para = re.sub(r" {2,}", " ", para).strip()
        para = re.sub(r"\s+([,.;:!?%])", r"\1", para)
        para = re.sub(r"([,.;:!?])(?=\S)", r"\1 ", para)
        para = re.sub(r"\(\s+", "(", para)
        para = re.sub(r"\s+\)", ")", para)
        if para:
            cleaned_paragraphs.append(para)

    if single_paragraph:
        return " ".join(cleaned_paragraphs).strip()
    return "\n\n".join(cleaned_paragraphs).strip()


def to_single_paragraph(text: str) -> str:
    """Clean and force all text into one continuous paragraph."""
    return clean_text(text, single_paragraph=True)


def split_paragraphs(text: str) -> list[str]:
    """Split text into non-empty paragraphs."""
    return [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
