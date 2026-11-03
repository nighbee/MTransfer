"""Tests for the Yandex export stage (no real network; yandex_music mocked)."""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from src import export_yandex
from src.export_yandex import save_tracks
from src.models import Track
from src.yandex_client import (
    YandexAuthError,
    fetch_track_details,
    get_client,
    iter_liked_raw,
)


RAW_FULL = {
    "id": "123456",
    "title": "  Test Song  ",
    "artists": [{"name": "Artist One"}, {"name": "Artist Two"}],
    "albums": [{"title": "Test Album"}],
    "duration_ms": 213000,
    "isrc": "USABC1234567",
    "cover_uri": "avatars.yandex.net/get-music-content/123/%%",
}

RAW_FLAT = {
    "yandex_id": "999",
    "title": "Flat Song",
    "artists": ["Solo Artist"],
    "album": "Flat Album",
    "duration_ms": 180000,
    "isrc": "",
    "cover_url": "https://example.com/cover.jpg",
}


def test_fetch_track_details_mapping():
    client = mock.MagicMock()
    track = fetch_track_details(client, dict(RAW_FULL))
    assert isinstance(track, Track)
    assert track.source == "yandex"
    assert track.yandex_id == "123456"
    assert track.title == "Test Song"
    assert track.artists == ["Artist One", "Artist Two"]
    assert track.album == "Test Album"
    assert track.duration_ms == 213000
    assert track.isrc == "USABC1234567"
    assert track.cover_url.startswith("https://")
    assert "%%" not in track.cover_url
    assert "avatars.yandex.net" in track.cover_url


def test_fetch_track_details_flat_variants():
    client = mock.MagicMock()
    track = fetch_track_details(client, dict(RAW_FLAT))
    assert track.yandex_id == "999"
    assert track.title == "Flat Song"
    assert track.artists == ["Solo Artist"]
    assert track.album == "Flat Album"
    assert track.duration_ms == 180000
    assert track.cover_url == "https://example.com/cover.jpg"


def test_iter_liked_raw_combines_pages():
    raw1 = {"id": "1", "title": "One", "artists": ["A"], "album": "Al",
            "duration_ms": 1000, "cover_url": ""}
    raw2 = {"id": "2", "title": "Two", "artists": ["B"], "album": "Al",
            "duration_ms": 2000, "cover_url": ""}
    raw3 = {"id": "3", "title": "Three", "artists": ["C"], "album": "Al",
            "duration_ms": 3000, "cover_url": ""}
    pages = {0: [raw1, raw2], 1: [raw3]}

    def fake(*args, **kwargs):
        page = kwargs.get("page", args[0] if args else 0)
        return pages.get(page, [])

    client = mock.MagicMock()
    client.users_likes_tracks.side_effect = fake

    got = list(iter_liked_raw(client, page_size=2))
    assert got == [raw1, raw2, raw3]
    assert client.users_likes_tracks.call_count >= 2


def test_iter_liked_raw_supports_bare_list_and_attr_shape():
    # Bare list return (no pagination kwargs handling) + object with .tracks
    client = mock.MagicMock()
    client.users_likes_tracks.return_value = [
        {"id": "7", "title": "Seven", "artists": ["Z"], "album": "",
         "duration_ms": 7000}
    ]
    got = list(iter_liked_raw(client, page_size=100))
    assert got[0]["id"] == "7"

    client2 = mock.MagicMock()
    holder = mock.MagicMock()
    holder.tracks = [{"id": "8", "title": "Eight"}]
    client2.users_likes_tracks.return_value = holder
    got2 = list(iter_liked_raw(client2, page_size=100))
    assert got2 == [{"id": "8", "title": "Eight"}]


def test_save_json_csv_roundtrip(tmp_path: Path):
    tracks = [
        Track(source="yandex", yandex_id="1", title="Song A",
              artists=["Art A", "Art B"], album="Alb A",
              duration_ms=213000, isrc="ISRC1",
              cover_url="https://example.com/a.jpg"),
        Track(source="yandex", yandex_id="2", title="Song B",
              artists=["Art C"], album="Alb B",
              duration_ms=180000, isrc="",
              cover_url="https://example.com/b.jpg"),
    ]
    nested = tmp_path / "nested" / "sub"
    json_path = nested / "yandex_favorites.json"
    csv_path = nested / "yandex_favorites.csv"

    save_tracks(tracks, json_path, csv_path)

    assert json_path.exists()
    assert csv_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data) == 2
    assert data[0]["yandex_id"] == "1"
    assert data[0]["artists"] == ["Art A", "Art B"]
    assert data[1]["title"] == "Song B"

    with csv_path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["yandex_id"] == "1"
    assert rows[0]["title"] == "Song A"
    # artists joined with "; " must split back
    assert [a.strip() for a in rows[0]["artists"].split(";")] == ["Art A", "Art B"]
    assert int(rows[0]["duration_ms"]) == 213000
    assert rows[0]["isrc"] == "ISRC1"
    assert rows[0]["cover_url"] == "https://example.com/a.jpg"


def test_401_iter_raises_clean_auth_error_without_token_leak():
    secret = "SECRET_TOKEN_abc123"
    client = mock.MagicMock()
    client.users_likes_tracks.side_effect = Exception(
        f"401 Unauthorized token={secret}"
    )
    with pytest.raises(YandexAuthError) as ei:
        list(iter_liked_raw(client))
    msg = str(ei.value)
    assert "re-login" in msg.lower()
    assert secret not in msg
    assert "401" in msg


def test_401_get_client_raises_clean_auth_error_without_token_leak():
    secret = "SECRET_TOKEN_xyz999"
    fake_client_cls = mock.MagicMock(
        side_effect=Exception(f"401 Unauthorized for {secret}")
    )
    fake_mod = mock.MagicMock()
    fake_mod.Client = fake_client_cls
    with mock.patch.dict(sys.modules, {"yandex_music": fake_mod}):
        with pytest.raises(YandexAuthError) as ei:
            get_client(secret)
    msg = str(ei.value)
    assert "re-login" in msg.lower()
    assert secret not in msg


def test_empty_likes_handled(tmp_path: Path):
    client = mock.MagicMock()
    client.users_likes_tracks.return_value = []
    assert list(iter_liked_raw(client)) == []

    assert export_yandex.export_likes(client, delay=0) == []

    json_path = tmp_path / "yandex_favorites.json"
    csv_path = tmp_path / "yandex_favorites.csv"
    save_tracks([], json_path, csv_path)
    assert json.loads(json_path.read_text(encoding="utf-8")) == []
    with csv_path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == []


def test_cli_verify_only_writes_no_files(tmp_path: Path, capsys):
    client = mock.MagicMock()
    raws = [
        {"id": "1", "title": "One", "artists": ["A"], "album": "Al",
         "duration_ms": 1000},
        {"id": "2", "title": "Two", "artists": ["B"], "album": "Al",
         "duration_ms": 2000},
    ]
    json_path = tmp_path / "out.json"
    csv_path = tmp_path / "out.csv"
    with mock.patch.object(export_yandex, "get_client", return_value=client), \
         mock.patch.object(export_yandex, "iter_liked_raw", return_value=iter(raws)), \
         mock.patch.object(
             export_yandex, "fetch_track_details",
             side_effect=lambda c, r: Track(yandex_id=str(r["id"]),
                                            title=r["title"],
                                            artists=list(r["artists"]),
                                            album=r["album"],
                                            duration_ms=r["duration_ms"])):
        rc = export_yandex.main(["--token", "dummy",
                                 "--verify-only",
                                 "--out-json", str(json_path),
                                 "--out-csv", str(csv_path),
                                 "--delay", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2" in out
    assert not json_path.exists()
    assert not csv_path.exists()


def test_cli_limit_and_token_env_fallback(tmp_path: Path):
    json_path = tmp_path / "y.json"
    csv_path = tmp_path / "c.csv"
    raws = [
        {"id": "1", "title": "One", "artists": ["A"], "album": "Al",
         "duration_ms": 1000},
        {"id": "2", "title": "Two", "artists": ["B"], "album": "Al",
         "duration_ms": 2000},
        {"id": "3", "title": "Three", "artists": ["C"], "album": "Al",
         "duration_ms": 3000},
    ]
    client = mock.MagicMock()
    seen = {}

    def fake_get_client(token):
        seen["token"] = token
        return client

    with mock.patch.object(export_yandex, "get_client",
                           side_effect=fake_get_client), \
         mock.patch.object(export_yandex, "iter_liked_raw",
                           return_value=iter(raws)), \
         mock.patch.object(
             export_yandex, "fetch_track_details",
             side_effect=lambda c, r: Track(yandex_id=str(r["id"]),
                                            title=r["title"],
                                            artists=list(r["artists"]),
                                            album=r["album"],
                                            duration_ms=r["duration_ms"])), \
         mock.patch.dict(os.environ, {"YANDEX_MUSIC_TOKEN": "env-token-123"}):
        rc = export_yandex.main(["--limit", "2",
                                 "--out-json", str(json_path),
                                 "--out-csv", str(csv_path),
                                 "--delay", "0"])
    assert rc == 0
    assert seen["token"] == "env-token-123"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data) == 2
