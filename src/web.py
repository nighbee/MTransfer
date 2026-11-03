"""MTransfer tiny web UI (FastAPI + MusicKit JS).

Reuses existing stage functions (imported, not duplicated):
- Yandex: ``src.yandex_client`` adapter + ``src.export_yandex`` helpers
- Match: ``src.match_apple.run``
- Apple tokens: ``src.extract_apple_tokens`` helpers
- Import: ``src.import_apple.run``

Secrets hygiene: tokens live in the in-memory ``TOKENS`` dict only,
never written to disk and never logged.

Run: ``uvicorn src.web:app`` (Windows OK).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from src import export_yandex, extract_apple_tokens, import_apple, match_apple, yandex_client
from src.yandex_client import YandexAuthError

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

YANDEX_JSON = DATA_DIR / "yandex_favorites.json"
YANDEX_CSV = DATA_DIR / "yandex_favorites.csv"
MATCHED_PATH = DATA_DIR / "matched_tracks.json"
UNMATCHED_PATH = DATA_DIR / "unmatched_tracks.json"
REPORT_PATH = DATA_DIR / "migration_report.txt"
JOB_PATH = DATA_DIR / "job.json"
DEV_TOKEN_PATH = BASE_DIR / ".developer_token"

# In-memory token store only. Never persisted to disk.
TOKENS: dict = {}
_TOKENS = TOKENS  # alias so tests may use either name

app = FastAPI(title="MTransfer")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _json_body(data) -> dict:
    if isinstance(data, dict):
        return data
    return {}


async def _parse_json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _read_json_list(path: Path) -> list:
    try:
        if not Path(path).exists():
            return []
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("matched", "results", "tracks", "data", "items"):
            val = raw.get(key)
            if isinstance(val, list):
                return val
        return []
    return []


def _read_text(path: Path) -> str:
    try:
        if Path(path).exists():
            return Path(path).read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def _extract_token(data: dict) -> str:
    for key in ("token", "user_token", "userToken", "media_user_token",
                "media-user-token", "mut", "yandex_token"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


# ---------------------------------------------------------------------------
# pages / health
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    if INDEX_HTML.exists():
        return FileResponse(str(INDEX_HTML), media_type="text/html")
    return JSONResponse({"detail": "index.html not found"}, status_code=404)


@app.get("/api/health")
def api_health():
    return {"status": "ok"}


# Alias so plain "GET /health" also works.
@app.get("/health", include_in_schema=False)
def health_alias():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Yandex
# ---------------------------------------------------------------------------

@app.post("/api/yandex/test")
async def yandex_test(request: Request):
    data = _json_body(await _parse_json(request))
    token = _extract_token(data)
    if not token:
        return JSONResponse({"detail": "token is required"}, status_code=400)
    try:
        client = yandex_client.get_client(token)
    except YandexAuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    except Exception as exc:
        logger.warning("yandex test: client setup failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "yandex client failed"}, status_code=500)
    try:
        count = 0
        for _raw in yandex_client.iter_liked_raw(client):
            count += 1
            if count > 100000:
                break
    except YandexAuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    except Exception as exc:
        logger.warning("yandex test: fetch failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "yandex fetch failed"}, status_code=500)
    # Memory only — never written to disk, never logged.
    TOKENS["yandex"] = token
    TOKENS["yandex_token"] = token
    return {"ok": True, "count": count}


@app.post("/api/yandex/export")
async def yandex_export(request: Request):
    data = _json_body(await _parse_json(request))
    token = _extract_token(data) or TOKENS.get("yandex") or TOKENS.get("yandex_token") \
        or os.environ.get("YANDEX_MUSIC_TOKEN", "")
    if not token:
        return JSONResponse({"detail": "token is required"}, status_code=400)
    limit = data.get("limit")
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    try:
        client = yandex_client.get_client(token)
    except YandexAuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    except Exception as exc:
        logger.warning("yandex export: client setup failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "yandex client failed"}, status_code=500)
    try:
        tracks = export_yandex.export_likes(client, delay=0, limit=limit)
        export_yandex.save_tracks(tracks, str(YANDEX_JSON), str(YANDEX_CSV))
    except YandexAuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    except Exception as exc:
        logger.warning("yandex export failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "yandex export failed"}, status_code=500)
    TOKENS["yandex"] = token
    TOKENS["yandex_token"] = token
    return {"ok": True, "exported": len(tracks),
            "json": str(YANDEX_JSON), "csv": str(YANDEX_CSV)}


# ---------------------------------------------------------------------------
# Match
# ---------------------------------------------------------------------------

@app.post("/api/match/run")
async def match_run(request: Request):
    data = _json_body(await _parse_json(request))
    limit = data.get("limit")
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    try:
        summary = match_apple.run(
            str(YANDEX_JSON), str(MATCHED_PATH), str(UNMATCHED_PATH),
            str(REPORT_PATH), limit=limit, resume_job=str(JOB_PATH),
        )
    except Exception as exc:
        logger.warning("match run failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "match failed"}, status_code=500)
    return summary


@app.get("/api/match/results")
def match_results():
    matched = _read_json_list(MATCHED_PATH)
    unmatched = _read_json_list(UNMATCHED_PATH)
    return {
        "matched": matched,
        "unmatched": unmatched,
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "total": len(matched) + len(unmatched),
    }


@app.get("/api/match/unmatched.csv", include_in_schema=False)
def match_unmatched_csv():
    unmatched = _read_json_list(UNMATCHED_PATH)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["yandex_id", "title", "artists",
                                             "duration_ms", "isrc", "reason",
                                             "confidence"])
    writer.writeheader()
    for u in unmatched:
        if not isinstance(u, dict):
            continue
        artists = u.get("artists", [])
        if isinstance(artists, list):
            artists = "; ".join(str(a) for a in artists)
        writer.writerow({
            "yandex_id": u.get("yandex_id", ""),
            "title": u.get("title", ""),
            "artists": artists,
            "duration_ms": u.get("duration_ms", 0),
            "isrc": u.get("isrc", ""),
            "reason": u.get("reason", ""),
            "confidence": u.get("confidence", 0.0),
        })
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition":
                                      "attachment; filename=unmatched.csv"})


# ---------------------------------------------------------------------------
# Apple tokens
# ---------------------------------------------------------------------------

@app.get("/api/apple/dev-token/status")
def dev_token_status():
    present = False
    if TOKENS.get("dev"):
        present = True
    else:
        try:
            extract_apple_tokens.load_token(str(DEV_TOKEN_PATH))
            present = True
        except Exception:
            present = False
    return {"present": present}


@app.post("/api/apple/user-token")
async def apple_user_token(request: Request):
    data = _json_body(await _parse_json(request))
    token = _extract_token(data)
    if not token:
        return JSONResponse({"detail": "token is required"}, status_code=400)
    # Memory only: never written to disk, never logged.
    TOKENS["user"] = token
    TOKENS["user_token"] = token
    return {"ok": True, "stored": True}


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _import_tokens() -> tuple:
    dev = TOKENS.get("dev") or TOKENS.get("dev_token")
    user = TOKENS.get("user") or TOKENS.get("user_token")
    return dev, user


@app.post("/api/import/dry-run")
async def import_dry_run(request: Request):
    data = _json_body(await _parse_json(request))
    matched = data.get("matched_json") or data.get("matched") or str(MATCHED_PATH)
    playlist = data.get("playlist") or data.get("playlist_name")
    dev, user = _import_tokens()
    try:
        stats = import_apple.run(
            matched, playlist_name=playlist, dry_run=True,
            dev_token=dev, user_token=user,
        )
    except FileNotFoundError as exc:
        return JSONResponse({"detail": f"matched file not found: {exc.filename or matched}"},
                            status_code=404)
    except Exception as exc:
        logger.warning("import dry-run failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "import dry-run failed"}, status_code=500)
    return stats


@app.post("/api/import/run")
async def import_run(request: Request):
    data = _json_body(await _parse_json(request))
    matched = data.get("matched_json") or data.get("matched") or str(MATCHED_PATH)
    playlist = data.get("playlist") or data.get("playlist_name")
    from_n = data.get("from_n", data.get("from", 0))
    try:
        from_n = int(from_n or 0)
    except (TypeError, ValueError):
        from_n = 0
    dev, user = _import_tokens()
    try:
        stats = import_apple.run(
            matched, playlist_name=playlist, dry_run=False, from_n=from_n,
            dev_token=dev, user_token=user,
        )
    except FileNotFoundError as exc:
        return JSONResponse({"detail": f"matched file not found: {exc.filename or matched}"},
                            status_code=404)
    except Exception as exc:
        logger.warning("import run failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "import failed"}, status_code=500)
    return stats


@app.post("/api/import/retry")
async def import_retry(request: Request):
    data = _json_body(await _parse_json(request))
    matched = data.get("matched_json") or data.get("matched") or str(MATCHED_PATH)
    playlist = data.get("playlist") or data.get("playlist_name")
    dev, user = _import_tokens()
    try:
        stats = import_apple.run(
            matched, playlist_name=playlist, dry_run=False, only_failed=True,
            dev_token=dev, user_token=user,
        )
    except FileNotFoundError as exc:
        return JSONResponse({"detail": f"matched file not found: {exc.filename or matched}"},
                            status_code=404)
    except Exception as exc:
        logger.warning("import retry failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "import retry failed"}, status_code=500)
    return stats


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@app.get("/api/report")
def report():
    text = _read_text(REPORT_PATH)
    matched = _read_json_list(MATCHED_PATH)
    unmatched = _read_json_list(UNMATCHED_PATH)
    imported = 0
    # Best-effort: derive imported count from matched when no import ran yet.
    return {
        "report": text,
        "matched": len(matched),
        "unmatched": len(unmatched),
        "total": len(matched) + len(unmatched),
        "imported": imported,
    }
