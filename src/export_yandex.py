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
    fetch_tracks_batch,
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


def _raw_id(raw) -> str:
    if isinstance(raw, dict):
        return str(raw.get("yandex_id") or raw.get("id") or "")
    rid = getattr(raw, "id", "")
    if rid:
        return str(rid)
    return str(raw or "")


def export_likes(client, delay: float = 0.3, limit: Optional[int] = None,
                 batch_size: int = 100, progress=None,
                 checkpoint=None) -> List[Track]:
    """Fetch liked tracks, resolving details in batches of ``batch_size``.

    ``progress(done, total_or_None)`` is called after each batch.
    ``checkpoint(tracks_so_far)`` is called after each batch so the caller
    can persist incrementally (survives Ctrl+C / network drops).
    """
    raws = list(iter_liked_raw(client))
    if limit is not None:
        raws = raws[:limit]
    tracks: List[Track] = []
    total = len(raws)
    for start in range(0, len(raws), max(1, batch_size)):
        chunk = raws[start:start + max(1, batch_size)]
        ids = [_raw_id(r) for r in chunk]
        details = fetch_tracks_batch(client, ids)
        for raw in chunk:
            bid = _raw_id(raw).split(":")[0].strip()
            hit = details.get(bid)
            if hit is not None:
                tracks.append(fetch_track_details(client, hit))
            else:
                tracks.append(fetch_track_details(client, raw))
        if progress:
            try:
                progress(len(tracks), total)
            except Exception:
                pass
        if checkpoint:
            try:
                checkpoint(list(tracks))
            except Exception:
                pass
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
                   help="Polite delay in seconds between batches.")
    p.add_argument("--batch-size", type=int, default=100,
                   help="Track-detail batch size (one API call per batch).")
    p.add_argument("--resume", action="store_true",
                   help="Resume: skip yandex_ids already present in out-json.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    token = args.token or os.environ.get("YANDEX_MUSIC_TOKEN", "")
    if not token:
        # Fall back to the device-flow token file (src.yandex_login --save).
        for candidate in (".yandex_token",
                          str(Path(__file__).resolve().parent.parent / ".yandex_token")):
            try:
                token = Path(candidate).read_text(encoding="utf-8").strip()
                if token:
                    break
            except OSError:
                continue
    if not token:
        print("Error: no Yandex token. Pass --token, set YANDEX_MUSIC_TOKEN, "
              "or run: python -m src.yandex_login",
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
            for _raw in iter_liked_raw(client):
                count += 1
                if args.limit is not None and count >= args.limit:
                    break
            print(f"Found {count} liked track(s) (verify-only, no files written).")
            return 0

        done_ids: set = set()
        if args.resume:
            try:
                with open(args.out_json, encoding="utf-8") as f:
                    for entry in json.load(f):
                        if isinstance(entry, dict) and entry.get("yandex_id"):
                            done_ids.add(str(entry["yandex_id"]))
                print(f"Resume: {len(done_ids)} track(s) already exported, skipping.")
            except (OSError, ValueError):
                pass
            if done_ids:
                # Skip already-done ids inside export by pre-seeding: wrap
                # iter_liked_raw via a filtering generator.
                _orig_iter = iter_liked_raw

                def _filtered(client, page_size: int = 100):
                    for raw in _orig_iter(client, page_size=page_size):
                        rid = _raw_id(raw).split(":")[0].strip()
                        if rid not in done_ids:
                            yield raw

                globals()["iter_liked_raw"] = _filtered
                _self_iter = _orig_iter

        def show_progress(done: int, total) -> None:
            print(f"  ... {done}/{total} tracks", flush=True)

        def checkpoint(tracks: list) -> None:
            if args.resume:
                save_tracks(tracks, args.out_json, args.out_csv)
            else:
                save_tracks(tracks, args.out_json + ".part", args.out_csv + ".part")

        tracks = export_likes(client, delay=args.delay, limit=args.limit,
                              batch_size=args.batch_size,
                              progress=show_progress, checkpoint=checkpoint)
        if args.resume and done_ids:
            globals()["iter_liked_raw"] = _self_iter
            # Merge previously exported entries back in.
            try:
                with open(args.out_json, encoding="utf-8") as f:
                    old = json.load(f)
                seen = {t.yandex_id for t in tracks}
                for entry in old:
                    if (isinstance(entry, dict) and entry.get("yandex_id")
                            and str(entry["yandex_id"]) not in seen):
                        tracks.append(Track(
                            source=entry.get("source", "yandex"),
                            yandex_id=str(entry["yandex_id"]),
                            title=entry.get("title", ""),
                            artists=list(entry.get("artists", []) or []),
                            album=entry.get("album", ""),
                            duration_ms=int(entry.get("duration_ms", 0) or 0),
                            isrc=entry.get("isrc", ""),
                            cover_url=entry.get("cover_url", ""),
                        ))
            except (OSError, ValueError):
                pass
        save_tracks(tracks, args.out_json, args.out_csv)
        # Remove partial files from non-resume runs.
        for part in (args.out_json + ".part", args.out_csv + ".part"):
            try:
                Path(part).unlink(missing_ok=True)
            except OSError:
                pass
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
