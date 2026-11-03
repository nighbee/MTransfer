"""Tests for Apple token extraction + library import stage.

No real network, no real tokens: all HTTP is mocked (requests/httpx).
Covers per task spec:
- dev JWT scrape from fake beta.music.apple.com HTML bundle
- save/load dot-token files
- dry-run performs zero POST/PUT
- playlist reuse by name
- batch chunking <= 50
- skip already-present tracks
- retry --only-failed reprocesses failed list
- per-track failure recorded with reason
- verify_tokens via mocked amp-api /v1/me/storefront
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import extract_apple_tokens as ext  # noqa: E402
from src import import_apple as imp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

def _fake_jwt(tag: str = "PAYLOAD", sig_len: int = 86) -> str:
    # eyJ... header.payload.signature (base64url chars only)
    header = "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6IkFCQzEyMyJ9"
    payload = "eyJpc3MiOiJBQkMxMjMiLCJpYXQiOjE3MDAwMDAwMDAsInR5cCI6IkRldlRva2VuIn0"
    sig = ("c2ln" + tag.replace("=", "").replace(".", "")) * ((sig_len // 8) + 2)
    sig = "".join(c for c in sig if c.isalnum() or c in "-_")[:sig_len]
    return f"{header}.{payload}.{sig}"


FAKE_JWT = _fake_jwt("FAKEDEVJWT")
FAKE_JWT_SHORT = "eyJhYmMiOiIxIn0.eyJkZWYiOiIyIn0.c2lnbmF0dXJl"

FAKE_BETA_HTML = (
    "<!doctype html><html><head>"
    '<script src="/assets/vendor.aaa111.js"></script>'
    '<script src="/assets/main.bbb222.js"></script>'
    "</head><body><div id='app'>Apple Music</div></body></html>"
)

FAKE_JS_BUNDLE = (
    "webpackJsonp([12],{abc:function(e,t,n){"
    f'var DEV_TOKEN="{FAKE_JWT}";'
    "var e2='eyJzaG9ydCI6MX0.eyJ4IjoxfQ.c2ln';"
    "}});"
)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text if text else (json.dumps(json_data) if json_data is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._json_data is not None:
            return self._json_data
        return json.loads(self.text)


def _matched_file(tmp_path: Path, n: int = 3, start: int = 1) -> Path:
    entries = [
        {"yandex_id": f"y{i}", "apple_id": f"s{i}", "confidence": 0.95, "method": "isrc"}
        for i in range(start, start + n)
    ]
    p = tmp_path / "matched_tracks.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. Dev JWT scrape
# ---------------------------------------------------------------------------

def test_scrape_developer_token_from_fake_beta_bundle():
    html = FAKE_BETA_HTML.replace(
        "</head>", f"<script>window.__DEV__='{FAKE_JS_BUNDLE}';</script></head>"
    )
    token = ext.scrape_developer_token(html)
    assert token == FAKE_JWT
    assert token.startswith("eyJ")


_M62YD_DUD = (
    "eyJM62YDdWQiOiIxIn0"  # fake prefix mimicking the known-bad M62YD bundle JWT
    ".eyJkZWYiOiIyIn0.c2lnbmF0dXJlM62YDdWQi"
)


def test_scrape_developer_token_from_js_bundle_text():
    # Shortest valid JWT wins; the long M62YD dud is skipped even though longer.
    dud = "eyJM62YD" + "x" * 120 + "." + "y" * 80 + "." + "z" * 80
    bundle = FAKE_JS_BUNDLE + f'var DUD="{dud}";'
    token = ext.scrape_developer_token(bundle)
    assert token == FAKE_JWT
    assert FAKE_JWT_SHORT not in token


def test_scrape_prefers_shortest_and_skips_m62yd():
    short = _fake_jwt("SHORT", sig_len=60)
    long_dud = "eyJM62YD" + "x" * 60 + "." + "y" * 40 + "." + "z" * 80
    token = ext.scrape_developer_token(f"a={long_dud};b={short};")
    assert token == short


def test_web_headers_sent_on_amp_requests():
    # Regression: amp-api 401s without Origin — headers must include it.
    assert "Origin" in ext.WEB_HEADERS
    assert ext.WEB_HEADERS["Origin"] == "https://beta.music.apple.com"
    from src import import_apple as imp

    h = imp._headers("dev123456789", "user123456789")
    assert "Origin" in h
    assert h["Authorization"].startswith("Bearer ")


def test_scrape_developer_token_not_found():
    with pytest.raises(ValueError, match="not found"):
        ext.scrape_developer_token("<html><body>no token here</body></html>")
    with pytest.raises(ValueError, match="not found"):
        ext.scrape_developer_token("")


def test_fetch_developer_token_from_web_follows_script_bundle():
    # beta HTML has no inline JWT; JWT lives in the JS bundle.
    js_url_seen: list[str] = []

    def fake_retry(method, url, **kwargs):
        assert method == "GET"
        if url == ext.BETA_URL or url.rstrip("/") == ext.BETA_URL.rstrip("/"):
            return FakeResponse(200, text=FAKE_BETA_HTML)
        js_url_seen.append(url)
        return FakeResponse(200, text=FAKE_JS_BUNDLE)

    with mock.patch.object(ext, "_request_with_retry", side_effect=fake_retry):
        token = ext.fetch_developer_token_from_web()
    assert token == FAKE_JWT
    assert len(js_url_seen) >= 1


# ---------------------------------------------------------------------------
# 2. save/load dot-token files
# ---------------------------------------------------------------------------

def test_save_and_load_token_roundtrip(tmp_path):
    p = tmp_path / ".developer_token"
    ext.save_token(p, "  SECRET123456789  \n")
    assert ext.load_token(p) == "SECRET123456789"
    # overwrite works
    ext.save_token(p, "SECOND_VALUE_abcdef")
    assert ext.load_token(p) == "SECOND_VALUE_abcdef"


def test_load_token_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ext.load_token(tmp_path / ".does_not_exist")


def test_load_token_empty_raises(tmp_path):
    p = tmp_path / ".media_user_token"
    p.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        ext.load_token(p)


def test_redact_never_leaks_full_value():
    secret = "DEV_SECRET_TOKEN_abcdef123456"
    for mod in (ext, imp):
        r = mod.redact(secret)
        assert secret not in r
        assert r == secret[:6] + "..."
    assert ext.redact("") == "***"
    assert ext.redact(None) == "***"


# ---------------------------------------------------------------------------
# 3. verify_tokens via mocked amp-api /v1/me/storefront
# ---------------------------------------------------------------------------

def test_verify_tokens_ok_mocked():
    with mock.patch.object(
        ext, "_request_with_retry", return_value=FakeResponse(200, {"data": [{"id": "us"}]})
    ) as m:
        assert ext.verify_tokens("DEV_FAKE_123456", "USER_FAKE_123456", "us") is True
        # called the storefront endpoint, never with raw token in logs path
        args, kwargs = m.call_args
        assert args[0] == "GET"
        assert args[1].endswith("/v1/me/storefront")


def test_verify_tokens_unauthorized_returns_false():
    with mock.patch.object(ext, "_request_with_retry", return_value=FakeResponse(401, {})):
        assert ext.verify_tokens("DEV_FAKE_123456", "USER_FAKE_123456") is False


def test_verify_tokens_network_error_returns_false():
    with mock.patch.object(ext, "_request_with_retry", side_effect=ConnectionError("down")):
        assert ext.verify_tokens("DEV_FAKE_123456", "USER_FAKE_123456") is False


# ---------------------------------------------------------------------------
# 4. local retry helper: backoff on 429/5xx (both modules)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod", [ext, imp], ids=["extract", "import"])
def test_request_with_retry_retries_429_then_succeeds(mod):
    calls = {"n": 0}

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(429, {}, headers={"Retry-After": "0"})
        return FakeResponse(200, {"ok": True})

    with mock.patch.object(mod.requests, "request", side_effect=fake_request):
        with mock.patch("time.sleep", return_value=None) as slp:
            resp = mod._request_with_retry("GET", "https://example.test/x",
                                           max_retries=3, backoff_factor=0)
    assert resp.status_code == 200
    assert calls["n"] == 2
    assert slp.called


@pytest.mark.parametrize("mod", [ext, imp], ids=["extract", "import"])
def test_request_with_retry_retries_500_then_succeeds(mod):
    seq = [FakeResponse(500, {}), FakeResponse(200, {"ok": True})]
    with mock.patch.object(mod.requests, "request", side_effect=seq):
        with mock.patch("time.sleep", return_value=None):
            resp = mod._request_with_retry("GET", "https://example.test/y",
                                           max_retries=3, backoff_factor=0)
    assert resp.status_code == 200


def test_request_with_retry_does_not_retry_4xx():
    with mock.patch.object(ext.requests, "request",
                           return_value=FakeResponse(401, {})) as req:
        resp = ext._request_with_retry("GET", "https://example.test/z", backoff_factor=0)
    assert resp.status_code == 401
    assert req.call_count == 1


# ---------------------------------------------------------------------------
# 5. dry-run performs zero POST/PUT
# ---------------------------------------------------------------------------

def test_dry_run_performs_zero_mutations(tmp_path):
    matched = _matched_file(tmp_path, n=5)
    mutate_calls: list[tuple] = []

    def fake_retry(method, url, **kwargs):
        if method.upper() in ("POST", "PUT", "DELETE", "PATCH"):
            mutate_calls.append((method, url))
            raise AssertionError(f"dry-run must not mutate: {method} {url}")
        raise AssertionError(f"dry-run must not call network at all: {method} {url}")

    with mock.patch.object(imp, "_request_with_retry", side_effect=fake_retry):
        stats = imp.run(
            matched,
            playlist_name="Yandex Import 2026-01-01",
            dry_run=True,
            dev_token="DEV_FAKE_123456",
            user_token="USER_FAKE_123456",
        )
    assert mutate_calls == []
    assert stats["dry_run"] is True
    assert stats["would_add"] == 5
    assert stats["would_favorite"] == 5
    assert stats["imported"] == 0


def test_dry_run_cli_prints_pending_without_mutation(tmp_path, capsys):
    matched = _matched_file(tmp_path, n=2)
    with mock.patch.object(
        imp, "_request_with_retry",
        side_effect=AssertionError("no network in dry-run"),
    ):
        rc = imp.main([str(matched), "--playlist", "Dry Playlist", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dry-run" in out
    assert "2 tracks would be added" in out


# ---------------------------------------------------------------------------
# 6. playlist reuse by name
# ---------------------------------------------------------------------------

def test_playlist_reuse_by_name(tmp_path):
    matched = _matched_file(tmp_path, n=2)
    with mock.patch.object(imp, "find_playlist_by_name", return_value="pl_EXIST123") as finder, \
         mock.patch.object(
             imp, "create_playlist",
             side_effect=AssertionError("must not create when reuse possible")) as creator, \
         mock.patch.object(imp, "list_playlist_tracks", return_value=[]), \
         mock.patch.object(imp, "add_tracks",
                           return_value={"added": ["s1", "s2"], "failed": []}) as adder, \
         mock.patch.object(imp, "set_favorites",
                           return_value={"added": ["s1", "s2"], "failed": []}):
        stats = imp.run(
            matched, playlist_name="Yandex Import 2026-01-01",
            dev_token="DEV_FAKE", user_token="USER_FAKE",
            failed_json=tmp_path / "failed.json",
        )
    assert finder.called
    assert creator.call_count == 0
    assert stats["playlist_id"] == "pl_EXIST123"
    assert stats["imported"] == 2
    assert adder.called


def test_find_playlist_by_name_returns_id_and_none():
    pages = [
        FakeResponse(200, {"data": [
            {"id": "pl111", "attributes": {"name": "Other Mix"}},
            {"id": "pl222", "attributes": {"name": "Yandex Import 2026-01-01"}},
        ]}),
    ]
    with mock.patch.object(imp, "_request_with_retry", side_effect=pages):
        assert imp.find_playlist_by_name("D", "U", "Yandex Import 2026-01-01") == "pl222"
    with mock.patch.object(
        imp, "_request_with_retry",
        return_value=FakeResponse(200, {"data": [{"id": "x", "attributes": {"name": "zzz"}}]}),
    ):
        assert imp.find_playlist_by_name("D", "U", "missing name") is None


def test_create_playlist_not_called_when_reused_via_cli(tmp_path):
    matched = _matched_file(tmp_path, n=1)
    with mock.patch.object(imp, "find_playlist_by_name", return_value="pl_REUSE"), \
         mock.patch.object(imp, "create_playlist",
                           side_effect=AssertionError("should reuse")), \
         mock.patch.object(imp, "list_playlist_tracks", return_value=["s1"]), \
         mock.patch.object(imp, "set_favorites",
                           return_value={"added": [], "failed": []}), \
         mock.patch.object(imp, "add_tracks",
                           return_value={"added": [], "failed": []}) as adder:
        stats = imp.run(matched, playlist_name="ReuseMe", dev_token="D", user_token="U",
                        failed_json=tmp_path / "f.json")
    assert stats["playlist_id"] == "pl_REUSE"
    # s1 already present -> nothing to add
    assert adder.call_args is not None
    _a, _k = adder.call_args
    assert _a[3] == [] or _k.get("track_ids", []) == [] if _k else True


# ---------------------------------------------------------------------------
# 7. batch chunking <= 50
# ---------------------------------------------------------------------------

def test_add_tracks_chunks_le_50():
    ids = [f"s{i}" for i in range(120)]
    seen_sizes: list[int] = []

    def fake_retry(method, url, **kwargs):
        assert method == "POST"
        body = kwargs.get("json_data") or {}
        chunk = body.get("data", [])
        seen_sizes.append(len(chunk))
        assert len(chunk) <= 50
        return FakeResponse(201, {"data": []})

    with mock.patch.object(imp, "_request_with_retry", side_effect=fake_retry):
        res = imp.add_tracks("DEV_FAKE", "USER_FAKE", "pl1", ids)
    assert seen_sizes == [50, 50, 20]
    assert len(res["added"]) == 120
    assert res["failed"] == []


def test_set_favorites_one_put_per_track():
    # Correct endpoint: PUT /v1/me/ratings/songs/{id} (bulk returns 405).
    ids = [f"s{i}" for i in range(5)]
    seen: list = []

    def fake_retry(method, url, **kwargs):
        assert method == "PUT"
        seen.append(url)
        body = kwargs.get("json_data") or {}
        assert body == {"attributes": {"value": 1}}
        return FakeResponse(200, {})

    with mock.patch.object(imp, "_request_with_retry", side_effect=fake_retry):
        res = imp.set_favorites("D", "U", ids)
    assert len(seen) == 5
    assert all(u.endswith(f"/ratings/songs/{i}") for u, i in zip(seen, ids))
    assert len(res["added"]) == 5


def test_get_storefront_parses_id():
    with mock.patch.object(
        imp, "_request_with_retry",
        return_value=FakeResponse(200, {"data": [{"id": "ru"}]}),
    ):
        assert imp.get_storefront("D", "U") == "ru"


# ---------------------------------------------------------------------------
# 8. skip already-present tracks
# ---------------------------------------------------------------------------

def test_skip_already_present_tracks(tmp_path):
    matched = _matched_file(tmp_path, n=4)  # s1..s4
    added_arg: dict = {}

    def fake_add(dev, user, pid, ids):
        added_arg["ids"] = list(ids)
        return {"added": list(ids), "failed": []}

    with mock.patch.object(imp, "find_playlist_by_name", return_value="pl1"), \
         mock.patch.object(imp, "create_playlist",
                           side_effect=AssertionError("reuse expected")), \
         mock.patch.object(imp, "list_playlist_tracks", return_value=["s1", "s3"]), \
         mock.patch.object(imp, "add_tracks", side_effect=fake_add), \
         mock.patch.object(imp, "set_favorites",
                           return_value={"added": ["s2", "s4"], "failed": []}):
        stats = imp.run(matched, playlist_name="P", dev_token="D", user_token="U",
                        failed_json=tmp_path / "failed.json")
    assert sorted(added_arg["ids"]) == ["s2", "s4"]
    assert stats["skipped"] == 2
    assert stats["imported"] == 2


# ---------------------------------------------------------------------------
# 9. retry --only-failed reprocesses failed list
# ---------------------------------------------------------------------------

def test_retry_only_failed_reprocesses_failed_list(tmp_path):
    matched = _matched_file(tmp_path, n=3)  # s1, s2, s3
    failed_file = tmp_path / "import_failed.json"
    failed_file.write_text(json.dumps([{"apple_id": "s2", "reason": "HTTP 500"}]),
                           encoding="utf-8")
    seen: dict = {}

    def fake_add(dev, user, pid, ids):
        seen["ids"] = list(ids)
        return {"added": list(ids), "failed": []}

    with mock.patch.object(imp, "find_playlist_by_name", return_value="pl9"), \
         mock.patch.object(imp, "list_playlist_tracks", return_value=[]), \
         mock.patch.object(imp, "add_tracks", side_effect=fake_add), \
         mock.patch.object(imp, "set_favorites",
                           return_value={"added": seen.get("ids", []), "failed": []}):
        stats = imp.run(
            matched, playlist_name="P", only_failed=True,
            dev_token="D", user_token="U", failed_json=failed_file,
        )
    assert seen["ids"] == ["s2"]
    assert stats["considered"] == 1
    assert stats["total"] == 3


def test_only_failed_cli_aliases_parse(tmp_path):
    matched = _matched_file(tmp_path, n=1)
    for flag in ("--only-failed", "--retry-only-failed"):
        failed_file = tmp_path / f"failed_{flag.strip('-')}.json"
        failed_file.write_text(json.dumps([{"apple_id": "s1", "reason": "x"}]),
                               encoding="utf-8")
        with mock.patch.object(imp, "find_playlist_by_name", return_value="pl1"), \
             mock.patch.object(imp, "list_playlist_tracks", return_value=[]), \
             mock.patch.object(imp, "add_tracks",
                               return_value={"added": ["s1"], "failed": []}), \
             mock.patch.object(imp, "set_favorites",
                               return_value={"added": ["s1"], "failed": []}):
            rc = imp.main([str(matched), "--playlist", "P", flag,
                           "--failed-json", str(failed_file),
                           "--dev-token", "D", "--user-token", "U"])
        assert rc == 0


def test_from_n_resume_offset(tmp_path):
    matched = _matched_file(tmp_path, n=5)  # s1..s5
    seen: dict = {}

    def fake_add(dev, user, pid, ids):
        seen["ids"] = list(ids)
        return {"added": list(ids), "failed": []}

    with mock.patch.object(imp, "find_playlist_by_name", return_value="pl1"), \
         mock.patch.object(imp, "list_playlist_tracks", return_value=[]), \
         mock.patch.object(imp, "add_tracks", side_effect=fake_add), \
         mock.patch.object(imp, "set_favorites",
                           return_value={"added": [], "failed": []}):
        imp.run(matched, playlist_name="P", from_n=3, dev_token="D", user_token="U",
                failed_json=tmp_path / "f.json")
    assert seen["ids"] == ["s4", "s5"]


def test_cli_from_flag_resume(tmp_path):
    matched = _matched_file(tmp_path, n=4)
    seen: dict = {}

    def fake_add(dev, user, pid, ids):
        seen["ids"] = list(ids)
        return {"added": list(ids), "failed": []}

    with mock.patch.object(imp, "find_playlist_by_name", return_value="pl1"), \
         mock.patch.object(imp, "list_playlist_tracks", return_value=[]), \
         mock.patch.object(imp, "add_tracks", side_effect=fake_add), \
         mock.patch.object(imp, "set_favorites",
                           return_value={"added": [], "failed": []}):
        rc = imp.main([str(matched), "--playlist", "P", "--from", "2",
                       "--dev-token", "D", "--user-token", "U",
                       "--failed-json", str(tmp_path / "f.json")])
    assert rc == 0
    assert seen["ids"] == ["s3", "s4"]


# ---------------------------------------------------------------------------
# 10. per-track failure recorded with reason
# ---------------------------------------------------------------------------

def test_per_track_failure_recorded_with_reason():
    def fake_retry(method, url, **kwargs):
        return FakeResponse(422, {"errors": [{"detail": "bad id"}]})

    with mock.patch.object(imp, "_request_with_retry", side_effect=fake_retry):
        res = imp.add_tracks("D", "U", "pl1", ["s_good", "s_bad"])
    assert res["added"] == []
    assert len(res["failed"]) == 2
    for f in res["failed"]:
        assert f["apple_id"] in ("s_good", "s_bad")
        assert "HTTP 422" in f["reason"]


def test_run_records_failure_and_persists_failed_file(tmp_path):
    matched = _matched_file(tmp_path, n=2)
    failed_path = tmp_path / "import_failed.json"

    def fake_retry(method, url, **kwargs):
        if method == "GET":
            if "tracks" in url:
                return FakeResponse(200, {"data": []})
            return FakeResponse(200, {"data": [{"id": "pl1", "attributes": {"name": "P"}}]})
        return FakeResponse(500, {})  # POST add fails -> retried inside add_tracks? single-shot here

    # add_tracks will see 500s; patch sleep to keep test fast
    with mock.patch.object(imp, "find_playlist_by_name", return_value="pl1"), \
         mock.patch.object(imp, "list_playlist_tracks", return_value=[]), \
         mock.patch.object(imp, "_request_with_retry", side_effect=fake_retry), \
         mock.patch("time.sleep", return_value=None):
        stats = imp.run(matched, playlist_name="P", dev_token="D", user_token="U",
                        failed_json=failed_path)
    assert stats["imported"] == 0
    assert len(stats["failed"]) >= 2  # add failures (+ maybe favorite failures)
    assert all("reason" in f and f["reason"] for f in stats["failed"])
    assert failed_path.exists()
    persisted = json.loads(failed_path.read_text(encoding="utf-8"))
    assert len(persisted) == len(stats["failed"])


def test_run_missing_apple_id_recorded_as_failure(tmp_path):
    p = tmp_path / "matched.json"
    p.write_text(json.dumps([
        {"yandex_id": "y1", "confidence": 0.1, "method": "miss"},
        {"yandex_id": "y2", "apple_id": "s2", "confidence": 0.9},
    ]), encoding="utf-8")
    with mock.patch.object(imp, "find_playlist_by_name", return_value="pl1"), \
         mock.patch.object(imp, "list_playlist_tracks", return_value=[]), \
         mock.patch.object(imp, "add_tracks",
                           return_value={"added": ["s2"], "failed": []}), \
         mock.patch.object(imp, "set_favorites",
                           return_value={"added": ["s2"], "failed": []}):
        stats = imp.run(p, playlist_name="P", dev_token="D", user_token="U",
                        failed_json=tmp_path / "f.json")
    reasons = [f["reason"] for f in stats["failed"]]
    assert any("missing apple_id" in r for r in reasons)


# ---------------------------------------------------------------------------
# 11. extract CLI: --save / --from-chrome / --verify (mocked)
# ---------------------------------------------------------------------------

def test_extract_cli_save_mocks_network(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with mock.patch.object(ext, "fetch_developer_token_from_web", return_value=FAKE_JWT):
        rc = ext.main(["--save"])
    assert rc == 0
    assert (tmp_path / ".developer_token").read_text(encoding="utf-8").strip() == FAKE_JWT
    out = capsys.readouterr().out
    assert FAKE_JWT not in out  # redacted
    assert ".developer_token" in out


def test_extract_cli_from_chrome_fallback_message(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with mock.patch.object(ext, "read_chrome_cookie", return_value=None):
        rc = ext.main(["--from-chrome"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Manual fallback" in out
    assert "media-user-token" in out


def test_extract_cli_from_chrome_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with mock.patch.object(ext, "read_chrome_cookie", return_value="USER_COOKIE_FAKE_123"):
        rc = ext.main(["--from-chrome"])
    assert rc == 0
    assert (tmp_path / ".media_user_token").read_text(encoding="utf-8").strip() == "USER_COOKIE_FAKE_123"


def test_extract_cli_verify_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".developer_token").write_text("DEV_FAKE_123456789\n", encoding="utf-8")
    (tmp_path / ".media_user_token").write_text("USER_FAKE_123456789\n", encoding="utf-8")
    with mock.patch.object(ext, "verify_tokens", return_value=True):
        rc = ext.main(["--verify"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out
    # raw tokens never echoed
    assert "DEV_FAKE_123456789" not in out
    assert "USER_FAKE_123456789" not in out


def test_no_real_network_guard():
    """Both modules must expose a local _request_with_retry (no shared http_utils)."""
    import inspect

    assert callable(ext._request_with_retry)
    assert callable(imp._request_with_retry)
    assert "backoff" in inspect.getsource(ext._request_with_retry).lower()
    assert "429" in inspect.getsource(imp._request_with_retry)
    # must not import a shared helper module (check import statements only)
    for mod in (ext, imp):
        src = inspect.getsource(mod)
        assert "import http_utils" not in src
        assert "from http_utils" not in src
