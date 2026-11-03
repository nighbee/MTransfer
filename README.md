# MTransfer — Yandex Music → Apple Music

One-time migration tool: export your liked tracks from **Yandex Music**, match them against the **Apple Music / iTunes catalog**, and import them into your Apple Music library + playlist.

> Stack: **Python CLI + tiny web UI** (FastAPI + MusicKit JS). Windows-compatible. Token/cookie auth (no official Yandex export API, no paid Apple Developer account needed for one-time run).

## Easy way (recommended)

Double-click **`run.bat`** (or `python -m src.web`), open http://127.0.0.1:8000 — a 4-step wizard guides you:

1. **Yandex login** — press "Get login code", enter the code at `ya.ru/device`, press "Export my likes". No token copying.
2. **Match** — press "Start matching", watch the progress bar. Review low-confidence matches, download the not-found list.
3. **Apple login** — press "1 · Enable Apple access" (auto), paste `media-user-token` from `beta.music.apple.com` cookies (30-sec how-to is on the page), press "Preview" to see counts.
4. **Transfer** — set playlist name, press "Start transfer". "Retry failed" and "♥ Add likes only" handle the rest.

No command line needed. Tokens stay in server memory only.

## CLI way

---

## EN

### How it works

```
[Yandex: OAuth token / cookie] → export likes → normalize → match (iTunes Search API)
→ [Apple: Developer JWT + Media User Token] → create playlist + add to library → report
```

Pipeline (same as all proven open-source approaches):

1. **Export** — pull liked tracks from Yandex via unofficial auth (`oauth.yandex.ru` token or logged-in cookie → `api.music.yandex.ru`), save `data/yandex_favorites.json/.csv`.
2. **Match** — for each track query iTunes Search API / Apple catalog search. Score: ISRC exact (100%) > normalized `artist — title + duration` fuzzy match. Output `matched_tracks.json` / `unmatched_tracks.json` / `migration_report.txt`.
3. **Import** — frontend authorizes via MusicKit JS `authorize()` (user token), backend uses scraped Developer JWT, then `POST /library/playlists`, add tracks, mark favorites. Playlist name: `Yandex Import YYYY-MM-DD`. Idempotent, resumable, `--dry-run` supported.

### Quickstart (planned)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

set YANDEX_MUSIC_TOKEN=your_token
python src/export_yandex.py
python src/match_apple.py
python src/extract_apple_tokens.py --from-chrome --save
python src/import_apple.py --dry-run
python src/import_apple.py --playlist "Yandex Import"
# or web UI:
uvicorn src.web:app --reload
```

### Layout (planned)

```
src/            export_yandex.py, match_apple.py, import_apple.py, extract_apple_tokens.py, web.py
docs/           PRD + Implementation plan (EN + RU)
data/           local only, gitignored — exports, match results, reports
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

Одноразовый инструмент переноса: выгрузить лайки из **Яндекс Музыки**, сопоставить с каталогом **Apple Music / iTunes** и импортировать в медиатеку + плейлист Apple Music.

> Стек: **Python CLI + крошечный веб-UI** (FastAPI + MusicKit JS). Работает на Windows. Авторизация через токены/куки (официального export API у Яндекса нет, платный Apple Developer для разового переноса не нужен).

## Быстрый путь (рекомендуется)

Двойной клик по **`run.bat`** (или `python -m src.web`), открыть http://127.0.0.1:8000 — мастер из 4 шагов проведёт за руку:

1. **Вход в Яндекс** — «Получить код входа», ввести код на `ya.ru/device`, «Выгрузить мои лайки». Ничего копировать не надо.
2. **Поиск** — «Начать поиск», смотреть прогресс-бар. Проверить сомнительные совпадения, скачать список ненайденного.
3. **Вход в Apple** — «1 · Включить доступ Apple» (автоматом), вставить `media-user-token` из куки `beta.music.apple.com` (инструкция на 30 сек прямо на странице), «Предпросмотр» покажет цифры.
4. **Перенос** — задать имя плейлиста, «Начать перенос». «Повторить ошибки» и «♥ Только лайки» доделают остальное.

Командная строка не нужна. Токены живут только в памяти сервера.

## Через командную строку

```
[Яндекс: OAuth-токен / cookie] → экспорт лайков → нормализация → матчинг (iTunes Search API)
→ [Apple: Developer JWT + Media User Token] → создание плейлиста + добавление в библиотеку → отчёт
```

Пайплайн (как во всех проверенных open-source решениях):

1. **Экспорт** — забираем лайки из Яндекса через неофициальную авторизацию (токен `oauth.yandex.ru` или cookie залогиненного браузера → `api.music.yandex.ru`), сохраняем `data/yandex_favorites.json/.csv`.
2. **Матчинг** — каждый трек ищем через iTunes Search API / catalog search Apple. Скоринг: точное совпадение ISRC (100%) > нечёткое сравнение нормализованных `артист — название + длительность`. На выходе `matched_tracks.json` / `unmatched_tracks.json` / `migration_report.txt`.
3. **Импорт** — фронтенд авторизуется через MusicKit JS `authorize()` (user token), бэкенд использует подсмотренный Developer JWT, затем `POST /library/playlists`, добавление треков, простановка лайков. Плейлист: `Yandex Import YYYY-MM-DD`. Идемпотентно, с resume, есть `--dry-run`.

### Быстрый старт (план)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

set YANDEX_MUSIC_TOKEN=ваш_токен
python src/export_yandex.py
python src/match_apple.py
python src/extract_apple_tokens.py --from-chrome --save
python src/import_apple.py --dry-run
python src/import_apple.py --playlist "Yandex Import"
# или веб-UI:
uvicorn src.web:app --reload
```

### Структура (план)

```
src/            export_yandex.py, match_apple.py, import_apple.py, extract_apple_tokens.py, web.py
docs/           PRD + план реализации (EN + RU)
data/           только локально, в gitignore — выгрузки, результаты матчинга, отчёты
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
