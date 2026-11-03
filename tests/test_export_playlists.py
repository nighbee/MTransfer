"""Tests for playlist export: list + fetch + CLI (mocked, no network)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from src import export_playlists as ep
from src.models import Track
from src.yandex_client import (
    fetch_playlist_tracks,
    list_playlists,
)


def _pl(kind="777", title="My Mix", count=3):
    pl = mock.MagicMock()
    pl.kind = kind
    pl.title = title
    pl.track_count = count
    return pl


def test_list_playlists_maps_kind_title_count():
    client = mock.MagicMock()
    client.users_playlists_list.return_value = [_pl(), _pl("888", "Chill", 5)]
    out = list_playlists(client)
    assert out == [
        {"kind": "777", "title": "My Mix", "track_count": 3, "owner": mock.ANY},
        {"kind": "888", "title": "Chill", "track_count": 5, "owner": mock.ANY},
    ]
    client.users_playlists_list.assert_called_once_with()


def test_list_playlists_dict_shape():
    client = mock.MagicMock()
    client.users_playlists_list.return_value = [
        {"kind": 111, "title": "Dict PL", "track_count": 7}
    ]
    out = list_playlists(client)
    assert out[0]["kind"] == "111"
    assert out[0]["title"] == "Dict PL"


def test_fetch_playlist_tracks_users_playlists_shape():
    holder = mock.MagicMock()
    holder.tracks = [{"id": "1"}, {"id": "2"}]
    client = mock.MagicMock()
    client.account_uid = 42
    client.users_playlists.return_value = holder
    out = fetch_playlist_tracks(client, "777")
    assert out == [{"id": "1"}, {"id": "2"}]
    client.users_playlists.assert_called_once()


def test_fetch_playlist_tracks_falls_back_to_playlist_uuid():
    holder = mock.MagicMock()
    holder.tracks = [{"id": "9"}]
    client = mock.MagicMock()
    client.users_playlists.side_effect = TypeError("nope")
    # second call form also fails -> fallback
    client.users_playlists.return_value = None
    client.playlist.return_value = holder
    client.users_playlists.reset_mock(side_effect=True)
    client.users_playlists.side_effect = Exception("boom-ignored")
    # force the users_playlists path to be skipped by removing it
    del client.users_playlists
    out = fetch_playlist_tracks(client, "uuid-1")
    assert out == [{"id": "9"}]


def test_export_playlists_list_cli(capsys):
    client = mock.MagicMock()
    client.users_playlists_list.return_value = [_pl()]
    with mock.patch.object(ep, "get_client", return_value=client):
        rc = ep.main(["--token", "T", "--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "777" in out and "My Mix" in out


def test_export_playlists_kinds_writes_files(tmp_path):
    client = mock.MagicMock()
    client.users_playlists_list.return_value = [_pl("777", "My Mix", 2)]
    raws = [{"id": "1", "title": "S1", "artists": ["A"], "duration_ms": 1000},
            {"id": "2", "title": "S2", "artists": ["B"], "duration_ms": 2000}]
    with mock.patch.object(ep, "get_client", return_value=client), \
         mock.patch.object(ep, "fetch_playlist_tracks", return_value=raws), \
         mock.patch.object(ep, "fetch_tracks_batch", return_value={}):
        rc = ep.main(["--token", "T", "--kinds", "777",
                      "--out-dir", str(tmp_path), "--delay", "0"])
    assert rc == 0
    files = list(tmp_path.glob("*.json"))
    assert any("777" in f.name for f in files if f.name != "index.json")
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert index[0]["kind"] == "777"
    assert index[0]["tracks"] == 2
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert {t["yandex_id"] for t in data} == {"1", "2"}


def test_export_playlists_unknown_kind_still_exports(tmp_path, capsys):
    client = mock.MagicMock()
    client.users_playlists_list.return_value = []
    with mock.patch.object(ep, "get_client", return_value=client), \
         mock.patch.object(ep, "fetch_playlist_tracks", return_value=[]):
        rc = ep.main(["--token", "T", "--kinds", "999",
                      "--out-dir", str(tmp_path), "--delay", "0"])
    assert rc == 0
    assert (tmp_path / "index.json").exists()
