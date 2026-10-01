"""Visible page text across every frame, whitespace collapsed. Shared by discovery (to prove a success
phrase is really on screen) and replay (to check it), so both read the page the same way."""

import re


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def page_text(page) -> str:
    parts = []
    for frame in page.frames:
        try:
            parts.append(frame.inner_text("body", timeout=1500))
        except Exception:
            continue  # a frame that is gone or not ready just contributes nothing
    return collapse(" ".join(parts))


def text_present(page, phrase: str) -> bool:
    return collapse(phrase) in page_text(page)
