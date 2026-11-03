"""Tests for the Apple catalog matcher stage.

Covers scoring/thresholds/match_track/run() in src.match_apple plus
throttling/retries/redacted logging in src.http_utils.

All HTTP is mocked (requests.get patched, time.sleep neutralised) --
no real network traffic.
"""
from __future__ import annotations

import json
import logging
import time as _time
from unittest import mock

import pytest
import requests

from src import http_utils
from src.match_apple import (
    main,
    match_track,
    run,
    score_candidate,
    search_isrc,
    search_term,
)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------
class FakeResp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _song(track_id, name, artist, millis, collection="Album"):
    return {
        "trackId": track_id,
        "trackName": name,
        "artistName": artist,
        "collectionName": collection,
        "trackTimeMillis": millis,
    }


@pytest.fixture
def no_sleep():
    """Neutralise sleeping so throttling/backoff cost nothing in tests."""
    with mock.patch("time.sleep", return_value=None):
        yield


@pytest.fixture
def track():
    return {
        "source": "yandex",
        "yandex_id": "7",
        "title": "Shape of You",
        "artists": ["Ed Sheeran"],
        "album": "Divide",
        "duration_ms": 263000,
        "isrc": "",
        "cover_url": "",
    }


def _write_input(tmp_path, tracks):
    p = tmp_path / "favorites.json"
    p.write_text(json.dumps(tracks, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# score_candidate
# ---------------------------------------------------------------------------
def test_score_identical_text_duration_high(track):
    cand = _song(1, "Shape of You", "Ed Sheeran", 264000)  # 1s apart
    assert score_candidate(track, cand) >= 0.85


def test_score_within_3s_boundary(track):
    cand = _song(1, "Shape of You", "Ed Sheeran", track["duration_ms"] + 3000)
    assert score_candidate(track, cand) >= 0.85


def test_score_far_duration_scores_lower(track):
    close = _song(1, "Shape of You", "Ed Sheeran", track["duration_ms"] + 1000)
    far = _song(2, "Shape of You", "Ed Sheeran", track["duration_ms"] + 60000)
    assert score_candidate(track, far) < score_candidate(track, close)


def test_score_low_for_different_track(track):
    cand = _song(9, "Completely Different Title Here", "Another Artist Entirely", 999000)
    assert score_candidate(track, cand) < 0.6


def test_score_handles_missing_duration(track):
    cand = dict(_song(1, "Shape of You", "Ed Sheeran", 0))
    assert score_candidate(track, cand) >= 0.85


# ---------------------------------------------------------------------------
# search_* (requests.get mocked)
# ---------------------------------------------------------------------------
def test_search_term_parses_results(no_sleep):
    payload = {"resultCount": 1, "results": [_song(42, "Hello", "Adele", 295000)]}
    with mock.patch("requests.get", return_value=FakeResp(payload)):
        res = search_term("Adele", "Hello")
    assert len(res) == 1
    assert res[0]["trackId"] == 42


def test_search_term_empty_guard(no_sleep):
    with mock.patch("requests.get") as getter:
        assert search_term("", "") == []
        getter.assert_not_called()


def test_search_isrc_parses_results(no_sleep):
    payload = {"resultCount": 1, "results": [_song(7, "Song", "Artist", 200000)]}
    with mock.patch("requests.get", return_value=FakeResp(payload)):
        res = search_isrc("USABC1234567")
    assert res and res[0]["trackId"] == 7


def test_search_isrc_empty_guard(no_sleep):
    with mock.patch("requests.get") as getter:
        assert search_isrc("") == []
        getter.assert_not_called()


# ---------------------------------------------------------------------------
# match_track
# ---------------------------------------------------------------------------
def test_match_isrc_exact_confidence(no_sleep):
    track = {
        "yandex_id": "1",
        "title": "Anything At All",
        "artists": ["Anyone"],
        "duration_ms": 200000,
        "isrc": "USRC17607839",
    }
    # Even a textually different catalog entry is accepted via ISRC.
    payload = {"resultCount": 1, "results": [_song(555, "Different Title", "Other Artist", 999999)]}
    with mock.patch("requests.get", return_value=FakeResp(payload)):
        res = match_track(track)
    assert res.confidence == 1.0
    assert res.method == "isrc"
    assert str(res.apple_id) == "555"


def test_match_text_duration_high(no_sleep, track):
    payload = {
        "resultCount": 1,
        "results": [_song(101, "Shape of You", "Ed Sheeran", track["duration_ms"] + 1000)],
    }
    with mock.patch("requests.get", return_value=FakeResp(payload)):
        res = match_track(track)
    assert res.apple_id != ""
    assert res.confidence >= 0.85
    assert res.method == "text+duration"


def test_match_review_bucket_still_matched(no_sleep, track):
    payload = {
        "resultCount": 1,
        "results": [_song(202, "Shape of You", "Ed Sheeran", track["duration_ms"] + 1000)],
    }
    with mock.patch("requests.get", return_value=FakeResp(payload)), mock.patch(
        "src.match_apple.score_candidate", return_value=0.7
    ):
        res = match_track(track)
    assert res.apple_id != ""
    assert res.method == "text+duration"
    assert res.confidence == pytest.approx(0.7)


def test_match_propagates_network_errors(no_sleep, track):
    with mock.patch("requests.get", side_effect=requests.ConnectionError("down")):
        with pytest.raises(requests.RequestException):
            match_track(track)


# ---------------------------------------------------------------------------
# run(): outputs, reasons, resume, limit, CLI
# ---------------------------------------------------------------------------
def test_run_low_score_unmatched_with_reason(no_sleep, tmp_path, track):
    bad = _song(9, "Unrelated Song Title ZZZ", "Unknown Band QQQ", 999000)
    payload = {"resultCount": 1, "results": [bad]}
    inp = _write_input(tmp_path, [track])
    with mock.patch("requests.get", return_value=FakeResp(payload)):
        summary = run(
            inp,
            str(tmp_path / "matched.json"),
            str(tmp_path / "unmatched.json"),
            str(tmp_path / "report.txt"),
            resume_job=str(tmp_path / "job.json"),
        )
    unmatched = _read_json(tmp_path / "unmatched.json")
    assert summary["matched"] == 0
    assert summary["unmatched"] == 1
    assert len(unmatched) == 1
    assert unmatched[0]["reason"] == "low_score"


def test_run_no_candidates_reason(no_sleep, tmp_path, track):
    inp = _write_input(tmp_path, [track])
    with mock.patch("requests.get", return_value=FakeResp({"resultCount": 0, "results": []})):
        run(
            inp,
            str(tmp_path / "matched.json"),
            str(tmp_path / "unmatched.json"),
            str(tmp_path / "report.txt"),
            resume_job=str(tmp_path / "job.json"),
        )
    unmatched = _read_json(tmp_path / "unmatched.json")
    assert len(unmatched) == 1
    assert unmatched[0]["reason"] == "no_candidates"


def _router(url, params=None, **kwargs):
    params = params or {}
    if "lookup" in url:
        if params.get("isrc") == "USAAA0000001":
            return FakeResp({"resultCount": 1, "results": [_song(111, "Song One", "Artist One", 200000)]})
        return FakeResp({"resultCount": 0, "results": []})
    term = str(params.get("term", ""))
    if "Shape of You" in term:
        return FakeResp({"resultCount": 1, "results": [_song(222, "Shape of You", "Ed Sheeran", 263500)]})
    return FakeResp({"resultCount": 0, "results": []})


def test_run_every_track_lands_matched_or_unmatched(no_sleep, tmp_path):
    tracks = [
        {"yandex_id": "y1", "title": "Song One", "artists": ["Artist One"],
         "duration_ms": 200000, "isrc": "USAAA0000001"},
        {"yandex_id": "y2", "title": "Shape of You", "artists": ["Ed Sheeran"],
         "duration_ms": 263000, "isrc": ""},
        {"yandex_id": "y3", "title": "Nonexistent Song ZZZ", "artists": ["Nobody QQQ"],
         "duration_ms": 123456, "isrc": ""},
    ]
    inp = _write_input(tmp_path, tracks)
    with mock.patch("requests.get", side_effect=_router):
        summary = run(
            inp,
            str(tmp_path / "matched.json"),
            str(tmp_path / "unmatched.json"),
            str(tmp_path / "report.txt"),
            resume_job=str(tmp_path / "job.json"),
        )
    matched = _read_json(tmp_path / "matched.json")
    unmatched = _read_json(tmp_path / "unmatched.json")
    report = (tmp_path / "report.txt").read_text(encoding="utf-8")

    assert summary == {"total": 3, "matched": 2, "unmatched": 1,
                       "report": str(tmp_path / "report.txt")}
    assert len(matched) + len(unmatched) == 3  # FR2: nothing silently dropped
    assert {m["yandex_id"] for m in matched} == {"y1", "y2"}
    assert {m["method"] for m in matched} == {"isrc", "text+duration"}
    by_id = {m["yandex_id"]: m for m in matched}
    assert by_id["y1"]["confidence"] == 1.0
    assert by_id["y2"]["confidence"] >= 0.85
    assert len(unmatched) == 1 and unmatched[0]["yandex_id"] == "y3"
    assert unmatched[0]["reason"] == "no_candidates"
    assert "total: 3" in report
    assert "matched: 2" in report
    assert "unmatched: 1" in report
    assert "auto (>=0.85)" in report


def test_run_resumes_from_job_file(no_sleep, tmp_path):
    base = {"title": "Same Song", "artists": ["Same Artist"], "duration_ms": 200000, "isrc": ""}
    tracks = [dict(base, yandex_id=yid) for yid in ("a", "b", "c")]
    inp = _write_input(tmp_path, tracks)
    matched_p = tmp_path / "matched.json"
    unmatched_p = tmp_path / "unmatched.json"
    job_p = tmp_path / "job.json"
    matched_p.write_text(json.dumps([{
        "yandex_id": "a", "apple_id": "11", "confidence": 1.0,
        "method": "text+duration", "candidates": [],
    }]), encoding="utf-8")
    unmatched_p.write_text("[]", encoding="utf-8")
    job_p.write_text(json.dumps({
        "job_id": "job1", "stage": "match", "total": 3, "done": 1,
        "failed": [], "created_at": "2026-01-01T00:00:00",
    }), encoding="utf-8")

    payload = {"resultCount": 1, "results": [_song(11, "Same Song", "Same Artist", 200500)]}
    with mock.patch("requests.get", return_value=FakeResp(payload)):
        summary = run(str(inp), str(matched_p), str(unmatched_p),
                      str(tmp_path / "report.txt"), resume_job=str(job_p))
    matched = _read_json(matched_p)
    assert summary["total"] == 3
    assert [m["yandex_id"] for m in matched] == ["a", "b", "c"]  # no duplicates
    assert _read_json(unmatched_p) == []
    assert _read_json(job_p)["done"] == 3


def test_run_limit_and_cli(no_sleep, tmp_path):
    tracks = [{
        "yandex_id": str(i), "title": "Same Song", "artists": ["Same Artist"],
        "duration_ms": 200000, "isrc": "",
    } for i in range(5)]
    inp = _write_input(tmp_path, tracks)
    payload = {"resultCount": 1, "results": [_song(11, "Same Song", "Same Artist", 200500)]}
    argv = ["--input", str(inp),
            "--matched", str(tmp_path / "matched.json"),
            "--unmatched", str(tmp_path / "unmatched.json"),
            "--report", str(tmp_path / "report.txt"),
            "--job", str(tmp_path / "job.json"),
            "--limit", "2"]
    with mock.patch("requests.get", return_value=FakeResp(payload)):
        assert main(argv) == 0
    assert len(_read_json(tmp_path / "matched.json")) == 2


# ---------------------------------------------------------------------------
# http_utils.throttled_get
# ---------------------------------------------------------------------------
def test_throttled_get_retries_429_then_succeeds():
    seq = [FakeResp({}, 429, {"Retry-After": "0"}), FakeResp({}, 500), FakeResp({"ok": 1}, 200)]
    with mock.patch("requests.get", side_effect=seq) as getter, mock.patch("time.sleep") as sleeper:
        resp = http_utils.throttled_get("https://example.com/x", params={"q": "1"}, delay=0)
    assert resp.status_code == 200
    assert getter.call_count == 3
    assert sleeper.call_count >= 2  # backoff between attempts


def test_throttled_get_retries_connection_errors():
    with mock.patch("requests.get", side_effect=[
        requests.ConnectionError("down"), FakeResp({"ok": 1}, 200),
    ]) as getter, mock.patch("time.sleep"):
        resp = http_utils.throttled_get("https://example.com/x", delay=0, retries=3)
    assert resp.status_code == 200
    assert getter.call_count == 2


def test_throttled_get_gives_up_after_retries():
    with mock.patch("requests.get", return_value=FakeResp({}, 503)), mock.patch("time.sleep"):
        with pytest.raises(requests.HTTPError):
            http_utils.throttled_get("https://example.com/x", delay=0, retries=3)


def test_throttled_get_no_retry_on_client_error():
    with mock.patch("requests.get", return_value=FakeResp({}, 404)) as getter, mock.patch("time.sleep"):
        with pytest.raises(requests.HTTPError):
            http_utils.throttled_get("https://example.com/x", delay=0, retries=3)
    assert getter.call_count == 1


def test_throttled_get_enforces_delay():
    with mock.patch("requests.get", return_value=FakeResp({}, 200)), mock.patch("time.sleep") as sleeper:
        http_utils._last_request_ts = _time.monotonic()  # pretend we just called
        http_utils.throttled_get("https://example.com/x", delay=30, retries=1)
    assert sleeper.call_count == 1
    assert sleeper.call_args[0][0] > 25.0


def test_throttled_get_redacts_secrets(caplog):
    with mock.patch("requests.get", return_value=FakeResp({}, 200)), mock.patch("time.sleep"):
        with caplog.at_level(logging.INFO, logger="src.http_utils"):
            http_utils.throttled_get(
                "https://example.com/s",
                params={"term": "hello", "token": "SECRET123"},
                delay=0,
            )
    assert "SECRET123" not in caplog.text
