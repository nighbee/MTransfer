"""Normalization helpers for track title/artist matching."""
from __future__ import annotations

import re
import unicodedata

_FEAT_RE = re.compile(r"\s*[\(\[]?\b(?:feat\.?|ft\.?|featuring)\b[^\)\]]*[\)\]]?", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def norm(s: str, yo_to_e: bool = False) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.strip().lower()
    if yo_to_e:
        s = s.replace("ё", "е")
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def strip_feat(s: str) -> tuple:
    """Return (clean_title, feat_part)."""
    m = _FEAT_RE.search(s or "")
    if not m:
        return (s or "").strip(), ""
    return ((s[: m.start()] + s[m.end():]).strip(), m.group(0).strip())
