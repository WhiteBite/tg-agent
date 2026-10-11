# Changelog

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
