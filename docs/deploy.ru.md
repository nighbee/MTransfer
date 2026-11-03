# MTransfer SaaS — гайд по деплою (RU)

Две цели: дешёвый **VPS** (публично) или **домашний сервер** (LAN / Tailscale).

## Что получаем

- Один `docker compose up -d` — приложение + Caddy (авто-HTTPS).
- Без аккаунтов: у каждого посетителя изолированная сессия (кука `mtsession`).
- Токены только в памяти сервера; данные сессии в `data/sessions/<id>/`.
- Неактивные сессии выпадают из памяти через 24ч (`SessionStore.cleanup`); файлы чистятся `docker volume prune` или сниппетом ниже.

## VPS (Hetzner / DigitalOcean / др.)

1. A-запись домена → IP сервера (например `mtransfer.example.com`).
2. В `Caddyfile` заменить `mtransfer.example.com` на свой домен.
3. На сервере:
   ```bash
   git clone <repo> && cd MTransfer
   git checkout saas
   docker compose up -d --build
   docker compose logs -f
   ```
4. Открыть `https://ваш-домен` — мастер проведёт каждого посетителя.

Минимальный бокс: 1 vCPU / 1 ГБ RAM. Матчинг I/O-bound (~3 сек/трек через iTunes API).

## Домашний сервер (LAN или Tailscale)

1. Тот же `docker compose up -d --build` на домашней коробке.
2. Только LAN: первую строку `Caddyfile` заменить на `http://<lan-ip-или-хост>` (без публичного TLS внутри сети).
3. Удалённый доступ: Tailscale-хост (`http://box.tailnet:80` через Caddy, либо пробросить порт приложения):
   Проще всего: раскомментировать блок `ports: ["8000:8000"]` в compose и ходить на `http://<tailscale-ip>:8000`.

## Эксплуатация

```bash
docker compose ps                    # статус
docker compose logs -f app           # логи (токены заредактированы)
docker compose down                  # стоп
docker compose up -d --build         # передеплой после git pull

# удалить файлы сессий старше 2 дней, .gitkeep не трогаем:
find /var/lib/docker/volumes/mtransfer-data/_data/sessions -maxdepth 1 -mtime +2 -exec rm -rf {} +
```

Проверено 2026-11-03: образ собирается, `/api/health` → ok, `/api/session`
ставит куку `mtsession`, две сессии получают разные id, `/` отдаёт визард.

## Про безопасность

- HTTPS терминирует Caddy (авто Let's Encrypt на VPS).
- Кука сессии `HttpOnly; SameSite=Lax`; только доверенная сеть или HTTPS.
- Токены и выгрузки не коммитятся — `data/`, dot-токены в gitignore.
- Про лимиты: iTunes Search ~20 запр/мин на IP — один IP сервера на всех посетителей, матчинг естественно встаёт в очередь; в UI живой прогресс по каждой задаче.
