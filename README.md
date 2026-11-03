# MTransfer — Yandex Music → Apple Music (SaaS branch)

**Self-hosted web service**: anyone with the link migrates their likes and playlists from **Yandex Music** to **Apple Music** — no accounts, no installs. Each visitor gets an isolated session (own tokens in server memory only, own data dir, 24h TTL).

> Branch `saas` = `master` + multi-user sessions + Docker deploy (app + Caddy auto-HTTPS). Single-user local mode still works via `run.bat`.

## Deploy (VPS or home server)

```bash
git checkout saas
# set your domain in Caddyfile (VPS), or use http://<lan-ip> for home LAN
docker compose up -d --build
docker compose logs -f app
```

Open `https://your-domain` (or `http://<lan-ip>:8000` for home server). Full guide: [deploy EN](docs/deploy.en.md) / [деплой RU](docs/deploy.ru.md).

Verified: image builds, `/api/health` → ok, `/api/session` sets `mtsession` cookie, sessions isolated, wizard served.

## For visitors (what the service does)

1. **Yandex login** — "Get login code", enter it at `ya.ru/device`, "Export my likes". No token copying.
2. **Match** — "Load my playlists" to tick chosen ones, or "Start matching" for likes. Progress bar, confidence review, not-found CSV download.
3. **Apple login** — "1 · Enable Apple access" (auto dev token), paste `media-user-token` (30-sec how-to on page), "Preview" shows counts.
4. **Transfer** — likes → playlist of your choice; ticked playlists → one Apple playlist each. "Retry failed" and "♥ Add likes only" included.

Tokens never touch disk; idle sessions vanish after 24h.

## Local single-user mode (same as master)

Double-click **`run.bat`** (or `python -m src.web`), open http://127.0.0.1:8000 — same 4-step wizard, data in local `data/`.

### CLI

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

# Likes
python -m src.yandex_login              # device-code login, saves .yandex_token
python -m src.export_yandex             # -> data/yandex_favorites.json/.csv
python -m src.match_apple               # -> matched/unmatched/report
python -m src.extract_apple_tokens --save --verify
python -m src.import_apple --dry-run
python -m src.import_apple --playlist "YM-port"

# Chosen playlists
python -m src.export_playlists --list            # show your playlists (kind, title, tracks)
python -m src.export_playlists --kinds 123,456   # export chosen ones
python -m src.export_playlists --all             # export every playlist

# Web UI instead of CLI:
python -m src.web          # or double-click run.bat -> http://127.0.0.1:8000
```

---

## EN

### How it works

```
[Yandex: device-code login] → export likes / chosen playlists → normalize → match (iTunes Search API)
→ [Apple: auto Developer JWT + Media User Token] → create playlist(s) + add to library → report
```

Pipeline (same as all proven open-source approaches):

1. **Export** — pull liked tracks (or chosen playlists via `export_playlists`) through device-code login, save `data/yandex_favorites.json/.csv` (playlists → `data/playlists/<kind>_<slug>.{json,csv}`).
2. **Match** — for each track query iTunes Search API. Score: ISRC exact (100%) > normalized `artist — title + duration` fuzzy match. Output `matched_tracks.json` / `unmatched_tracks.json` / `migration_report.txt`.
3. **Import** — scraped Developer JWT (auto-refreshed, Origin headers included) + user `media-user-token`, then `POST /library/playlists`, batch track adds, per-track ratings `PUT /v1/me/ratings/songs/{id}`. Idempotent, resumable, `--dry-run` supported.

### Quickstart

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

# Likes
python -m src.yandex_login              # device-code login, saves .yandex_token
python -m src.export_yandex             # -> data/yandex_favorites.json/.csv
python -m src.match_apple               # -> matched/unmatched/report
python -m src.extract_apple_tokens --save --verify
python -m src.import_apple --dry-run
python -m src.import_apple --playlist "YM-port"

# Chosen playlists
python -m src.export_playlists --list            # show your playlists
python -m src.export_playlists --kinds 123,456   # export chosen ones

# Web UI instead of CLI:
python -m src.web          # or double-click run.bat -> http://127.0.0.1:8000
```

### Layout

```
src/            export_yandex.py, export_playlists.py, yandex_login.py,
                match_apple.py, import_apple.py, extract_apple_tokens.py, web.py
docs/           PRD + Implementation plan (EN + RU)
data/           local only, gitignored — exports, match results, reports
run.bat         one-click launcher (Windows)
```

### Docs

- [PRD (EN)](docs/prd.en.md) / [PRD (RU)](docs/prd.ru.md)
- [Implementation Plan (EN)](docs/implementation-plan.en.md) / [План реализации (RU)](docs/implementation-plan.ru.md)

### Secrets — never commit

`.env` / `.developer_token` / `.media_user_token` / `.apple_music_token` / `data/` are gitignored. Repo is **private** until migration is done, then will be opened to public (scrubbed of tokens).

### Limitations

- Unofficial Yandex auth can break; tokens expire.
- RU-catalog / Cyrillic tracks may be missing in Apple → manual review of `unmatched`.
- No MP3 download (ToS risk) — catalog-link transfer only.
- iTunes Search ~20 req/min — matching is throttled.

---

## RU (ветка SaaS)

Самостоятельный веб-сервис: любой по ссылке переносит лайки и плейлисты из **Яндекс Музыки** в **Apple Music** — без аккаунтов и установок. У каждого посетителя изолированная сессия (свои токены только в памяти сервера, своя папка данных, TTL 24ч).

> Ветка `saas` = `master` + многопользовательские сессии + Docker-деплой (app + Caddy с авто-HTTPS). Локальный однопользовательский режим работает как раньше через `run.bat`.

## Деплой (VPS или домашний сервер)

```bash
git checkout saas
# прописать свой домен в Caddyfile (VPS) или http://<lan-ip> для домашней сети
docker compose up -d --build
docker compose logs -f app
```

Открыть `https://ваш-домен` (или `http://<lan-ip>:8000` дома). Полный гайд: [deploy EN](docs/deploy.en.md) / [деплой RU](docs/deploy.ru.md).

Проверено: образ собирается, `/api/health` → ok, `/api/session` ставит куку `mtsession`, сессии изолированы, визард отдаётся.

## Для посетителей (что умеет сервис)

1. **Вход в Яндекс** — «Получить код входа», ввести на `ya.ru/device`, «Выгрузить мои лайки». Ничего копировать не надо.
2. **Поиск** — «Загрузить мои плейлисты», отметить нужные, или «Начать поиск» для лайков. Прогресс-бар, проверка сомнительных, скачивание ненайденного.
3. **Вход в Apple** — «1 · Включить доступ Apple» (авто-токен), вставить `media-user-token` (инструкция на 30 сек на странице), «Предпросмотр» покажет цифры.
4. **Перенос** — лайки → в плейлист с вашим названием; отмеченные плейлисты → по одному плейлисту Apple на каждый. «Повторить ошибки» и «♥ Только лайки» в комплекте.

Токены не пишутся на диск; неактивные сессии исчезают через 24ч.

## Локальный режим (как в master)

Двойной клик по **`run.bat`** (или `python -m src.web`), открыть http://127.0.0.1:8000 — тот же мастер из 4 шагов, данные в локальной `data/`.

### CLI

```
[Яндекс: вход по коду устройства] → экспорт лайков / выбранных плейлистов → нормализация → матчинг (iTunes Search API)
→ [Apple: авто Developer JWT + Media User Token] → создание плейлистов + добавление в библиотеку → отчёт
```

Пайплайн (как во всех проверенных open-source решениях):

1. **Экспорт** — забираем лайки (или выбранные плейлисты через `export_playlists`) после входа по коду, сохраняем `data/yandex_favorites.json/.csv` (плейлисты → `data/playlists/<kind>_<slug>.{json,csv}`).
2. **Матчинг** — каждый трек ищем через iTunes Search API. Скоринг: точное совпадение ISRC (100%) > нечёткое сравнение нормализованных `артист — название + длительность`. На выходе `matched_tracks.json` / `unmatched_tracks.json` / `migration_report.txt`.
3. **Импорт** — фронтенд авторизуется через MusicKit JS `authorize()` (user token), бэкенд использует подсмотренный Developer JWT, затем `POST /library/playlists`, добавление треков, простановка лайков. Идемпотентно, с resume, есть `--dry-run`.

### Быстрый старт

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# Лайки
python -m src.yandex_login              # вход по коду, сохраняет .yandex_token
python -m src.export_yandex             # -> data/yandex_favorites.json/.csv
python -m src.match_apple               # -> matched/unmatched/report
python -m src.extract_apple_tokens --save --verify
python -m src.import_apple --dry-run
python -m src.import_apple --playlist "YM-port"

# Выбранные плейлисты (НОВОЕ)
python -m src.export_playlists --list            # показать плейлисты (kind, название, треки)
python -m src.export_playlists --kinds 123,456   # выгрузить выбранные
python -m src.export_playlists --all             # выгрузить все плейлисты

# Или веб-UI вместо CLI:
python -m src.web          # или двойной клик run.bat -> http://127.0.0.1:8000
```

### Структура

```
src/            export_yandex.py, export_playlists.py, yandex_login.py,
                match_apple.py, import_apple.py, extract_apple_tokens.py, web.py
docs/           PRD + план реализации (EN + RU)
data/           только локально, в gitignore — выгрузки, результаты матчинга, отчёты
run.bat         запуск в один клик (Windows)
```

### Документация

- [PRD (EN)](docs/prd.en.md) / [PRD (RU)](docs/prd.ru.md)
- [Implementation Plan (EN)](docs/implementation-plan.en.md) / [План реализации (RU)](docs/implementation-plan.ru.md)

### Секреты — не коммитить

`.env` / `.developer_token` / `.media_user_token` / `.apple_music_token` / `data/` — в gitignore. Репозиторий **приватный** до завершения переноса, затем откроем публично (вычистив токены).

### Ограничения

- Неофициальная авторизация Яндекса может сломаться; токены протухают.
- Треки из RU-каталога / на кириллице могут отсутствовать в Apple → ручная проверка `unmatched`.
- Без скачивания MP3 (риск нарушения ToS) — только перенос связок каталога.
- iTunes Search ~20 запр/мин — матчинг с троттлингом.
