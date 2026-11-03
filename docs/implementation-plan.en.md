# MTransfer — Implementation Plan (EN)

**Scope:** one-time personal migration, tokens/cookies auth, Python CLI + tiny web UI (FastAPI + MusicKit JS), Windows-first.
Companion: [PRD (EN)](prd.en.md).

---

## 0. Principles

- One-time-project pragmatism: hardcode-friendly, no multi-tenancy, no migrations framework — JSON job file is enough.
- CLI first, web UI second: every stage works headless; the UI only visualizes + does MusicKit `authorize()`.
- Never lose a track silently: export → match → import preserve counts; unmatched always listed with reason.
- Secrets hygiene from step 0: `.gitignore` + redacted logs before any token exists.

## 1. Work Breakdown

### Phase 0 — Skeleton (½ day)

- `README.md` (done), `docs/` (done), `.gitignore` (`data/`, `.env`, `.developer_token`, `.media_user_token`, `.apple_music_token`, `.venv/`), `requirements.txt` (`yandex-music`, `requests`, `fastapi`, `uvicorn`, `python-dotenv`), `src/__init__.py`, `data/.gitkeep`, `src/models.py` (Track/MatchResult/Job dataclasses + JSON ser/de).

### Phase 1 — Yandex export CLI

- `src/export_yandex.py`:
  - Auth: `YANDEX_MUSIC_TOKEN` env (document OAuth implicit-flow URL to obtain it); `--from-cookie` fallback reading Chrome cookie store (read-only, documented).
  - Fetch `users/likes/tracks` with pagination; per-track `fetch` for ISRC/duration/album/cover; polite delay (~300ms).
  - Write `data/yandex_favorites.json` + `.csv`; print count; `--verify-only` mode (count without dump).
  - Errors: 401 → "token invalid/expired, re-login" (no stack trace dump of token); network → 3× backoff.
- Done when: my likes count in JSON matches the Yandex UI count.

### Phase 2 — Matcher

- `src/normalize.py`: `norm(s)` = lower, trim, collapse whitespace/punct, `ё→е` optional flag, strip `feat.` to separate field, Cyrillic intact.
- `src/match_apple.py`:
  - Lookup order: (a) ISRC via iTunes `lookup?isrc=` when present → accept 1.0; (b) `search?term={artist title}&media=music&entity=song&limit=5`; score candidates: title similarity (difflib/rapidfuzz) × artist overlap × duration Δ≤3s bonus; threshold auto ≥0.85, review 0.6–0.85, miss <0.6.
  - Throttle ~3s between searches; resume from `data/job.json`; write `matched_tracks.json`, `unmatched_tracks.json` (with reason: `no_candidates|low_score|isrc_miss`), `migration_report.txt`.
  - `--limit N` for smoke tests (first 20 tracks).
- Done when: report shows buckets; spot-check 30 matches manually — precision ≥90% in auto bucket.

### Phase 3 — Apple tokens + dry-run import

- `src/extract_apple_tokens.py`: `--save` scrapes Developer JWT from `beta.music.apple.com` bundle → `.developer_token`; `--from-chrome` copies `media-user-token` cookie → `.media_user_token` (document manual DevTools fallback); `--verify` calls `amp-api` `/v1/me/storefront` with both tokens.
- `src/import_apple.py`:
  - Resolve storefront from user token; `--dry-run` prints "playlist X, N tracks, M favorites" and exits 0 without mutations.
  - Real run: create `Yandex Import YYYY-MM-DD` (reuse if re-run same day — check by name), batch-add ≤50 IDs, `PUT ratings` for likes, skip already-present (fetch playlist tracks first), log per-track failures, `--from N` resume, `retry --only-failed`.
- Done when: dry-run on 20-track subset passes; real 20-track import visible in Apple Music web + app.

### Phase 4 — Tiny web UI

- `src/web.py` (FastAPI) + `src/static/index.html` (MusicKit JS v3):
  - Screens: ① Yandex connect (token input + [Test→count] + [Export→progress via SSE/poll of job.json]); ② Match (progress + results table with confidence colors, skip checkboxes, manual ISRC/Apple-ID override field, [Download unmatched.csv]); ③ Apple connect ([Sign in with Apple Music] → `musicKit.authorize()` → send MUT to backend in-memory only, [Dry-run]); ④ Transfer (progress + done report + retry button).
  - Backend endpoints: `/api/yandex/test|export`, `/api/match/run|results`, `/api/apple/dev-token/status`, `/api/apple/user-token` (accept from frontend, memory only), `/api/import/dry-run|run|retry`, `/api/report`.
  - EN/RU toggle (simple dict, no i18n framework).
- Done when: full flow clickable end-to-end on localhost without touching CLI.

### Phase 5 — My migration + public release

- Full run (export → match → review → import), save `migration_report.txt`, manually add top unmatched in Apple UI.
- Scrub: `git log --all -p | grep -i token` clean, rotate Yandex/Apple tokens, delete `data/` contents (keep `.gitkeep`), flip repo to public, add screenshots to README.

## 2. File-by-file build order

1. `.gitignore`, `requirements.txt`, `src/models.py`
2. `src/export_yandex.py` → test on my account
3. `src/normalize.py` + `src/match_apple.py` → test `--limit 20`
4. `src/extract_apple_tokens.py` → `--verify`
5. `src/import_apple.py --dry-run` → 20-track live test
6. `src/web.py` + `static/` → full local flow
7. Full migration → scrub → public

## 3. Testing strategy (lightweight, one-time)

- Smoke: each CLI has `--limit/--dry-run/--verify-only`.
- Spot-checks over unit tests: 30-match manual precision check; duplicate re-run check; kill-resume check (`Ctrl-C` mid-match → rerun continues).
- Secret-leak check: `git status --porcelain` must never show `data/*` or dot-token files; a `pre-commit`-style `check_secrets.py` (5 lines, grep staged paths) optional.
- No CI required; local `python -m py_compile` + one full dry-run is the gate.

## 4. Risks → concrete actions (build-time)

- Yandex lib breakage → pin version in requirements; wrap API calls in one `yandex_client.py` adapter so swap = 1 file.
- JWT scrape breakage → isolate in `extract_apple_tokens.py`; document manual DevTools copy as permanent fallback.
- Matcher quality on Cyrillic → normalization flags + manual-override column in UI from day one (don't postpone).
- 429s → central `throttled_get()` helper with backoff used by both matcher and importer.

## 5. Definition of Done

Same as PRD §12; plus: repo public, README has 2 screenshots (match table, done report), and my Apple playlist exists.

---

*Русская версия: [План реализации (RU)](implementation-plan.ru.md). Требования: [PRD (EN)](prd.en.md).*
