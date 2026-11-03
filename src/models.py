"""Canonical data models: Track, MatchResult, Job."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class Track:
    source: str = "yandex"
    yandex_id: str = ""
    title: str = ""
    artists: list = field(default_factory=list)
    album: str = ""
    duration_ms: int = 0
    isrc: str = ""
    cover_url: str = ""


@dataclass
class MatchResult:
    yandex_id: str = ""
    apple_id: str = ""
    confidence: float = 0.0
    method: str = ""
    candidates: list = field(default_factory=list)


@dataclass
class Job:
    job_id: str = ""
    stage: str = ""
    total: int = 0
    done: int = 0
    failed: list = field(default_factory=list)
    created_at: str = ""


def to_json(obj) -> str:
    return json.dumps(asdict(obj), ensure_ascii=False)


def track_from_dict(d: dict) -> Track:
    return Track(
        source=d.get("source", "yandex"),
        yandex_id=str(d.get("yandex_id", "")),
        title=d.get("title", ""),
        artists=list(d.get("artists", [])),
        album=d.get("album", ""),
        duration_ms=int(d.get("duration_ms", 0) or 0),
        isrc=d.get("isrc", ""),
        cover_url=d.get("cover_url", ""),
    )
