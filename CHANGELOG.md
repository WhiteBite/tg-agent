# Changelog

## 0.3.1 — 2026-10-12
Фиксы аудита (безопасность, корректность, Windows):
- Секреты маскируются на инджесте (`message_row`), а не только на чтении; LIKE-фолбэк, UI, `run_sql` и дайджесты тоже отдают замаскированный текст.
- `run_sql`: `PRAGMA query_only` + authorizer на отдельном соединении вместо keyword-blacklist; слова в строковых литералах больше не блокируются.
- Починен триггёр удаления из FTS: `DELETE FROM messages` больше не падает «SQL logic error» (старые базы мигрируются автоматически).
- `tg_new` не сбрасывает `truncated` при лимите; `search_fts` отдаёт единый контракт `engine/count/hits` без служебного `_fallback`.
- `tg_live_*`: async-инструменты; сбой отдаёт JSON-ошибку вместо зависания вызова.
- sync: `entity.id` хранится в marked-форме (`-100…`) — live.py и watch.py видят одни и те же id; backfill продолжается с места остановки (`meta:backfill:<chat>`); `sender_name` не лезет в event loop.
- ui: Host-гейт (DNS-rebinding), POST только с `application/json` (CSRF), constant-time сравнение токена, отказ поднимать `0.0.0.0` без `TG_UI_TOKEN`; state/poll/search без N+1; листание «показать старее» починено.
- live: чаты, добавленные после старта, подхватываются без рестарта; один коннект к БД.
- digest: сбой LLM-провайдера не двигает курсор; выжимка маскирует сырой текст.
- Тесты: 144 офлайн-проверки, сьют работает на Windows (utf-8 пайпы, очередь вместо select).

## 0.3.0 — 2026-10-11
Гейты и упаковка в репозиторий:
- `TG_STRICT_WATCH=1` — агент читает только темы из watch-листа (tg_read/tg_find/tg_stats/tg_links/tg_topics).
- `doctor.py` — диагностика окружения (библиотеки, креды, сессия, БД, FTS5, self-test маскировки); `--live` проверяет подключение к Telegram.
- Бюджет контекста дайджеста (`DIGEST_MAX_CHARS`, по умолчанию 12000).
- FloodWait в sync/backfill: сон + до 3 повторов чанка.
- `PRAGMA busy_timeout` — live.py, sync.py и UI пишут в одну базу без «database is locked».
- `TG_UI_TOKEN` — токен-гейт на все `/api/*` при публикации UI наружу.
- `tg_read`: `date_from`/`date_to`, `before_id`/`next_before`, `budget_chars`+`truncated`.
- Новый инструмент `tg_topics` — структура индекса без угадывания id.
- live.py: новые темы сохраняются сразу с заголовком.
- LICENSE (MIT), .gitignore, CI (GitHub Actions), этот changelog.
- Тесты: 103 офлайн-проверки.

## 0.2.0 — 2026-10-11
Майнинг готовых MCP + слой 3:
- sanitize.py (chigwell, Apache-2.0), extract_refs (0xMRK, MIT), пины, папки Telegram.
- Маскировка секретов на инджесте (redact.py).
- Live-режим (live.py + /api/poll), дайджесты (digest.py, tg_digests), топ по реакциям.
- UI: рейка папок, баннеры пинов и дайджестов, прыжок «к дате», «копировать для агента».

## 0.1.0 — 2026-10-11
Первая версия: Telethon → SQLite (FTS5) → MCP-сервер (read-only), watch-лист,
веб-визуал «как в TG Desktop», демо-база.
