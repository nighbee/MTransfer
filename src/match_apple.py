"""Apple catalog matcher stage (iTunes Search API, no auth).

Lookup order:
  (a) ISRC via lookup?isrc= -> accept 1.0
  (b) search?term={artist title} -> score candidates, thresholds:
      auto >=0.85, review 0.6-0.85 (still matched, UI flags), miss <0.6.

Writes matched_tracks.json, unmatched_tracks.json (with reason),
migration_report.txt, and maintains a resume job file.
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from src.http_utils import throttled_get
from src.models import MatchResult
from src.normalize import norm, strip_feat

logger = logging.getLogger(__name__)

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"

AUTO_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.6

try:  # rapidfuzz when available, else difflib fallback
    from rapidfuzz import fuzz as _rf_fuzz

    def _text_sim(a: str, b: str) -> float:
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return float(_rf_fuzz.ratio(a, b)) / 100.0

    _USING_RAPIDFUZZ = True
except Exception:  # pragma: no cover - fallback path

    def _text_sim(a: str, b: str) -> float:
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return float(difflib.SequenceMatcher(None, a, b).ratio())

    _USING_RAPIDFUZZ = False


def _artist_overlap(track_artists, candidate_artist: str) -> float:
    """Jaccard overlap of normed artist tokens."""
    if isinstance(track_artists, str):
        track_artists = [track_artists]
    track_tokens: set[str] = set()
    for a in track_artists or []:
        track_tokens.update(norm(str(a)).split())
    cand_tokens = set(norm(str(candidate_artist or "")).split())
    if not track_tokens and not cand_tokens:
        return 1.0
    union = track_tokens | cand_tokens
    if not union:
        return 1.0
    return len(track_tokens & cand_tokens) / len(union)


def score_candidate(track: dict, candidate: dict) -> float:
    """Score a catalog candidate in [0, 1]."""
    raw_title = str(track.get("title", "") or "")
    cand_title = str(candidate.get("trackName", "") or "")
    title_a = norm(strip_feat(raw_title)[0])
    title_b = norm(strip_feat(cand_title)[0])
    title_sim = _text_sim(title_a, title_b)
    artist_sim = _artist_overlap(track.get("artists", []), candidate.get("artistName", ""))

    base = 0.7 * title_sim + 0.3 * artist_sim

    try:
        track_ms = int(track.get("duration_ms", 0) or 0)
    except (TypeError, ValueError):
        track_ms = 0
    try:
        cand_ms = int(candidate.get("trackTimeMillis", 0) or candidate.get("duration_ms", 0) or 0)
    except (TypeError, ValueError):
        cand_ms = 0

    if not track_ms or not cand_ms:
        return max(0.0, min(1.0, float(base)))

    diff_sec = abs(track_ms - cand_ms) / 1000.0
    if diff_sec <= 3.0:
        # Duration bonus: small lift, capped at 1.0.
        return max(0.0, min(1.0, float(base + 0.05)))
    # Duration penalty grows with distance, floored so text still matters.
    factor = max(0.5, 1.0 - (diff_sec - 3.0) / 60.0)
    return max(0.0, min(1.0, float(base * factor)))


def _results_from(resp) -> list:
    try:
        payload = resp.json()
    except Exception:
        return []
    if isinstance(payload, dict):
        results = payload.get("results", [])
        return list(results) if isinstance(results, list) else []
    return []


def search_isrc(isrc: str, delay: float = 3.0) -> list:
    code = str(isrc or "").strip()
    if not code:
        return []
    resp = throttled_get(ITUNES_LOOKUP_URL, params={"isrc": code}, delay=delay)
    return _results_from(resp)


def search_term(artist: str, title: str, delay: float = 3.0, limit: int = 5) -> list:
    artist = str(artist or "").strip()
    title = str(title or "").strip()
    if not artist and not title:
        return []
    term = f"{artist} {title}".strip()
    resp = throttled_get(
        ITUNES_SEARCH_URL,
        params={"term": term, "media": "music", "entity": "song", "limit": limit},
        delay=delay,
    )
    return _results_from(resp)


def match_track(track: dict, delay: float = 3.0) -> MatchResult:
    """Match a single track. Network errors propagate to the caller."""
    yandex_id = str(track.get("yandex_id", "") or "")
    isrc = str(track.get("isrc", "") or "").strip()

    if isrc:
        isrc_hits = search_isrc(isrc, delay=delay)
        if isrc_hits:
            top = isrc_hits[0]
            apple_id = str(top.get("trackId", "") or "")
            if apple_id:
                return MatchResult(
                    yandex_id=yandex_id,
                    apple_id=apple_id,
                    confidence=1.0,
                    method="isrc",
                    candidates=isrc_hits,
                )

    artists = track.get("artists", []) or []
    if isinstance(artists, str):
        artists = [artists]
    artist_q = " ".join(str(a) for a in artists)
    title_q = str(track.get("title", "") or "")
    candidates = search_term(artist_q, title_q, delay=delay)

    if not candidates:
        return MatchResult(yandex_id=yandex_id, apple_id="", confidence=0.0, method="", candidates=[])

    scored = [(score_candidate(track, c), c) for c in candidates]
    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best = scored[0]
    best_score = float(best_score)
    if best_score >= REVIEW_THRESHOLD:
        return MatchResult(
            yandex_id=yandex_id,
            apple_id=str(best.get("trackId", "") or ""),
            confidence=best_score,
            method="text+duration",
            candidates=candidates,
        )
    return MatchResult(
        yandex_id=yandex_id, apple_id="", confidence=best_score, method="", candidates=candidates
    )


# ---------------------------------------------------------------------------
# run() + CLI
# ---------------------------------------------------------------------------
def _load_json_list(path: str) -> list:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("tracks", "items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return []


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _write_json(path: str, data) -> None:
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def _load_job(path: str) -> dict:
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                return d
        except (OSError, ValueError):
            pass
    return {}


def _save_job(path: str, job: dict) -> None:
    if not path:
        return
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False, indent=2)


def run(
    input_json: str,
    matched_out: str,
    unmatched_out: str,
    report_out: str,
    limit=None,
    resume_job: str = "data/job.json",
    progress=None,
) -> dict:
    tracks = _load_json_list(input_json)
    if limit is not None:
        try:
            tracks = tracks[: int(limit)]
        except (TypeError, ValueError):
            pass
    total = len(tracks)

    matched: list = _load_json_list(matched_out)
    unmatched: list = _load_json_list(unmatched_out)
    if not isinstance(matched, list):
        matched = []
    if not isinstance(unmatched, list):
        unmatched = []

    processed: set[str] = set()
    for m in matched:
        if isinstance(m, dict) and "yandex_id" in m:
            processed.add(str(m["yandex_id"]))
    for u in unmatched:
        if isinstance(u, dict) and "yandex_id" in u:
            processed.add(str(u["yandex_id"]))

    job = _load_job(resume_job or "")
    if not job.get("job_id"):
        job["job_id"] = uuid.uuid4().hex
    job["stage"] = "match"
    job["total"] = total
    if not job.get("created_at"):
        job["created_at"] = datetime.now(timezone.utc).isoformat()
    if "failed" not in job or not isinstance(job["failed"], list):
        job["failed"] = []

    def _persist(done_count: int) -> None:
        job["done"] = done_count
        job["failed"] = [str(u.get("yandex_id", "")) for u in unmatched if isinstance(u, dict)]
        _write_json(matched_out, matched)
        _write_json(unmatched_out, unmatched)
        _save_job(resume_job, job)
        if progress:
            try:
                progress(done_count, total)
            except Exception:
                pass

    try:
        for track in tracks:
            if not isinstance(track, dict):
                continue
            yid = str(track.get("yandex_id", "") or "")
            if yid and yid in processed:
                continue
            try:
                res = match_track(track)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # network failure after retries -> record, never drop
                logger.warning("match failed for %s: %s: %s", yid, type(exc).__name__, exc)
                unmatched.append(
                    {
                        "yandex_id": yid,
                        "title": str(track.get("title", "") or ""),
                        "artists": list(track.get("artists", []) or []),
                        "duration_ms": track.get("duration_ms", 0) or 0,
                        "isrc": str(track.get("isrc", "") or ""),
                        "reason": "error",
                        "confidence": 0.0,
                    }
                )
                if yid:
                    processed.add(yid)
                _persist(len(processed))
                continue

            if res.apple_id:
                matched.append(
                    {
                        "yandex_id": res.yandex_id,
                        "apple_id": res.apple_id,
                        "confidence": float(res.confidence),
                        "method": res.method,
                        "candidates": res.candidates or [],
                    }
                )
            else:
                cands = res.candidates or []
                if not cands:
                    reason = "isrc_miss" if str(track.get("isrc", "") or "").strip() else "no_candidates"
                else:
                    reason = "low_score"
                unmatched.append(
                    {
                        "yandex_id": yid,
                        "title": str(track.get("title", "") or ""),
                        "artists": list(track.get("artists", []) or []),
                        "duration_ms": track.get("duration_ms", 0) or 0,
                        "isrc": str(track.get("isrc", "") or ""),
                        "reason": reason,
                        "confidence": float(res.confidence or 0.0),
                    }
                )
            if yid:
                processed.add(yid)
            _persist(len(processed))
    except KeyboardInterrupt:
        _persist(len(processed))
        raise

    _persist(len(processed))

    # ---- report ----
    auto = sum(1 for m in matched if isinstance(m, dict) and float(m.get("confidence", 0) or 0) >= AUTO_THRESHOLD)
    review = sum(
        1
        for m in matched
        if isinstance(m, dict) and REVIEW_THRESHOLD <= float(m.get("confidence", 0) or 0) < AUTO_THRESHOLD
    )
    miss = len(unmatched)
    methods: dict[str, int] = {}
    for m in matched:
        if isinstance(m, dict):
            methods[str(m.get("method", ""))] = methods.get(str(m.get("method", "")), 0) + 1
    reasons: dict[str, int] = {}
    for u in unmatched:
        if isinstance(u, dict):
            reasons[str(u.get("reason", ""))] = reasons.get(str(u.get("reason", "")), 0) + 1

    lines = [
        "Migration report (Apple catalog matching)",
        "=========================================",
        f"total: {total}",
        f"matched: {len(matched)}",
        f"unmatched: {len(unmatched)}",
        "",
        "Buckets:",
        f"auto (>=0.85): {auto}",
        f"review (0.6-0.85): {review}",
        f"miss (<0.6): {miss}",
        "",
        "Methods:",
    ]
    for k in sorted(methods):
        lines.append(f"  {k or '(none)'}: {methods[k]}")
    lines.append("")
    lines.append("Unmatched reasons:")
    for k in sorted(reasons):
        lines.append(f"  {k or '(none)'}: {reasons[k]}")
    lines.append("")
    _ensure_parent(report_out)
    with open(report_out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    return {"total": total, "matched": len(matched), "unmatched": len(unmatched), "report": report_out}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Match Yandex favorites against the Apple catalog.")
    p.add_argument("--input", default="data/yandex_favorites.json", help="Input favorites JSON")
    p.add_argument("--matched", default="data/matched_tracks.json", help="Matched output JSON")
    p.add_argument("--unmatched", default="data/unmatched_tracks.json", help="Unmatched output JSON")
    p.add_argument("--report", default="data/migration_report.txt", help="Report output TXT")
    p.add_argument("--job", default="data/job.json", help="Resume job file")
    p.add_argument("--limit", type=int, default=None, help="Only process first N tracks (smoke test)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    summary = run(args.input, args.matched, args.unmatched, args.report, limit=args.limit, resume_job=args.job)
    print(f"total={summary['total']} matched={summary['matched']} unmatched={summary['unmatched']}")
    print(f"report: {summary['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
