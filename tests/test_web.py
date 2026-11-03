"""Web UI tests (Phase 4).

Covers per task spec (mocked, no real network, no real tokens):
- GET / health (+ /api/health)
- POST /api/yandex/test with fake token calls adapter (mocked)
- GET /api/match/results reads matched/unmatched files
- POST /api/import/dry-run returns counts without mutations (importer mocked)
- POST /api/apple/user-token stores in memory only (never written to disk)
- EN/RU strings both present
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import web as webmod  # noqa: E402
from src.web import TOKENS, app  # noqa: E402


@pytest.fixture()
def client():
    TOKENS.clear()
    with TestClient(app) as c:
        yield c
    TOKENS.clear()


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_root_health_alias(client):
    # Plain "GET /health" also answers (task shorthand for health check).
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "MTransfer" in r.text


def test_en_ru_strings_both_present(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    # EN strings
    assert "Connect Yandex" in html
    assert "Match tracks" in html or "Run matching" in html
    assert "Sign in with Apple Music" in html
    # RU strings (JS dict)
    assert "Подключить" in html or "Яндекс" in html
    assert "Сопоставить" in html or "Сопоставление" in html
    # Toggle present
    assert "toggleLang" in html
    # 4 screens + MusicKit authorize + dry-run + unmatched.csv link
    assert "musickit" in html.lower()
    assert "authorize" in html
    assert "unmatched.csv" in html.lower()
    assert 'id="s1"' in html and 'id="s4"' in html


def test_yandex_test_calls_adapter_mocked(client):
    fake_client = object()
    with mock.patch.object(webmod.yandex_client, "get_client",
                           return_value=fake_client) as gc, \
         mock.patch.object(webmod.yandex_client, "iter_liked_raw",
                           return_value=iter([{"id": "1"}, {"id": "2"},
                                              {"id": "3"}])) as it:
        r = client.post("/api/yandex/test", json={"token": "FAKE_YANDEX_123"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("count") == 3
    gc.assert_called_once()
    # token passed through to adapter (mockable seam), never echoed back
    assert gc.call_args[0][0] == "FAKE_YANDEX_123"
    assert "FAKE_YANDEX_123" not in r.text
    it.assert_called_once()


def test_yandex_test_missing_token_400(client):
    r = client.post("/api/yandex/test", json={})
    assert r.status_code == 400


def test_yandex_export_mocked_no_real_network(client, tmp_path):
    fake_client = object()
    from src.models import Track
    fake_tracks = [Track(yandex_id="y1", title="T1", artists=["A1"]),
                   Track(yandex_id="y2", title="T2", artists=["A2"])]
    with mock.patch.object(webmod.yandex_client, "get_client",
                           return_value=fake_client), \
         mock.patch.object(webmod.export_yandex, "export_likes",
                           return_value=fake_tracks) as ex, \
         mock.patch.object(webmod.export_yandex, "save_tracks",
                           return_value=None) as sv:
        r = client.post("/api/yandex/export",
                        json={"token": "FAKE_YANDEX_123", "limit": 2})
    assert r.status_code == 200, r.text
    assert r.json().get("exported") == 2
    ex.assert_called_once()
    sv.assert_called_once()
    assert "FAKE_YANDEX_123" not in r.text


def test_match_run_mocked(client):
    with mock.patch.object(webmod.match_apple, "run",
                           return_value={"total": 5, "matched": 4,
                                         "unmatched": 1}) as m:
        r = client.post("/api/match/run", json={"limit": 5})
    assert r.status_code == 200, r.text
    assert r.json()["matched"] == 4
    m.assert_called_once()
    _a, kw = m.call_args
    assert kw.get("limit") == 5


def test_match_results_reads_files(client, tmp_path, monkeypatch):
    matched = [{"yandex_id": "y1", "apple_id": "s1",
                "confidence": 0.97, "method": "isrc"}]
    unmatched = [{"yandex_id": "y2", "title": "Nope", "artists": ["Nobody"],
                  "reason": "no_candidates", "confidence": 0.0}]
    mp = tmp_path / "matched_tracks.json"
    up = tmp_path / "unmatched_tracks.json"
    mp.write_text(json.dumps(matched), encoding="utf-8")
    up.write_text(json.dumps(unmatched), encoding="utf-8")
    monkeypatch.setattr(webmod, "MATCHED_PATH", mp)
    monkeypatch.setattr(webmod, "UNMATCHED_PATH", up)
    r = client.get("/api/match/results")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched_count"] == 1
    assert body["unmatched_count"] == 1
    assert body["matched"][0]["apple_id"] == "s1"
    assert body["unmatched"][0]["yandex_id"] == "y2"


def test_import_dry_run_returns_counts_without_mutations(client):
    stats = {"dry_run": True, "would_add": 7, "would_favorite": 7,
             "total": 7, "considered": 7}
    with mock.patch.object(webmod.import_apple, "run",
                           return_value=dict(stats)) as m, \
         mock.patch.object(webmod.import_apple, "_request_with_retry",
                           side_effect=AssertionError("no network in dry-run")):
        r = client.post("/api/import/dry-run", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["would_add"] == 7
    assert body["dry_run"] is True
    # importer was the (mocked) seam, called in dry-run mode
    m.assert_called_once()
    _a, kw = m.call_args
    assert kw.get("dry_run") is True


def test_import_run_mocked(client):
    with mock.patch.object(webmod.import_apple, "run",
                           return_value={"imported": 3, "skipped": 1,
                                         "failed": []}) as m:
        r = client.post("/api/import/run", json={})
    assert r.status_code == 200
    assert r.json()["imported"] == 3
    m.assert_called_once()


def test_import_retry_mocked(client):
    with mock.patch.object(webmod.import_apple, "run",
                           return_value={"imported": 1, "skipped": 0,
                                         "failed": []}) as m:
        r = client.post("/api/import/retry", json={})
    assert r.status_code == 200
    assert r.json()["imported"] == 1
    _a, kw = m.call_args
    assert kw.get("only_failed") is True


def test_apple_user_token_memory_only_no_file(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = {p.resolve() for p in tmp_path.rglob("*") if p.is_file()}
    secret = "MUT_FAKE_SECRET_abcdef123456"
    with mock.patch.object(webmod.extract_apple_tokens, "save_token",
                           side_effect=AssertionError("must not persist")) as sv:
        r = client.post("/api/apple/user-token", json={"token": secret})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
    # stored in memory
    assert secret in TOKENS.values()
    sv.assert_not_called()
    # never written to disk: no new files, secret absent from all files
    after = {p.resolve() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    for p in after:
        assert secret not in p.read_text(encoding="utf-8", errors="ignore")
    assert secret not in r.text
    # repo dot-token files untouched
    for name in (".media_user_token", ".apple_music_token", ".developer_token"):
        assert not (tmp_path / name).exists()
        assert not (webmod.BASE_DIR / name).exists() or \
            secret not in (webmod.BASE_DIR / name).read_text(
                encoding="utf-8", errors="ignore")


def test_apple_user_token_missing_400(client):
    r = client.post("/api/apple/user-token", json={})
    assert r.status_code == 400


def test_dev_token_status_endpoint(client, tmp_path, monkeypatch):
    # memory token present
    TOKENS["dev"] = "DEV_FAKE_123456"
    r = client.get("/api/apple/dev-token/status")
    assert r.status_code == 200
    assert r.json()["present"] is True
    TOKENS.clear()
    # no memory token, no file
    monkeypatch.setattr(webmod, "DEV_TOKEN_PATH",
                        tmp_path / ".developer_token")
    with mock.patch.object(webmod.extract_apple_tokens, "load_token",
                           side_effect=FileNotFoundError("no token")):
        r = client.get("/api/apple/dev-token/status")
    assert r.json()["present"] is False


def test_report_endpoint(client, tmp_path, monkeypatch):
    rp = tmp_path / "migration_report.txt"
    mp = tmp_path / "matched_tracks.json"
    up = tmp_path / "unmatched_tracks.json"
    rp.write_text("Migration report\ntotal: 2\n", encoding="utf-8")
    mp.write_text(json.dumps([{"yandex_id": "y1"}]), encoding="utf-8")
    up.write_text(json.dumps([{"yandex_id": "y2"}]), encoding="utf-8")
    monkeypatch.setattr(webmod, "REPORT_PATH", rp)
    monkeypatch.setattr(webmod, "MATCHED_PATH", mp)
    monkeypatch.setattr(webmod, "UNMATCHED_PATH", up)
    r = client.get("/api/report")
    assert r.status_code == 200
    body = r.json()
    assert "Migration report" in body.get("report", "")
    assert body["matched"] == 1
    assert body["unmatched"] == 1
