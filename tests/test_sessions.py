"""Tests for the per-visitor session store (no network, no tokens)."""
from __future__ import annotations

from src.sessions import SESSION_COOKIE, SessionStore


def test_get_or_create_issues_stable_sid(tmp_path):
    store = SessionStore(tmp_path)
    s1 = store.get_or_create(None)
    assert s1.sid
    s2 = store.get_or_create(s1.sid)
    assert s2.sid == s1.sid
    assert s1.data_dir.exists()


def test_sessions_isolated_tokens_and_dirs(tmp_path):
    store = SessionStore(tmp_path)
    a = store.get_or_create(None)
    b = store.get_or_create(None)
    assert a.sid != b.sid
    a.tokens["yandex"] = "AAA"
    assert "yandex" not in b.tokens
    assert a.data_dir != b.data_dir


def test_cleanup_removes_idle(tmp_path):
    store = SessionStore(tmp_path, ttl_seconds=0)
    s = store.get_or_create(None)
    import time as _t
    _t.sleep(0.02)
    assert store.cleanup() >= 1
    assert store.get(s.sid) is None


def test_count(tmp_path):
    store = SessionStore(tmp_path)
    assert store.count() == 0
    store.get_or_create(None)
    assert store.count() == 1


def test_cookie_name():
    assert SESSION_COOKIE == "mtsession"
