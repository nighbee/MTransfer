"""MTransfer tiny web UI (FastAPI + MusicKit JS).

User-friendly guided wizard:
- Step 1: Yandex login via device code (no token pasting) + export
- Step 2: Matching with live progress
- Step 3: Apple connect (auto dev-token + assisted user-token) + dry-run
- Step 4: Transfer with progress + report

Long operations run as background jobs with progress polling.

Secrets hygiene: tokens live in the in-memory ``TOKENS`` dict only,
never written to disk and never logged.

Run: ``uvicorn src.web:app`` (Windows OK) or ``python -m src.web``.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
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
# background jobs with progress
# ---------------------------------------------------------------------------

@dataclass
class JobState:
    job_id: str
    kind: str  # export | match | import | retry | favorites
    status: str = "running"  # running | done | error
    done: int = 0
    total: int = 0
    result: dict = field(default_factory=dict)
    error: str = ""


JOBS: dict[str, JobState] = {}
JOBS_LOCK = threading.Lock()


def _job_new(kind: str) -> JobState:
    job = JobState(job_id=uuid.uuid4().hex[:12], kind=kind)
    with JOBS_LOCK:
        JOBS[job.job_id] = job
    return job


def _job_progress(job_id: str, done: int, total) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.done = done
            job.total = total or 0


def _job_finish(job_id: str, result: dict) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.status = "done"
            job.result = result


def _job_fail(job_id: str, error: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.status = "error"
            job.error = error


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return JSONResponse({"detail": "unknown job"}, status_code=404)
        return {
            "job_id": job.job_id, "kind": job.kind, "status": job.status,
            "done": job.done, "total": job.total,
            "result": job.result, "error": job.error,
        }


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
# Yandex: device-code login (no token pasting) + export
# ---------------------------------------------------------------------------

def _yandex_token() -> str:
    return (TOKENS.get("yandex") or TOKENS.get("yandex_token")
            or os.environ.get("YANDEX_MUSIC_TOKEN", ""))


@app.post("/api/yandex/device/start")
async def yandex_device_start():
    """Step 1: request a device code. User enters it at ya.ru/device."""
    try:
        from yandex_music import Client
    except ImportError:
        return JSONResponse({"detail": "yandex-music not installed"}, status_code=500)
    try:
        code = Client().request_device_code()
    except Exception as exc:
        logger.warning("device code request failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "could not start Yandex login"}, status_code=500)
    TOKENS["yandex_device_code"] = code.device_code
    return {"verification_url": code.verification_url,
            "user_code": code.user_code}


@app.post("/api/yandex/device/poll")
async def yandex_device_poll():
    """Step 2: check whether the user confirmed; store token in memory."""
    device_code = TOKENS.get("yandex_device_code", "")
    if not device_code:
        return JSONResponse({"detail": "no login started"}, status_code=400)
    try:
        from yandex_music import Client
    except ImportError:
        return JSONResponse({"detail": "yandex-music not installed"}, status_code=500)
    try:
        token = Client().poll_device_token(device_code)
    except Exception as exc:
        logger.warning("device poll failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "login failed, restart"}, status_code=500)
    if token is None:
        return {"confirmed": False}
    access = token.access_token
    TOKENS["yandex"] = access
    TOKENS["yandex_token"] = access
    TOKENS.pop("yandex_device_code", None)
    # Resolve login name for a friendly greeting (never log the token).
    try:
        client = yandex_client.get_client(access)
        login = client.me.account.login if client.me and client.me.account else ""
    except Exception:
        login = ""
    return {"confirmed": True, "login": login or "Yandex user"}


@app.post("/api/yandex/test")
async def yandex_test(request: Request):
    data = _json_body(await _parse_json(request))
    token = _extract_token(data) or _yandex_token()
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
    token = _extract_token(data) or _yandex_token()
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
# Match (background job with progress)
# ---------------------------------------------------------------------------

def _run_match_job(job_id: str, limit) -> None:
    try:
        summary = match_apple.run(
            str(YANDEX_JSON), str(MATCHED_PATH), str(UNMATCHED_PATH),
            str(REPORT_PATH), limit=limit, resume_job=str(JOB_PATH),
            progress=lambda done, total: _job_progress(job_id, done, total),
        )
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                job.done = summary.get("total", 0)
                job.total = summary.get("total", 0)
        _job_finish(job_id, summary)
    except Exception as exc:
        logger.warning("match job failed: %s", type(exc).__name__)
        _job_fail(job_id, "match failed")


@app.post("/api/match/run")
async def match_run(request: Request):
    data = _json_body(await _parse_json(request))
    limit = data.get("limit")
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    job = _job_new("match")
    t = threading.Thread(target=_run_match_job, args=(job.job_id, limit), daemon=True)
    t.start()
    return {"job_id": job.job_id}


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
# Apple tokens: auto dev-token + assisted user-token
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


@app.post("/api/apple/dev-token/refresh")
async def dev_token_refresh():
    """One click: scrape the web dev-token and keep it in memory."""
    try:
        jwt = extract_apple_tokens.fetch_developer_token_from_web()
    except Exception as exc:
        logger.warning("dev token refresh failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "could not fetch developer token"}, status_code=500)
    TOKENS["dev"] = jwt
    TOKENS["dev_token"] = jwt
    return {"ok": True, "present": True}


@app.post("/api/apple/user-token")
async def apple_user_token(request: Request):
    data = _json_body(await _parse_json(request))
    token = _extract_token(data)
    if not token:
        return JSONResponse({"detail": "token is required"}, status_code=400)
    # Memory only: never written to disk, never logged.
    TOKENS["user"] = token
    TOKENS["user_token"] = token
    # Friendly check: resolve the storefront to prove the combo works.
    dev = _import_tokens()[0]
    storefront = ""
    if dev:
        try:
            storefront = import_apple.get_storefront(dev, token)
        except Exception:
            storefront = ""
    return {"ok": True, "stored": True, "storefront": storefront}


# ---------------------------------------------------------------------------
# Import (background job with progress)
# ---------------------------------------------------------------------------

def _import_tokens() -> tuple:
    dev = TOKENS.get("dev") or TOKENS.get("dev_token")
    if not dev:
        try:
            dev = extract_apple_tokens.load_token(str(DEV_TOKEN_PATH))
        except Exception:
            dev = None
    user = TOKENS.get("user") or TOKENS.get("user_token")
    return dev, user


def _run_import_job(job_id: str, matched, playlist, dry_run, from_n, only_failed,
                    favorites_only: bool = False) -> None:
    try:
        dev, user = _import_tokens()
        if favorites_only:
            # Favorites-only pass: apply ratings for every matched id.
            entries = import_apple.load_matched_entries(matched)
            ids = [import_apple._entry_apple_id(e) for e in entries]
            ids = [i for i in ids if i]
            total = len(ids)
            _job_progress(job_id, 0, total)
            res = {"added": [], "failed": []}
            BATCH = 25
            for start in range(0, len(ids), BATCH):
                chunk = ids[start:start + BATCH]
                part = import_apple.set_favorites(dev, user, chunk)
                res["added"].extend(part.get("added", []))
                res["failed"].extend(part.get("failed", []))
                _job_progress(job_id, len(res["added"]) + len(res["failed"]), total)
            _job_finish(job_id, {"favorites_set": len(res["added"]),
                                 "failed": res["failed"], "total": total})
            return
        stats = import_apple.run(
            matched, playlist_name=playlist, dry_run=dry_run, from_n=from_n,
            only_failed=only_failed, dev_token=dev, user_token=user,
        )
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                total = stats.get("total", 0) or stats.get("considered", 0)
                job.done = stats.get("imported", 0) + stats.get("skipped", 0)
                job.total = total
        _job_finish(job_id, stats)
    except FileNotFoundError as exc:
        _job_fail(job_id, f"matched file not found: {exc.filename or matched}")
    except Exception as exc:
        logger.warning("import job failed: %s", type(exc).__name__)
        _job_fail(job_id, "import failed")


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


def _start_import_job(data: dict, only_failed=False, favorites_only=False):
    matched = data.get("matched_json") or data.get("matched") or str(MATCHED_PATH)
    playlist = data.get("playlist") or data.get("playlist_name")
    from_n = data.get("from_n", data.get("from", 0))
    try:
        from_n = int(from_n or 0)
    except (TypeError, ValueError):
        from_n = 0
    kind = "favorites" if favorites_only else ("retry" if only_failed else "import")
    job = _job_new(kind)
    t = threading.Thread(
        target=_run_import_job,
        args=(job.job_id, matched, playlist, False, from_n, only_failed, favorites_only),
        daemon=True,
    )
    t.start()
    return {"job_id": job.job_id}


@app.post("/api/import/run")
async def import_run(request: Request):
    return _start_import_job(_json_body(await _parse_json(request)))


@app.post("/api/import/retry")
async def import_retry(request: Request):
    data = _json_body(await _parse_json(request))
    return _start_import_job(data, only_failed=True)


@app.post("/api/import/favorites")
async def import_favorites(request: Request):
    """Favorites-only pass (ratings), no playlist touched."""
    data = _json_body(await _parse_json(request))
    return _start_import_job(data, favorites_only=True)


# ---------------------------------------------------------------------------
# Progress + report
# ---------------------------------------------------------------------------

@app.get("/api/progress")
def progress():
    """One-shot status snapshot for the wizard header: export/match/import."""
    with JOBS_LOCK:
        jobs = [
            {"job_id": j.job_id, "kind": j.kind, "status": j.status,
             "done": j.done, "total": j.total}
            for j in JOBS.values()
        ]
    return {
        "exported": len(_read_json_list(YANDEX_JSON)),
        "matched": len(_read_json_list(MATCHED_PATH)),
        "unmatched": len(_read_json_list(UNMATCHED_PATH)),
        "jobs": jobs[-5:],
    }


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


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("src.web:app", host="127.0.0.1", port=8000, reload=False)
