"""Per-visitor session store for the SaaS pivot.

Each visitor gets an isolated session (cookie ``mtsession``):
- own in-memory token dict (Yandex / Apple tokens, device codes)
- own scoped data directory ``data/sessions/<sid>/`` for exports/matches
- last-seen timestamp for TTL cleanup (default 24h)

The legacy global ``TOKENS`` dict is kept as a fallback alias so old tests
and single-user flows keep working: when no session id is provided, the
global store is used.

Thread-safe. No tokens ever touch disk.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


SESSION_COOKIE = "mtsession"
SESSION_TTL_SECONDS = 24 * 3600


@dataclass
class Session:
    sid: str
    tokens: dict = field(default_factory=dict)
    jobs: dict = field(default_factory=dict)
    data_dir: Path | None = None
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class SessionStore:
    def __init__(self, base_data_dir: Path,
                 ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self._base = Path(base_data_dir)
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def _dir_for(self, sid: str) -> Path:
        d = self._base / "sessions" / sid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_or_create(self, sid: str | None) -> Session:
        """Return existing session or create a new one (new sid if needed)."""
        with self._lock:
            if sid and sid in self._sessions:
                s = self._sessions[sid]
                s.last_seen = time.time()
                return s
            new_sid = sid if sid else uuid.uuid4().hex[:16]
            while new_sid in self._sessions:
                new_sid = uuid.uuid4().hex[:16]
            s = Session(sid=new_sid)
            s.data_dir = self._dir_for(new_sid)
            self._sessions[new_sid] = s
            return s

    def get(self, sid: str | None) -> Session | None:
        with self._lock:
            s = self._sessions.get(sid or "")
            if s is not None:
                s.last_seen = time.time()
            return s

    def cleanup(self) -> int:
        """Drop sessions idle longer than TTL. Returns count removed."""
        now = time.time()
        with self._lock:
            stale = [sid for sid, s in self._sessions.items()
                     if now - s.last_seen > self._ttl]
            for sid in stale:
                del self._sessions[sid]
        return len(stale)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)
