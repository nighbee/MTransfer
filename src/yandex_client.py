"""Adapter wrapping the ``yandex_music`` library (single seam for swaps).

Exposes:
  - :class:`YandexAuthError` — clean auth failure (401: re-login, no token leak).
  - :func:`get_client` — build an authenticated client from an OAuth token.
  - :func:`iter_liked_raw` — paginated generator yielding raw track dicts.
  - :func:`fetch_track_details` — map a raw dict/object to :class:`Track`.

Tokens are never embedded in exception messages or logs.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional

from src.models import Track, track_from_dict

__all__ = [
    "YandexAuthError",
    "get_client",
    "iter_liked_raw",
    "fetch_track_details",
]


class YandexAuthError(Exception):
    """Raised when Yandex rejects the token (HTTP 401)."""


_TOKEN_KV_RE = re.compile(r"(?i)(token\s*[:=]\s*)\S+")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)\S+")


def _redact(text: str, secrets: tuple = ()) -> str:
    for s in secrets:
        if s:
            text = text.replace(str(s), "<redacted>")
    text = _TOKEN_KV_RE.sub(r"\1<redacted>", text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    return text


def _is_auth_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    try:
        if status is not None and int(status) == 401:
            return True
    except (TypeError, ValueError):
        pass
    name = type(exc).__name__.lower()
    if "unauthor" in name and "error" in name:
        return True
    msg = str(exc).lower()
    return (
        "401" in msg
        or "unauthorized" in msg
        or "invalid token" in msg
        or "expired token" in msg
    )


def _auth_message() -> str:
    return (
        "Yandex auth failed (HTTP 401): token invalid or expired, "
        "please re-login and set a fresh YANDEX_MUSIC_TOKEN."
    )


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------

def get_client(token: str):
    """Build an authenticated ``yandex_music`` client.

    Raises :class:`YandexAuthError` (no token in the message) on 401 or when
    the token is missing.
    """
    if not token:
        raise YandexAuthError(
            "Yandex token is missing: pass --token or set YANDEX_MUSIC_TOKEN "
            "(obtain via the OAuth implicit flow), then re-login."
        )
    try:
        from yandex_music import Client  # local import: lib is optional for tests
    except ImportError as exc:
        raise RuntimeError(
            "The 'yandex-music' package is not installed. "
            "Run: pip install -r requirements.txt"
        ) from exc
    try:
        client = Client(token)
        init = getattr(client, "init", None)
        if callable(init):
            init()
        return client
    except YandexAuthError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise YandexAuthError(_auth_message()) from None
        raise RuntimeError(
            _redact(f"Failed to create Yandex client: {exc}", secrets=(token,))
        ) from None


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def _likes_method(client):
    for name in ("users_likes_tracks", "usersLikesTracks", "likes_tracks", "get_likes"):
        method = getattr(client, name, None)
        if callable(method):
            return method
    return None


def _fetch_page(client, method, page: int, page_size: int):
    try:
        return method(page=page, page_size=page_size)
    except TypeError:
        pass
    try:
        return method(page, page_size)
    except TypeError:
        pass
    if page == 0:
        return method()
    return []


def _page_items(res) -> Optional[List[Any]]:
    if res is None:
        return []
    if isinstance(res, dict):
        for key in ("tracks", "result", "items", "likes", "library"):
            val = res.get(key)
            if isinstance(val, (list, tuple)):
                return list(val)
        if "id" in res or "title" in res or "yandex_id" in res:
            return [res]
        return []
    if isinstance(res, (list, tuple)):
        return list(res)
    tracks = getattr(res, "tracks", None)
    if isinstance(tracks, (list, tuple)):
        return list(tracks)
    try:
        return list(res)  # generic iterable (paginator, etc.)
    except TypeError:
        return [res]


def _obj_to_dict(obj) -> Dict[str, Any]:
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    d: Dict[str, Any] = {}
    for attr in (
        "id", "track_id", "trackId", "realId", "title", "name",
        "duration_ms", "durationMs", "duration",
        "isrc", "ISRC", "cover_uri", "coverUri", "cover_url", "coverUrl",
        "cover", "og_image", "ogImage",
    ):
        try:
            val = getattr(obj, attr, None)
        except Exception:
            continue
        if val is not None and not callable(val):
            d[attr] = val
    for attr in ("artists", "albums", "album"):
        try:
            val = getattr(obj, attr, None)
        except Exception:
            continue
        if val is not None and not callable(val):
            d[attr] = val
    return d


def _coerce_raw(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        inner = item.get("track")
        if isinstance(inner, dict):
            return inner
        return item
    track_obj = getattr(item, "track", item)
    if isinstance(track_obj, dict):
        return track_obj
    if isinstance(track_obj, str):
        return {"id": track_obj}
    return _obj_to_dict(track_obj)


def iter_liked_raw(client, page_size: int = 100) -> Iterator[Dict[str, Any]]:
    """Yield raw liked-track dicts, combining paginated pages.

    Stops on the first empty page. If consecutive pages are identical (APIs
    or mocks that ignore pagination), stops after the first copy.
    """
    method = _likes_method(client)
    if method is None:
        raise RuntimeError("Yandex client has no users_likes_tracks method.")
    page = 0
    prev_key = None
    while True:
        if page > 10000:
            break
        try:
            res = _fetch_page(client, method, page, page_size)
        except YandexAuthError:
            raise
        except Exception as exc:
            if _is_auth_error(exc):
                raise YandexAuthError(_auth_message()) from None
            raise RuntimeError(_redact(f"Failed to fetch likes page: {exc}")) from None
        items = _page_items(res)
        if not items:
            break
        key = [repr(_coerce_raw(i)) for i in items]
        if prev_key is not None and key == prev_key:
            break  # non-paginated source repeating itself
        prev_key = key
        for item in items:
            yield _coerce_raw(item)
        page += 1


# ---------------------------------------------------------------------------
# Raw -> Track mapping
# ---------------------------------------------------------------------------

def _artists_to_list(raw_artists: Any) -> List[str]:
    if raw_artists is None:
        return []
    if isinstance(raw_artists, str):
        raw_artists = [raw_artists]
    out: List[str] = []
    for entry in list(raw_artists):
        if entry is None:
            continue
        if isinstance(entry, str):
            name = entry.strip()
        elif isinstance(entry, dict):
            name = str(
                entry.get("name") or entry.get("title") or entry.get("artist") or ""
            ).strip()
        else:
            name = str(getattr(entry, "name", entry) or "").strip()
        if name:
            out.append(name)
    return out


def _album_title(raw: Dict[str, Any]) -> str:
    album = raw.get("album")
    if isinstance(album, str) and album.strip():
        return album.strip()
    if isinstance(album, dict):
        t = album.get("title") or album.get("name") or ""
        if str(t).strip():
            return str(t).strip()
    albums = raw.get("albums")
    if isinstance(albums, (list, tuple)) and albums:
        first = albums[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
        if isinstance(first, dict):
            t = first.get("title") or first.get("name") or ""
            return str(t or "").strip()
        t = getattr(first, "title", None) or getattr(first, "name", None)
        if t:
            return str(t).strip()
    for key in ("album_title", "albumTitle"):
        if raw.get(key):
            return str(raw[key]).strip()
    return ""


def _duration_ms(raw: Dict[str, Any]) -> int:
    for key in ("duration_ms", "durationMs"):
        val = raw.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    val = raw.get("duration")
    if val is None:
        return 0
    try:
        num = float(val)
    except (TypeError, ValueError):
        return 0
    if num > 10000:  # already milliseconds
        return int(num)
    return int(num * 1000)


def _cover_url(raw: Dict[str, Any]) -> str:
    cover = (
        raw.get("cover_url")
        or raw.get("coverUrl")
        or raw.get("cover_uri")
        or raw.get("coverUri")
        or raw.get("cover")
        or raw.get("og_image")
        or raw.get("ogImage")
        or ""
    )
    if isinstance(cover, dict):
        cover = cover.get("uri") or cover.get("url") or ""
    if not cover:
        albums = raw.get("albums")
        if isinstance(albums, (list, tuple)) and albums:
            first = albums[0]
            if isinstance(first, dict):
                cover = first.get("coverUri") or first.get("cover_uri") or ""
    cover = str(cover or "").strip()
    if not cover:
        return ""
    if "%%" in cover:
        cover = cover.replace("%%", "400x400")
    if cover.startswith("//"):
        return "https:" + cover
    if cover.startswith("http://") or cover.startswith("https://"):
        return cover
    return "https://" + cover.lstrip("/")


def _try_enrich_by_id(client, track_id: str) -> Optional[Dict[str, Any]]:
    fetcher = getattr(client, "tracks", None)
    if not callable(fetcher):
        return None
    try:
        try:
            res = fetcher([track_id])
        except TypeError:
            res = fetcher(track_id)
    except Exception:
        return None
    first = res[0] if isinstance(res, (list, tuple)) and res else res
    if isinstance(first, dict):
        if isinstance(first.get("title"), str) and first["title"].strip():
            return first
        return None
    title = getattr(first, "title", None)
    if isinstance(title, str) and title.strip():
        return _obj_to_dict(first)
    return None


def fetch_track_details(client, raw: Any) -> Track:
    """Map one raw Yandex entry to a canonical :class:`Track`.

    Uses :func:`track_from_dict`; no network call when ``raw`` already
    carries the fields (extra lookup only for id-only dicts).
    """
    if raw is None:
        return Track()
    if isinstance(raw, str):
        enriched = _try_enrich_by_id(client, raw) if client is not None else None
        if enriched is not None:
            raw = enriched
        else:
            return Track(yandex_id=raw)
    if not isinstance(raw, dict):
        track_obj = getattr(raw, "track", raw)
        raw = track_obj if isinstance(track_obj, dict) else _obj_to_dict(track_obj)
    inner = raw.get("track")
    if isinstance(inner, dict) and not raw.get("title"):
        raw = inner
    if not raw.get("title") and raw.get("id") and client is not None:
        enriched = _try_enrich_by_id(client, str(raw["id"]))
        if enriched is not None:
            raw = enriched
    canonical = {
        "source": "yandex",
        "yandex_id": str(
            raw.get("yandex_id")
            or raw.get("id")
            or raw.get("track_id")
            or raw.get("trackId")
            or raw.get("realId")
            or ""
        ),
        "title": str(raw.get("title") or raw.get("name") or "").strip(),
        "artists": _artists_to_list(raw.get("artists") or raw.get("artist")),
        "album": _album_title(raw),
        "duration_ms": _duration_ms(raw),
        "isrc": str(raw.get("isrc") or raw.get("ISRC") or "").strip(),
        "cover_url": _cover_url(raw),
    }
    return track_from_dict(canonical)
