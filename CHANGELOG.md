# Changelog

## 0.4.0 — 2026-10-14
Слой извлечения сущностей поверх индекса (сообщения остаются источником истины, сущности — пересчитываемая проекция):
- Новый `extract.py`: generic движок по декларативным пакетам `packs/`; курсор `extract:<chat>:<topic>:<pack>`, первое окно `backfill_days`, до 200 сообщений за прогон; операции new/update/close/reopen одной транзакцией (`entities`, `entity_events`, `extraction_runs`); сбой провайдера курсор не двигает; `EXTRACT_PROVIDER=stub|openai` (`EXTRACT_URL`/`EXTRACT_KEY`/`EXTRACT_MODEL`), `--pack`, `--dry-run`.
- Новый пакет `packs/construction.py` (PM для стройки): kinds decision/task/promise/question; каждая операция обязана evidence-цитатой, цитата проверяется подстрокой текста сохранённого сообщения (плюс транскрипты) — без валидного evidence операция отклоняется.
- MCP: инструментов стало 13 вместо 10 — новые `tg_entities` (фильтры pack/kind/status/project/chat_id), `tg_entity` (история событий с доказательствами), `tg_overdue` (просроченное и горизонт `days_ahead`). Сервер по-прежнему read-only и не импортирует extract/notify/media.
- Новый `monitor.py`: сводка просроченного и дедлайнов по проектам (`--days-ahead`, `--project`, `--send`); новый `notify.py`: отправка в Saved Messages — единственный пишущий в Telegram модуль, dry-run по умолчанию, реальная отправка только за `TG_ALLOW_SEND=1`, cap `TG_NOTIFY_CAP` (10).
- Новый `media.py`: очередь транскрипции голосовых/документов (`media_assets`) из sync/live; `MEDIA_PROVIDER=stub|openai` (`MEDIA_URL`/`MEDIA_KEY`/`MEDIA_MODEL`); транскрипты подмешиваются в промпт извлечения; openai без скачанных байтов — `NotImplementedError` (загрузка файлов — отдельный инкремент).
- Новый `export.py`: выгрузка сущностей во внешние системы; file-провайдер (JSONL, `--out`/`TG_EXPORT_PATH`), курсор в таблице `integrations`; 101/notion — структурированная ошибка без исключений.
- Мульти-проектность: тег `project` у строк watch-листа (`watch.py add --project`, подкоманда `project`); сущности наследуют проект от темы.
- Тесты: 285 офлайн-проверок.

## 0.3.1 — 2026-10-12
Фиксы аудита (безопасность, корректность, Windows):
- Новый `start.py`: одна команда вместо десяти — логин (если нужно), выбор чатов по номерам, синк, открытие визуала.
- Визуал: панель «＋ из Telegram» — список чатов из Telegram (форум/группа/канал, пометка «в индексе»), фильтр, окно дней и синк кнопкой (`/api/available`, `/api/sync_chat`); `start.py` теперь только логин → браузер (`--cli` — прежний выбор в терминале).
- `sync.py`: `--days` реально ограничивает окно (раньше ползла вся история); корень темы берётся из `reply_to_top_id` (вложенные ответы больше не разъезжаются по темам); `Telethon.start` не получает `phone=None` (падало до проверки сессии).
- Новый `login.py`: логин QR-кодом без ввода телефона и кода (2FA — пароль, до 3 попыток без повторного скана), `--phone` фолбэк, `--export` — строка сессии.
- Новый `env.py`: все скрипты читают `.env` (env-переменные приоритетнее; BOM/UTF-16; `TG_ENV_FILE=""` отключает чтение) — креды и пути задаются один раз.
- Секреты маскируются на инджесте (`message_row`), а не только на чтении; LIKE-фолбэк, UI, `run_sql` и дайджесты тоже отдают замаскированный текст; `tg_live_*` тоже.
- `run_sql`: `PRAGMA query_only` + authorizer на отдельном соединении вместо keyword-blacklist; слова в строковых литералах больше не блокируются; limit зажимается в 1..2000.
- Починен триггёр удаления из FTS: `DELETE FROM messages` больше не падает «SQL logic error» (старые базы мигрируются автоматически).
- `tg_new` не сбрасывает `truncated` при лимите, а `returned`/`max_id` описывают показанное после урезки бюджетом — mark_read больше не пропускает невиданное.
- `search_fts` отдаёт единый контракт `engine/count/hits` без служебного `_fallback`.
- `tg_live_*`: async-инструменты; сбой отдаёт JSON-ошибку вместо зависания вызова.
- sync: `entity.id` хранится в marked-форме (`-100…`) — live.py и watch.py видят одни и те же id; базы до 0.3.1 мигрируются на marked-id автоматически (chats/messages/watch/refs/pins/digests/курсоры); backfill продолжается с места остановки (`meta:backfill:<chat>`); `sender_name` не лезет в event loop; темы берут настоящий `ForumTopic.title` (строка).
- ui: Host-гейт (DNS-rebinding), POST только с `application/json` (CSRF), constant-time сравнение токена, отказ поднимать `0.0.0.0` без `TG_UI_TOKEN`; state/poll/search без N+1 и с корректным last-сообщением по (chat_id, msg_id); листание «показать старее» починено.
- live: чаты, добавленные после старта, подхватываются без рестарта; один коннект к БД.
- digest: сбой LLM-провайдера не двигает курсор; дайджест и курсор пишутся одной транзакцией; выжимка маскирует сырой текст.
- Тесты: 155 офлайн-проверок, сьют работает на Windows (utf-8 пайпы, очередь вместо select).

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
