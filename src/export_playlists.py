"""Export chosen Yandex Music playlists to JSON + CSV.

Usage:
    python -m src.export_playlists --list
    python -m src.export_playlists --kinds 1234,5678
    python -m src.export_playlists --all

Token source: ``--token`` flag, ``YANDEX_MUSIC_TOKEN`` env, or ``.yandex_token`` file.
Output: ``data/playlists/<kind>_<slug>.{json,csv}`` + ``data/playlists/index.json``.
Tokens are never printed or logged.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import List, Optional

from src.export_yandex import _raw_id, save_tracks, _token_from_sources
from src.models import Track
from src.yandex_client import (
    YandexAuthError,
    fetch_playlist_tracks,
    fetch_track_details,
    fetch_tracks_batch,
    get_client,
    list_playlists,
)

PLAYLISTS_DIR = Path(__file__).resolve().parent.parent / "data" / "playlists"


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "", flags=re.UNICODE).strip()
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:60] or "playlist"


def _resolve_tracks(client, raws, delay: float, batch_size: int,
                    progress=None) -> List[Track]:
    tracks: List[Track] = []
    total = len(raws)
    for start in range(0, len(raws), max(1, batch_size)):
        chunk = raws[start:start + max(1, batch_size)]
        details = fetch_tracks_batch(client, [_raw_id(r) for r in chunk])
        for raw in chunk:
            bid = _raw_id(raw).split(":")[0].strip()
            hit = details.get(bid)
            tracks.append(fetch_track_details(client, hit if hit is not None else raw))
        if progress:
            try:
                progress(len(tracks), total)
            except Exception:
                pass
        if delay and delay > 0:
            time.sleep(delay)
    return tracks


def export_playlist(client, kind: str, delay: float = 0.3,
                    batch_size: int = 100, limit: Optional[int] = None,
                    progress=None) -> tuple:
    """Export one playlist by kind. Returns (title, tracks)."""
    raws = fetch_playlist_tracks(client, kind)
    if limit is not None:
        raws = raws[:limit]
    tracks = _resolve_tracks(client, raws, delay, batch_size, progress)
    return tracks


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Export Yandex Music playlists.")
    p.add_argument("--token", default=None)
    p.add_argument("--list", dest="list_only", action="store_true",
                   help="List your playlists (kind, title, tracks) and exit.")
    p.add_argument("--kinds", default=None,
                   help="Comma-separated playlist kinds to export.")
    p.add_argument("--all", dest="all_playlists", action="store_true",
                   help="Export every playlist.")
    p.add_argument("--out-dir", default=str(PLAYLISTS_DIR))
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--delay", type=float, default=0.3)
    p.add_argument("--batch-size", type=int, default=100)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    token = args.token or _token_from_sources()
    if not token:
        print("Error: no Yandex token. Pass --token, set YANDEX_MUSIC_TOKEN, "
              "or run: python -m src.yandex_login", file=sys.stderr)
        return 2
    try:
        client = get_client(token)
    except YandexAuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        playlists = list_playlists(client)
    except YandexAuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.list_only or (not args.kinds and not args.all_playlists):
        if not playlists:
            print("No playlists found.")
        for pl in playlists:
            print(f"{pl['kind']}\t{pl.get('track_count', '?')}\t{pl.get('title', '')}")
        return 0

    if args.all_playlists:
        wanted = [str(pl["kind"]) for pl in playlists]
    else:
        wanted = [k.strip() for k in (args.kinds or "").split(",") if k.strip()]

    by_kind = {str(pl["kind"]): pl for pl in playlists}
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for kind in wanted:
        meta = by_kind.get(kind, {"title": kind, "track_count": "?"})
        title = str(meta.get("title") or kind)
        print(f"Exporting playlist {kind} {title!r} ...")

        def _prog(done, total, _t=title):
            print(f"  [{_t}] ... {done}/{total} tracks", flush=True)

        try:
            tracks = export_playlist(client, kind, delay=args.delay,
                                     batch_size=args.batch_size,
                                     limit=args.limit, progress=_prog)
        except YandexAuthError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        slug = _slug(title)
        json_path = out_dir / f"{kind}_{slug}.json"
        csv_path = out_dir / f"{kind}_{slug}.csv"
        save_tracks(tracks, json_path, csv_path)
        print(f"  -> {len(tracks)} tracks: {json_path}, {csv_path}")
        index.append({"kind": kind, "title": title,
                      "tracks": len(tracks),
                      "json": str(json_path), "csv": str(csv_path)})
    with open(out_dir / "index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
