"""Export Yandex Music liked tracks to JSON + CSV.

Usage:
    python -m src.export_yandex --token <TOKEN> [--out-json ...] [--out-csv ...]
    python -m src.export_yandex --verify-only   # count only, no files
    YANDEX_MUSIC_TOKEN=... python -m src.export_yandex --limit 20

Token source: ``--token`` flag, else ``YANDEX_MUSIC_TOKEN`` env var.
Tokens are never printed or logged.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List, Optional

from src.models import Track
from src.yandex_client import (
    YandexAuthError,
    fetch_track_details,
    get_client,
    iter_liked_raw,
)

DEFAULT_JSON = Path(__file__).resolve().parent.parent / "data" / "yandex_favorites.json"
DEFAULT_CSV = Path(__file__).resolve().parent.parent / "data" / "yandex_favorites.csv"

CSV_FIELDS = ["yandex_id", "title", "artists", "album", "duration_ms", "isrc",
              "cover_url", "source"]


def _redact(text: str) -> str:
    token = os.environ.get("YANDEX_MUSIC_TOKEN", "")
    if token and token in text:
        text = text.replace(token, "<redacted>")
    return text


def export_likes(client, delay: float = 0.3, limit: Optional[int] = None) -> List[Track]:
    """Fetch liked tracks via the adapter and map them to :class:`Track`."""
    tracks: List[Track] = []
    for raw in iter_liked_raw(client):
        if limit is not None and len(tracks) >= limit:
            break
        tracks.append(fetch_track_details(client, raw))
        if delay and delay > 0:
            time.sleep(delay)
    return tracks


def save_tracks(tracks: Iterable[Track], json_path, csv_path) -> None:
    """Write tracks to JSON + CSV, creating parent dirs as needed."""
    tracks = list(tracks)
    json_path = Path(json_path)
    csv_path = Path(csv_path)
    if json_path.parent and str(json_path.parent):
        json_path.parent.mkdir(parents=True, exist_ok=True)
    if csv_path.parent and str(csv_path.parent):
        csv_path.parent.mkdir(parents=True, exist_ok=True)

    with json_path.open("w", encoding="utf-8") as f:
        json.dump([asdict(t) for t in tracks], f, ensure_ascii=False, indent=2)
        f.write("\n")

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for t in tracks:
            writer.writerow({
                "yandex_id": t.yandex_id,
                "title": t.title,
                "artists": "; ".join(t.artists),
                "album": t.album,
                "duration_ms": t.duration_ms,
                "isrc": t.isrc,
                "cover_url": t.cover_url,
                "source": t.source,
            })


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Export Yandex Music liked tracks.")
    p.add_argument("--token", default=None,
                   help="Yandex Music OAuth token (fallback: YANDEX_MUSIC_TOKEN env).")
    p.add_argument("--out-json", default=str(DEFAULT_JSON),
                   help="Output JSON path (default: data/yandex_favorites.json).")
    p.add_argument("--out-csv", default=str(DEFAULT_CSV),
                   help="Output CSV path (default: data/yandex_favorites.csv).")
    p.add_argument("--verify-only", action="store_true",
                   help="Only count liked tracks; do not write files.")
    p.add_argument("--limit", type=int, default=None,
                   help="Export at most N tracks (smoke tests).")
    p.add_argument("--delay", type=float, default=0.3,
                   help="Polite delay in seconds between track fetches.")
    p.add_argument("--page-size", type=int, default=100,
                   help="Likes pagination page size.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    token = args.token or os.environ.get("YANDEX_MUSIC_TOKEN", "")
    if not token:
        print("Error: no Yandex token. Pass --token or set YANDEX_MUSIC_TOKEN.",
              file=sys.stderr)
        return 2
    try:
        client = get_client(token)
    except YandexAuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {_redact(str(exc))}", file=sys.stderr)
        return 1

    try:
        if args.verify_only:
            count = 0
            for raw in iter_liked_raw(client, page_size=args.page_size):
                fetch_track_details(client, raw)
                count += 1
                if args.limit is not None and count >= args.limit:
                    break
            print(f"Found {count} liked track(s) (verify-only, no files written).")
            return 0

        # Re-iterate via export_likes for the real dump (keeps one code path).
        # iter_liked_raw returns a fresh generator each call, so this is safe
        # for real clients; tests mock at this seam as well.
        tracks = export_likes(client, delay=args.delay, limit=args.limit)
        save_tracks(tracks, args.out_json, args.out_csv)
        print(f"Exported {len(tracks)} liked track(s) to "
              f"{args.out_json} and {args.out_csv}.")
        return 0
    except YandexAuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {_redact(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
