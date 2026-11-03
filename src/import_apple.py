"""Apple Music library import stage.

- Resolves storefront, reuses/creates playlist, batch-adds tracks (<=50),
  sets favorites, skips already-present tracks, records per-track failures.
- ``run()`` supports ``dry_run`` (zero POST/PUT), ``from_n`` resume and
  ``only_failed`` retry.
- Secrets hygiene: never log raw token values — use :func:`redact`.
- All HTTP via local :func:`_request_with_retry` (own module-local helper).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

AMP_BASE = "https://amp-api.music.apple.com"
BATCH_SIZE = 50

# Same browser headers as extract_apple_tokens: amp-api 401s without Origin.
try:
    from src.extract_apple_tokens import WEB_HEADERS
except Exception:  # pragma: no cover - standalone use
    WEB_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Origin": "https://beta.music.apple.com",
        "Referer": "https://beta.music.apple.com/",
    }

logger = logging.getLogger(__name__)


def redact(token: str | None) -> str:
    """Redact a secret for logs: first 6 chars + '...'."""
    if not token:
        return "***"
    s = str(token)
    if len(s) <= 6:
        return "***"
    return s[:6] + "..."


def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json_data: dict | list | None = None,
    params: dict | None = None,
    timeout: int = 15,
    max_retries: int = 3,
    backoff_factor: float = 0.8,
):
    """Local retry helper with backoff on 429/5xx. Never logs header values."""
    if requests is None:  # pragma: no cover
        raise RuntimeError("The 'requests' package is required (pip install -r requirements.txt)")
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.request(
                method.upper(), url, headers=headers, json=json_data, params=params, timeout=timeout
            )
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(backoff_factor * (2**attempt))
                continue
            raise
        if resp.status_code == 429 or 500 <= resp.status_code <= 599:
            if attempt < max_retries - 1:
                retry_after = None
                try:
                    retry_after = resp.headers.get("Retry-After")
                except Exception:
                    retry_after = None
                if retry_after:
                    try:
                        time.sleep(float(retry_after))
                    except Exception:
                        time.sleep(backoff_factor * (2**attempt))
                else:
                    time.sleep(backoff_factor * (2**attempt))
                continue
        return resp
    if last_exc is not None:  # pragma: no cover
        raise last_exc
    raise RuntimeError(f"Request failed after {max_retries} attempts: {method} {url}")


def _headers(dev_token: str, user_token: str) -> dict:
    return {
        "Authorization": f"Bearer {dev_token}",
        "Media-User-Token": user_token,
        "Accept": "application/json",
        "Content-Type": "application/json",
        **WEB_HEADERS,
    }


def _parse_storefront(payload: dict) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and first.get("id"):
            return str(first["id"])
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"])
    for key in ("storefront", "storefrontId", "id"):
        if isinstance(payload, dict) and payload.get(key):
            return str(payload[key])
    raise ValueError("Storefront not found in response")


def get_storefront(dev_token: str, user_token: str) -> str:
    """Resolve the user storefront via ``GET /v1/me/storefront``."""
    url = f"{AMP_BASE}/v1/me/storefront"
    logger.info("Resolving storefront (dev=%s)", redact(dev_token))
    resp = _request_with_retry("GET", url, headers=_headers(dev_token, user_token), timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"get_storefront failed: HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except Exception as exc:
        raise RuntimeError(f"get_storefront: invalid JSON: {exc}") from exc
    return _parse_storefront(payload)


def _iter_playlists(dev_token: str, user_token: str, limit: int = 100):
    offset = 0
    while True:
        url = f"{AMP_BASE}/v1/me/library/playlists"
        params = {"limit": limit, "offset": offset}
        resp = _request_with_retry(
            "GET", url, headers=_headers(dev_token, user_token), params=params, timeout=15
        )
        if resp.status_code != 200:
            raise RuntimeError(f"list playlists failed: HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except Exception as exc:
            raise RuntimeError(f"list playlists: invalid JSON: {exc}") from exc
        items = payload.get("data", []) if isinstance(payload, dict) else []
        if not items:
            break
        for item in items:
            yield item
        nxt = payload.get("next") if isinstance(payload, dict) else None
        if not nxt and len(items) < limit:
            break
        offset += len(items)
        if offset > 10000:  # safety guard
            break


def find_playlist_by_name(dev_token: str, user_token: str, name: str) -> str | None:
    """Return playlist id matching ``name``, or None."""
    logger.info("Searching playlist by name: %r", name)
    for item in _iter_playlists(dev_token, user_token):
        attrs = item.get("attributes", {}) if isinstance(item, dict) else {}
        if attrs.get("name") == name:
            return str(item.get("id"))
    return None


def create_playlist(
    dev_token: str, user_token: str, name: str, description: str = "Imported by MTransfer"
) -> str:
    """Create a library playlist; returns its id."""
    url = f"{AMP_BASE}/v1/me/library/playlists"
    body = {"attributes": {"name": name, "description": description}}
    logger.info("Creating playlist %r (dev=%s)", name, redact(dev_token))
    resp = _request_with_retry(
        "POST", url, headers=_headers(dev_token, user_token), json_data=body, timeout=15
    )
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(f"create_playlist failed: HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except Exception as exc:
        raise RuntimeError(f"create_playlist: invalid JSON: {exc}") from exc
    data = payload.get("data", []) if isinstance(payload, dict) else []
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("id"):
        return str(data[0]["id"])
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"])
    if isinstance(payload, dict) and payload.get("id"):
        return str(payload["id"])
    raise RuntimeError("create_playlist: no playlist id in response")


def list_playlist_tracks(dev_token: str, user_token: str, playlist_id: str) -> list[str]:
    """List song ids already in a playlist (paginated)."""
    out: list[str] = []
    offset = 0
    limit = 100
    while True:
        url = f"{AMP_BASE}/v1/me/library/playlists/{playlist_id}/tracks"
        params = {"limit": limit, "offset": offset}
        resp = _request_with_retry(
            "GET", url, headers=_headers(dev_token, user_token), params=params, timeout=15
        )
        if resp.status_code != 200:
            raise RuntimeError(f"list_playlist_tracks failed: HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except Exception as exc:
            raise RuntimeError(f"list_playlist_tracks: invalid JSON: {exc}") from exc
        items = payload.get("data", []) if isinstance(payload, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                out.append(str(item["id"]))
        nxt = payload.get("next") if isinstance(payload, dict) else None
        if not nxt and len(items) < limit:
            break
        if not items:
            break
        offset += len(items)
        if offset > 100000:  # safety guard
            break
    return out


def _chunks(ids: list[str], size: int = BATCH_SIZE):
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def add_tracks(
    dev_token: str, user_token: str, playlist_id: str, track_ids: list[str]
) -> dict:
    """Add song ids to a playlist in chunks of <=50.

    Returns ``{"added": [...], "failed": [{"apple_id":..., "reason":...}]}``.
    A chunk that returns non-2xx (after retries) marks all its ids failed.
    """
    added: list[str] = []
    failed: list[dict] = []
    ids = [str(t) for t in (track_ids or []) if str(t)]
    if not ids:
        return {"added": [], "failed": []}
    for chunk in _chunks(ids, BATCH_SIZE):
        assert len(chunk) <= BATCH_SIZE, "chunk exceeds Apple batch limit"
        url = f"{AMP_BASE}/v1/me/library/playlists/{playlist_id}/tracks"
        body = {"data": [{"id": tid, "type": "songs"} for tid in chunk]}
        try:
            resp = _request_with_retry(
                "POST", url, headers=_headers(dev_token, user_token), json_data=body, timeout=15
            )
        except Exception as exc:
            for tid in chunk:
                failed.append({"apple_id": tid, "reason": f"request-error: {exc}"})
            continue
        if resp.status_code in (200, 201, 202, 204):
            added.extend(chunk)
        else:
            for tid in chunk:
                failed.append({"apple_id": tid, "reason": f"HTTP {resp.status_code}"})
    return {"added": added, "failed": failed}


def set_favorites(dev_token: str, user_token: str, track_ids: list[str]) -> dict:
    """Mark song ids as favorites.

    Correct endpoint (probed 2026-11-03): one PUT per track to
    ``/v1/me/ratings/songs/{id}`` with body ``{"attributes": {"value": 1}}``.
    The bulk ``PUT /v1/me/ratings/songs`` returns 405 — not supported.
    """
    added: list[str] = []
    failed: list[dict] = []
    ids = [str(t) for t in (track_ids or []) if str(t)]
    for tid in ids:
        url = f"{AMP_BASE}/v1/me/ratings/songs/{tid}"
        body = {"attributes": {"value": 1}}
        try:
            resp = _request_with_retry(
                "PUT", url, headers=_headers(dev_token, user_token),
                json_data=body, timeout=15,
            )
        except Exception as exc:
            failed.append({"apple_id": tid, "reason": f"request-error: {exc}"})
            continue
        if resp.status_code in (200, 201, 202, 204):
            added.append(tid)
        else:
            failed.append({"apple_id": tid, "reason": f"HTTP {resp.status_code}"})
    return {"added": added, "failed": failed}


def _load_token_file(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Token file not found: {p}. Run extract_apple_tokens first or pass tokens explicitly."
        )
    value = p.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"Token file is empty: {p}")
    return value


def load_matched_entries(matched_json: str | Path) -> list[dict]:
    """Load matched entries; accepts a JSON list or a dict with list inside."""
    p = Path(matched_json)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if isinstance(raw, dict):
        for key in ("matched", "results", "tracks", "data"):
            if isinstance(raw.get(key), list):
                return [e for e in raw[key] if isinstance(e, dict)]
        # Single entry dict?
        if "apple_id" in raw:
            return [raw]
    raise ValueError(f"Unsupported matched JSON format in {p}")


def _entry_apple_id(entry: dict) -> str:
    for key in ("apple_id", "appleId", "id", "song_id"):
        if entry.get(key):
            return str(entry[key])
    return ""


def _entry_yandex_id(entry: dict) -> str:
    for key in ("yandex_id", "yandexId", "source_id"):
        if entry.get(key) is not None:
            return str(entry[key])
    return ""


def _default_failed_path(matched_json: str | Path) -> Path:
    return Path(matched_json).parent / "import_failed.json"


def _load_failed_ids(failed_json: str | Path) -> set[str]:
    p = Path(failed_json)
    if not p.exists():
        return set()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return set()
    items = raw if isinstance(raw, list) else raw.get("failed", []) if isinstance(raw, dict) else []
    ids: set[str] = set()
    for e in items:
        if isinstance(e, dict):
            for key in ("apple_id", "appleId", "id"):
                if e.get(key):
                    ids.add(str(e[key]))
                    break
            else:
                continue
        elif isinstance(e, (str, int)):
            ids.add(str(e))
    return ids


def run(
    matched_json: str | Path,
    playlist_name: str | None = None,
    dry_run: bool = False,
    from_n: int = 0,
    only_failed: bool = False,
    *,
    dev_token: str | None = None,
    user_token: str | None = None,
    dev_token_path: str | Path = ".developer_token",
    user_token_path: str | Path = ".media_user_token",
    failed_json: str | Path | None = None,
    description: str = "Imported by MTransfer",
) -> dict:
    """Run the import stage.

    - ``matched_json``: path to matched_tracks.json (list of {yandex_id, apple_id}).
    - ``playlist_name``: target playlist; defaults to ``Yandex Import YYYY-MM-DD``.
    - ``dry_run``: compute pending changes, perform zero POST/PUT.
    - ``from_n``: resume offset (skip first N entries).
    - ``only_failed``: reprocess only ids listed in the failed file.
    Returns stats dict with imported/skipped/failed + reasons.
    """
    if playlist_name is None:
        playlist_name = f"Yandex Import {date.today().isoformat()}"
    entries = load_matched_entries(matched_json)
    total_loaded = len(entries)

    failed_path = Path(failed_json) if failed_json else _default_failed_path(matched_json)
    if only_failed:
        wanted = _load_failed_ids(failed_path)
        entries = [e for e in entries if _entry_apple_id(e) in wanted]
    if from_n:
        entries = entries[from_n:]

    pending_ids: list[str] = []
    missing_no_apple: list[dict] = []
    for e in entries:
        aid = _entry_apple_id(e)
        if not aid:
            missing_no_apple.append(
                {
                    "apple_id": "",
                    "yandex_id": _entry_yandex_id(e),
                    "reason": "missing apple_id (unmatched entry)",
                }
            )
        else:
            pending_ids.append(aid)

    if dry_run:
        # Zero mutations: no network, no file writes.
        logger.info(
            "Dry-run: playlist %r, %d tracks pending, %d favorites (dev=%s)",
            playlist_name,
            len(pending_ids),
            len(pending_ids),
            redact(dev_token),
        )
        return {
            "playlist_id": None,
            "playlist_name": playlist_name,
            "total": total_loaded,
            "considered": len(entries),
            "would_add": len(pending_ids),
            "would_favorite": len(pending_ids),
            "skipped": 0,
            "skipped_missing_apple_id": len(missing_no_apple),
            "imported": 0,
            "failed": missing_no_apple,
            "dry_run": True,
        }

    # Real run: resolve tokens (explicit args win over dot-files).
    dev = dev_token or _load_token_file(dev_token_path)
    user = user_token or _load_token_file(user_token_path)

    pid = find_playlist_by_name(dev, user, playlist_name)
    if pid is None:
        pid = create_playlist(dev, user, playlist_name, description=description)
    else:
        logger.info("Reusing existing playlist %r (id=%s)", playlist_name, pid)

    try:
        existing = set(list_playlist_tracks(dev, user, pid))
    except Exception as exc:
        logger.warning("Could not list playlist tracks: %s", exc)
        existing = set()

    to_add = [tid for tid in pending_ids if tid not in existing]
    skipped_present = len(pending_ids) - len(to_add)

    add_res = add_tracks(dev, user, pid, to_add)
    fav_res = set_favorites(dev, user, add_res.get("added", []))

    failed: list[dict] = []
    # Attach yandex_id to failures where known.
    aid_to_yid = {_entry_apple_id(e): _entry_yandex_id(e) for e in entries}
    for f in add_res.get("failed", []):
        failed.append(
            {
                "apple_id": f.get("apple_id", ""),
                "yandex_id": aid_to_yid.get(str(f.get("apple_id", "")), ""),
                "reason": f.get("reason", "add failed"),
            }
        )
    for f in fav_res.get("failed", []):
        failed.append(
            {
                "apple_id": f.get("apple_id", ""),
                "yandex_id": aid_to_yid.get(str(f.get("apple_id", "")), ""),
                "reason": f"favorite failed: {f.get('reason', '')}",
            }
        )
    failed.extend(missing_no_apple)

    imported = len(add_res.get("added", []))

    # Persist failed list for `retry --only-failed`.
    if failed:
        try:
            failed_path.parent.mkdir(parents=True, exist_ok=True)
            failed_path.write_text(json.dumps(failed, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not write failed file %s: %s", failed_path, exc)
    else:
        # Clear stale failed file on full success.
        try:
            if failed_path.exists():
                failed_path.unlink()
        except Exception:
            pass

    logger.info(
        "Import done: playlist %r id=%s imported=%d skipped=%d failed=%d (dev=%s)",
        playlist_name,
        pid,
        imported,
        skipped_present,
        len(failed),
        redact(dev),
    )
    return {
        "playlist_id": pid,
        "playlist_name": playlist_name,
        "total": total_loaded,
        "considered": len(entries),
        "imported": imported,
        "skipped": skipped_present,
        "failed": failed,
        "dry_run": False,
    }


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import matched tracks into Apple Music")
    parser.add_argument("matched_json", nargs="?", default="data/matched_tracks.json")
    parser.add_argument("--playlist", default=None, help="Playlist name (default: Yandex Import YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without any POST/PUT mutations")
    parser.add_argument("--from", dest="from_n", type=int, default=0, help="Resume offset (skip first N)")
    parser.add_argument("--retry-only-failed", dest="only_failed", action="store_true")
    parser.add_argument("--only-failed", dest="only_failed", action="store_true")
    parser.add_argument("--dev-token", default=None)
    parser.add_argument("--user-token", default=None)
    parser.add_argument("--dev-token-path", default=".developer_token")
    parser.add_argument("--user-token-path", default=".media_user_token")
    parser.add_argument("--failed-json", default=None)
    parser.add_argument("--description", default="Imported by MTransfer")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        stats = run(
            args.matched_json,
            playlist_name=args.playlist or f"Yandex Import {date.today().isoformat()}",
            dry_run=args.dry_run,
            from_n=args.from_n,
            only_failed=args.only_failed,
            dev_token=args.dev_token,
            user_token=args.user_token,
            dev_token_path=args.dev_token_path,
            user_token_path=args.user_token_path,
            failed_json=args.failed_json,
            description=args.description,
        )
    except FileNotFoundError as exc:
        print(f"Import failed: {exc}")
        return 1
    except RuntimeError as exc:
        print(f"Import failed: {exc}")
        return 1

    if stats.get("dry_run"):
        print(
            f"Dry-run: playlist {stats['playlist_name']!r}: "
            f"{stats['would_add']} tracks would be added, "
            f"{stats['would_favorite']} favorites would be set "
            f"({stats['considered']} considered of {stats['total']} total)."
        )
    else:
        print(
            f"Import: playlist {stats['playlist_name']!r} (id={stats['playlist_id']}): "
            f"imported={stats['imported']} skipped={stats['skipped']} "
            f"failed={len(stats['failed'])}."
        )
        for f in stats["failed"][:20]:
            print(f"  FAILED apple_id={f.get('apple_id')} yandex_id={f.get('yandex_id')} reason={f.get('reason')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
