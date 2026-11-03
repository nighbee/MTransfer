# MTransfer — Yandex Music → Apple Music

**Move your liked tracks AND chosen playlists** from **Yandex Music** to **Apple Music**. Matched against the Apple catalog, imported into your library + playlist(s).

> Stack: **Python CLI + web UI wizard** (FastAPI + MusicKit JS). Windows-first. Device-code login (no token pasting), auto Apple developer token, background jobs with progress.

## Easy way (recommended)

Double-click **`run.bat`** (or `python -m src.web`), open http://127.0.0.1:8000 — a 4-step wizard guides you:

1. **Yandex login** — press "Get login code", enter the code at `ya.ru/device`, press "Export my likes". No token copying.
2. **Match** — press "Load my playlists" to tick chosen playlists, or press "Start matching" for likes. Watch the progress bar. Review low-confidence matches, download the not-found list.
3. **Apple login** — press "1 · Enable Apple access" (auto), paste `media-user-token` from `beta.music.apple.com` cookies (30-sec how-to is on the page), press "Preview" to see counts.
4. **Transfer** — set playlist name, press "Start transfer" (likes), or "Port ticked playlists" (each Yandex playlist → its own Apple playlist). "Retry failed" and "♥ Add likes only" handle the rest.

No command line needed. Tokens stay in server memory only.

## CLI way

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

# Chosen playlists (NEW)
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

## RU

Перенос лайков **и выбранных плейлистов** из **Яндекс Музыки** в **Apple Music**. Сопоставление с каталогом Apple, импорт в медиатеку + плейлист(ы).

> Стек: **Python CLI + мастер веб-UI** (FastAPI + MusicKit JS). В первую очередь Windows. Вход по коду устройства (без копирования токенов), авто-токен разработчика Apple, фоновые задачи с прогрессом.

## Быстрый путь (рекомендуется)

Двойной клик по **`run.bat`** (или `python -m src.web`), открыть http://127.0.0.1:8000 — мастер из 4 шагов проведёт за руку:

1. **Вход в Яндекс** — «Получить код входа», ввести код на `ya.ru/device`, «Выгрузить мои лайки». Ничего копировать не надо.
2. **Поиск** — «Загрузить мои плейлисты», отметить нужные, или «Начать поиск» для лайков. Смотреть прогресс-бар. Проверить сомнительные совпадения, скачать список ненайденного.
3. **Вход в Apple** — «1 · Включить доступ Apple» (автоматом), вставить `media-user-token` из куки `beta.music.apple.com` (инструкция на 30 сек прямо на странице), «Предпросмотр» покажет цифры.
4. **Перенос** — задать имя плейлиста и «Начать перенос» (лайки) или «Перенести отмеченные плейлисты» (каждый плейлист Яндекса → свой плейлист Apple). «Повторить ошибки» и «♥ Только лайки» доделают остальное.

Командная строка не нужна. Токены живут только в памяти сервера.

## Через командную строку

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
