# MTransfer — Product Requirements Document (EN)

**Version:** 0.1 (draft) · **Status:** private repo, one-time personal migration → then public release (scrubbed)
**Stack:** Python CLI + tiny web UI (FastAPI + MusicKit JS) · **OS:** Windows-first · **Auth:** tokens/cookies only

---

## 1. Problem & Goal

Yandex Music offers no official library export. Apple Music offers an official import path (MusicKit) but requires a Developer JWT + user-authorized Media User Token, with no Yandex-side bridge. Manual re-adding of hundreds/thousands of liked tracks is infeasible.

**Goal:** a small, throwaway-friendly program/service that transfers my Yandex Music liked tracks into Apple Music (library + dated playlist) with a match report and manual-review loop for unmatched tracks.

**Non-goal:** a maintained multi-user SaaS, audio file download/upload, or ToS-violating scraping at scale.

## 2. Users

Single user (me) for v1. Post-open-sourcing: any Yandex→Apple migrant with basic CLI/browser skills.

## 3. Scope

### In scope (v1)

- **F1 — Yandex auth:** OAuth token (`oauth.yandex.ru`, pasted manually) and/or cookie import from a logged-in Chrome session. Token stored in env var / `.env`, never committed.
- **F2 — Yandex export:** liked tracks (title, artists[], album, duration_ms, ISRC if available, Yandex track id, cover URL). Output: `data/yandex_favorites.json` + `.csv`. Progress + resume on interruption.
- **F3 — Normalization:** canonical `Track` schema; lowercase/trim/punctuation folding; `feat./feat/ft.` handling; Cyrillic preserved; duration rounded to seconds.
- **F4 — Apple catalog matching:** per-track lookup via iTunes Search API (no auth, throttled ~20 req/min) with fallback to Apple catalog search (needs Developer JWT). Scoring: ISRC exact = auto-accept (1.0); normalized text + duration Δ≤3s = high (≥0.85); text-only = medium (manual review). Output: `matched_tracks.json`, `unmatched_tracks.json`, `migration_report.txt` with stats and confidence buckets.
- **F5 — Web UI review screen:** table of matches (green/yellow/red by confidence), checkbox include/skip, manual search-and-pick for unmatched, `--dry-run` preview counts.
- **F6 — Apple auth:** Developer JWT auto-extracted from `beta.music.apple.com` JS bundle (script `extract_apple_tokens.py --save`); Media User Token via MusicKit JS `authorize()` in the web UI (preferred, no cookie copy-paste) with cookie-paste fallback (`.apple_music_token`). Tokens expire in weeks–months; re-extract documented.
- **F7 — Apple import:** create playlist `Yandex Import YYYY-MM-DD`; add matched Apple IDs; add to library / set favorites; idempotent (skip existing playlist entries, safe re-run); progress bar; resume `--from N`; per-track error log; `--dry-run`.
- **F8 — Report & retry:** final report (imported/skipped/failed counts + reasons); unmatched CSV for manual Apple-side adding; `retry --only-failed`.

### Out of scope (v1)

Playlists beyond Likes (v2), albums/artists follows, MP3 download, scheduled sync, multi-account, mobile app, paid Apple Developer account flow.

## 4. User Flow (web UI)

1. **Connect Yandex** → paste OAuth token (or "import cookie from Chrome") → [Test] shows `N liked tracks found` → [Export] with progress.
2. **Match** → auto-run with progress → table: ✅ high / ⚠️ review / ❌ not found; adjust picks; [Continue].
3. **Connect Apple** → [Sign in with Apple Music] (MusicKit `authorize()`) → shows account/storefront → [ Dry-run ] shows "X will be added".
4. **Transfer** → progress bar → done screen: imported / skipped / failed + links to playlist + [Download unmatched.csv] [Retry failed].

CLI mirrors every step (`export → match → extract-tokens → import --dry-run → import`).

## 5. Functional Requirements

- FR1: export must capture ≥ all fields needed for matching (title/artists/album/duration/ISRC).
- FR2: matcher must never silently drop a track — every input ends in matched or unmatched with a reason + score.
- FR3: matcher throughput ≥ ~15 tracks/min respecting rate limits; full run resumable.
- FR4: dry-run must perform zero Apple-library mutations and print exact pending changes.
- FR5: import must be idempotent — re-running adds no duplicates.
- FR6: secrets (tokens, cookies, exports) must never be written to git-tracked paths; `.gitignore` enforced; `--verify` redacts token values in logs.
- FR7: all network failures retried with backoff (3 attempts), then recorded as failed with reason.

## 6. Data Model

```jsonc
// Track (canonical)
{ "source": "yandex", "yandex_id": "123", "title": "…", "artists": ["…"],
  "album": "…", "duration_ms": 213000, "isrc": "…", "cover_url": "…" }
// MatchResult
{ "yandex_id": "123", "apple_id": "456", "confidence": 0.93,
  "method": "isrc|text+duration|manual", "candidates": [...] }
// Job
{ "job_id": "uuid", "stage": "export|match|import", "total": N, "done": M,
  "failed": [...], "created_at": "…" }
```

Files: `data/yandex_favorites.{json,csv}`, `data/matched_tracks.json`, `data/unmatched_tracks.json`, `data/migration_report.txt`, `data/job.json` (resume pointer). `src/` modules: `export_yandex.py`, `match_apple.py`, `import_apple.py`, `extract_apple_tokens.py`, `web.py` (+ `static/` MusicKit JS page).

## 7. Apple Music Integration (concrete endpoints)

- Dev JWT: scraped from `beta.music.apple.com` bundle (temporary, months-long TTL).
- User token: `musicKit.authorize()` in browser (preferred) or `media-user-token` cookie value.
- Search: `GET https://itunes.apple.com/search?term=&media=music&entity=song&limit=5` (no auth) or `GET https://amp-api.music.apple.com/v1/catalog/{storefront}/search` (needs both tokens).
- Library: `POST /v1/me/library/playlists` (create), `POST /v1/me/library/playlists/{id}/tracks` (add), `PUT /v1/me/ratings/songs` (favorite). Storefront derived from user token (`/v1/me/storefront`).
- Quotas: throttle search (~3s delay), batch add in chunks (≤50), backoff on 429.

## 8. Yandex Integration (concrete)

- Auth A: OAuth implicit flow `https://oauth.yandex.ru/authorize?response_type=token&client_id=<app>` → token in URL hash → `YANDEX_MUSIC_TOKEN`.
- Auth B: cookie harvest from logged-in Chrome profile (documented, opt-in).
- API: `api.music.yandex.ru` (or `yandex-music` Python lib) — `likes/tracks` pagination; fields per §6. Respect delays; on 401 → "re-login" message.

## 9. UX Requirements

- Bilingual UI strings (EN/RU toggle); one screen per stage; big status ("312 / 1050 matched"); confidence colors; single primary button per screen; copy-pasteable token fields with `password` masking; links to docs at each auth step.

## 10. Non-Functional Requirements

- Windows 10/11 + Python 3.10+; `pip install -r requirements.txt` only.
- No background services; SQLite/JSON job file; Ctrl-C safe.
- Logs to console + `data/run.log`; tokens redacted.
- Full migration of ~1–2k likes in < 2h wall-clock (mostly throttled matching).

## 11. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Yandex unofficial API/cookie changes | pin `yandex-music` version; cookie fallback; clear re-auth error |
| Dev JWT / user token expiry | `extract --save --verify`; MusicKit re-authorize button |
| RU/Cyrillic tracks missing in Apple catalog | unmatched CSV + manual pick UI; don't block import |
| Rate-limit 429s | delays + backoff + resume |
| Accidental public token leak | gitignore + pre-commit check; private repo until scrubbed |

## 12. Acceptance Criteria (v1 done)

- [ ] Export produces JSON+CSV with counts matching Yandex likes (± explicitly listed skips).
- [ ] Every track lands in matched or unmatched with score+reason.
- [ ] Dry-run changes nothing; real run creates dated playlist with matched tracks + favorites.
- [ ] Re-run adds zero duplicates; resume works after kill.
- [ ] Final report + unmatched.csv exist; no secrets in git (`git log --all -p` clean).
- [ ] Repo made public only after token scrub + docs done.

## 13. Milestones

M1 CLI export+match → M2 Apple import (dry-run) → M3 web UI + MusicKit authorize → M4 my full migration + report → M5 scrub + public release.

---

*Russian version: [PRD (RU)](prd.ru.md). Implementation: [Implementation Plan (EN)](implementation-plan.en.md).*
