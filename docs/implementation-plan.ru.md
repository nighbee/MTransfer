# MTransfer — План реализации (RU)

**Объём:** разовый личный перенос, авторизация токенами/куками, Python CLI + крошечный веб-UI (FastAPI + MusicKit JS), в первую очередь Windows.
Компаньон: [PRD (RU)](prd.ru.md).

---

## 0. Принципы

- Прагматизм одноразового проекта: можно хардкодить, никакого мультитенанта, никакого фреймворка миграций — JSON job-файла достаточно.
- Сначала CLI, потом веб-UI: каждый этап работает headless; UI только визуализирует + делает MusicKit `authorize()`.
- Не терять треки молча: export → match → import сохраняют счётчики; несопоставленное всегда перечислено с причиной.
- Гигиена секретов с шага 0: `.gitignore` + заредактированные логи до появления первого токена.

## 1. Декомпозиция работ

### Фаза 0 — Скелет (полдня)

- `README.md` (готово), `docs/` (готово), `.gitignore` (`data/`, `.env`, `.developer_token`, `.media_user_token`, `.apple_music_token`, `.venv/`), `requirements.txt` (`yandex-music`, `requests`, `fastapi`, `uvicorn`, `python-dotenv`), `src/__init__.py`, `data/.gitkeep`, `src/models.py` (dataclasses Track/MatchResult/Job + JSON ser/de).

### Фаза 1 — Экспорт из Яндекса (CLI)

- `src/export_yandex.py`:
  - Авторизация: `YANDEX_MUSIC_TOKEN` из env (документировать OAuth implicit-flow URL для получения); запасной `--from-cookie` — чтение cookie-хранилища Chrome (read-only, документированно).
  - Забор `users/likes/tracks` с пагинацией; до-fetch каждого трека для ISRC/длительности/альбома/обложки; вежливая пауза (~300мс).
  - Запись `data/yandex_favorites.json` + `.csv`; печать количества; режим `--verify-only` (только посчитать без дампа).
  - Ошибки: 401 → «токен невалиден/протух, перелогиньтесь» (без дампа токена в трейс); сеть → 3× backoff.
- Готово когда: количество в JSON сходится с цифрой лайков в UI Яндекса.

### Фаза 2 — Матчер

- `src/normalize.py`: `norm(s)` = lower, trim, схлопывание пробелов/пунктуации, опциональный флаг `ё→е`, вынос `feat.` в отдельное поле, кириллица нетронута.
- `src/match_apple.py`:
  - Порядок поиска: (а) ISRC через iTunes `lookup?isrc=`, если есть → принять 1.0; (б) `search?term={artist title}&media=music&entity=song&limit=5`; скоринг кандидатов: схожесть названий (difflib/rapidfuzz) × пересечение артистов × бонус за Δ длительности ≤3с; пороги: авто ≥0.85, на проверку 0.6–0.85, мимо <0.6.
  - Троттлинг ~3с между поисками; resume из `data/job.json`; запись `matched_tracks.json`, `unmatched_tracks.json` (с причиной: `no_candidates|low_score|isrc_miss`), `migration_report.txt`.
  - `--limit N` для smoke-тестов (первые 20 треков).
- Готово когда: отчёт показывает корзины; ручная проверка 30 совпадений — precision ≥90% в авто-корзине.

### Фаза 3 — Токены Apple + dry-run импорта

- `src/extract_apple_tokens.py`: `--save` извлекает Developer JWT из бандла `beta.music.apple.com` → `.developer_token`; `--from-chrome` копирует cookie `media-user-token` → `.media_user_token` (документировать ручной fallback через DevTools); `--verify` дергает `amp-api` `/v1/me/storefront` обоими токенами.
- `src/import_apple.py`:
  - Сторфронт — из user token; `--dry-run` печатает «плейлист X, N треков, M лайков» и выходит с 0 без мутаций.
  - Реальный прогон: создать `Yandex Import YYYY-MM-DD` (при повторе в тот же день — переиспользовать, поиск по имени), батч-добавление ≤50 ID, `PUT ratings` для лайков, пропуск уже-находящихся (сначала забрать треки плейлиста), лог ошибок по трекам, `--from N` resume, `retry --only-failed`.
- Готово когда: dry-run на подмножестве из 20 треков проходит; реальный импорт 20 треков виден в веб- и десктоп-Apple Music.

### Фаза 4 — Крошечный веб-UI

- `src/web.py` (FastAPI) + `src/static/index.html` (MusicKit JS v3):
  - Экраны: ① Подключение Яндекса (поле токена + [Проверить→количество] + [Экспорт→прогресс через SSE/опрос job.json]); ② Матчинг (прогресс + таблица результатов с цветами уверенности, чекбоксы пропуска, поле ручного override ISRC/Apple-ID, [Скачать unmatched.csv]); ③ Подключение Apple ([Войти через Apple Music] → `musicKit.authorize()` → MUT в бэкенд только в память, [Dry-run]); ④ Перенос (прогресс + финальный отчёт + кнопка повтора).
  - Эндпоинты бэкенда: `/api/yandex/test|export`, `/api/match/run|results`, `/api/apple/dev-token/status`, `/api/apple/user-token` (принять с фронтенда, только в память), `/api/import/dry-run|run|retry`, `/api/report`.
  - Переключатель EN/RU (простой словарь, без i18n-фреймворка).
- Готово когда: полный флоу кликается end-to-end на localhost без CLI.

### Фаза 5 — Мой перенос + публичный релиз

- Полный прогон (экспорт → матчинг → проверка → импорт), сохранить `migration_report.txt`, топ несопоставленного добавить вручную в UI Apple.
- Вычистка: `git log --all -p | grep -i token` чист, ротация токенов Яндекса/Apple, удалить содержимое `data/` (оставить `.gitkeep`), переключить репозиторий в public, добавить скриншоты в README.

## 2. Порядок создания файлов

1. `.gitignore`, `requirements.txt`, `src/models.py`
2. `src/export_yandex.py` → тест на моём аккаунте
3. `src/normalize.py` + `src/match_apple.py` → тест `--limit 20`
4. `src/extract_apple_tokens.py` → `--verify`
5. `src/import_apple.py --dry-run` → живой тест на 20 треках
6. `src/web.py` + `static/` → полный локальный флоу
7. Полный перенос → вычистка → public

## 3. Стратегия тестирования (лёгкая, одноразовая)

- Smoke: у каждого CLI есть `--limit/--dry-run/--verify-only`.
- Спот-чеки вместо юнит-тестов: ручная проверка precision 30 совпадений; проверка дубликатов при повторе; проверка resume после kill (`Ctrl-C` посреди матчинга → перезапуск продолжает).
- Проверка утечек секретов: `git status --porcelain` никогда не показывает `data/*` и dot-токены; опциональный `check_secrets.py` в духе pre-commit (5 строк, grep staged-путей).
- CI не нужен; гейт — локальный `python -m py_compile` + один полный dry-run.

## 4. Риски → конкретные действия (на этапе сборки)

- Поломка библиотеки Яндекса → пиним версию в requirements; заворачиваем вызовы API в один адаптер `yandex_client.py`, замена = 1 файл.
- Поломка скрапинга JWT → изоляция в `extract_apple_tokens.py`; ручная копия через DevTools как постоянный fallback документируется.
- Качество матчера на кириллице → флаги нормализации + колонка ручного override в UI с первого дня (не откладывать).
- 429-е → общий хелпер `throttled_get()` с backoff, используется и матчером, и импортёром.

## 5. Definition of Done

Как в PRD §12; плюс: репозиторий публичен, в README 2 скриншота (таблица матчинга, финальный отчёт), мой плейлист Apple существует.

---

*Английская версия: [Implementation Plan (EN)](implementation-plan.en.md). Требования: [PRD (RU)](prd.ru.md).*
