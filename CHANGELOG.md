# Changelog

## 0.7.0 — 2026-10-19
Индекс догоняет реальность переписки, медиа-очередь точна, экспорт 101 работает:
- Правки и удаления сообщений доходят до индекса: `db.py` — колонки `edited_ts`/`deleted` (+миграция старых баз, FTS-триггер), `update_message`/`mark_deleted`; `sync.py` подхватывает `edit_date` в rescan и backfill (`edited_messages` в отчёте); `live.py` обрабатывает `MessageEdited`/`MessageDeleted`. Строка отражает последний текст, предыдущие версии правок не хранятся; удаление — флаг, строка остаётся видима читалкам.
- Медиа-очередь: sync/live ставят в очередь только транскрибируемое аудио (voice + аудио-документы, гейт `media.is_transcribable`); обычные документы больше не enqueue. Скачивание реализовано (Telethon `download_media`); openai без байтов — `ValueError`.
- Паки: изоляция скоупа — `exclude_project_regex: "^сервисы$"` в construction; детерминированный fallback `due_text` → `due_ts` (абсолютные даты, дата без года резолвится по дате сообщения); ссылка `supersedes` — new/update закрывает открытую сущность того же scope с событием op='supersede'.
- `export.py`: провайдер 101 — реальный HTTP POST (stdlib urllib): `EXPORT_101_URL`, JWT `EXPORT_101_TOKEN`, `EXPORT_101_PATH` (дефолт `/entities`), `EXPORT_101_TIMEOUT`; один POST на сущность, курсор двигается только после 2xx; без кредов — структурированная ошибка. notion остаётся заглушкой.
- Тесты: `tests_index_media.py`, `tests_packs_semantics.py`, `tests_export_101.py` запускаются из `test_offline.py` подпроцессами; проверки: 475 → 594.

## 0.6.0 — 2026-10-14
Замкнутый цикл: LLM-мозг, накопление базы, проактивные сводки, автономность:
- `extract.py`: `--until-done` — раунды (по 200 сообщений на тему) дожёвывают весь хвост истории за один запуск; курсор и overflow-семантика прежние.
- Пакет `services` прицелен в watch-темы с проектом «сервисы» (scope `project_regex`) — не шумит в код-чатах; `construction` остался глобальным.
- `monitor.py`: блок «новое за Nч» в сводке — свежие сущности всех пакетов со временем появления (`--news-hours`, по умолчанию сутки). Утренний `pipeline --loop 1440 --send` = ежедневная сводка о новых триалах в Saved Messages.
- `pipeline.py`: `--loop N` — демон (полный цикл каждые N минут, cron не нужен); extract внутри конвейера теперь until_done.
- `watch.py suggest` — топ-тем по активности, не отмеченных в watch, с готовыми командами add: кураторство по данным, не вслепую.
- MCP `tg_entities` / `db.entities_list`: параметр `query` — подстрока в title/body («в каких триалах упоминался Claude»).
- Провайдер: проверен и подключён dashscope (openai-совместимый, strict json_schema) — пресет в `.env.example`.
- Проверки: 369 → 380.

## 0.5.1 — 2026-10-14
Эксплуатация на реальном аккаунте: живой discovery, папки под контролем агента, устойчивость:
- Новый `folders.py`: правка состава папок Telegram (`list` / `add` / `remove` по @username или id). Второй пишущий модуль после `notify.py`, контент не пишет — только membership; после правки состав перечитывается в индекс (`folder_chats`). Агенту достаточно попросить «добавь чат X в папку AI».
- `sync.py`: `--folder` переживает `FolderIdInvalidError` (слой 229 отклоняет folder_id в getDialogs) — состав берётся из include_peers (getDialogFilters). `fetch_folders` разворачивает объект `DialogFilters` и TextWithEntities-title.
- `sync.py`/`server.py`: `receive_updates=False` у CLI- и MCP-клиентов + `busy_timeout=30000` на общий `.session` — параллельная работа живого MCP-клиента и CLI-синка больше не падает `database is locked`.
- `env.py`: `harden_stdio()` — print не роняет скрипты на эмодзи в cp1251-консоли Windows (обрывал sync на названиях чатов).
- Проверки: 354 → 369.

## 0.5.0 — 2026-10-14
Discovery, предметный пакет и автономный конвейер:
- Новый `search.py`: глобальный поиск Telegram по ключевому слову (`messages.SearchGlobalRequest` — обычный API-запрос, не парсинг). Каналы/группы/юзеры с marked-id, `--type channel|group|user`, `--limit`; цепочка «поиск → sync --chat → watch» закрывает сценарий «найди все каналы по ИИ».
- MCP: `tg_live_search` за `TG_ALLOW_LIVE=1` — агент сам ищет каналы/группы/юзеров (только чтение), найденное не добавляет — предлагает синхронизировать.
- Новый пакет `packs/services.py`: сервисы и триалы из постов (kinds service/trial/promo); ключ дедупа — `dedup_hint` (домен/слаг сервиса): «Триал 30 дней на Acme» и «Acme Cloud trial» — одна сущность. Второй декларативный пакет поверх того же движка.
- `extract.py`: stub-триггеры для services-пакета; повторный прогон без новых сообщений пуст.
- Новый `pipeline.py`: автономный конвейер sync (watch) → extract → digest → monitor одной командой; шаги независимы, сбой шага — в отчёт и exit 1; `--pack`, `--skip sync`, `--days-ahead`, `--send`. Для регулярности — cron.
- Проверки: 334 → 354.

## 0.4.1 — 2026-10-14
Фиксы ревью: производительность на больших объёмах, потери данных, дедупликация:
- `db.py`: `messages.topic_id` канонически NOT NULL (General=1): вставка без темы и `topic_id=0` нормализуются, старые базы с NULL мигрируют при открытии. Все запросы (watch_list/topics/history/search/stats/watch_new/extract/digest/ui) сравнивают `topic_id = ?` напрямую — раньше `COALESCE(m.topic_id,1)` обходил индекс `ix_msg_chat_topic` и на чатах 100k+ сообщений `tg_watch` делал три полных скана чата на тему.
- `extract.py`: валидные операции сверх 50 больше не теряются молча — применяются первые по позиции evidence, остаток фиксируется в новой колонке `extraction_runs.ops_overflow` (status=partial), курсор остаётся до первой отброшенной позиции, следующий прогон дочитывает (zero-progress fallback двигает курсор, чтобы прогон не заклинил).
- `extract.py`: движковая поддержка `dedup_hint` — опционального стабильного идентификатора сущности в операции; ключ дедупа от него сильнее дословного title (разные формулировки одного сервиса дедупятся).
- `extract.py`: флаг `--all-indexed` — область прогона все (chat, topic) индекса, а не только watch-лист (массовая обработка без курирования).
- `export.py`: flush+commit на каждую сущность — краш посреди прогона оставляет максимум один дубль строки вместо повторного экспорта всей пачки.
- `sync.py`: `pull_chat` накапливает new/per_topic через FloodWait-ретраи — сообщения, вставленные до ожидания, не занижают отчётный `new`.
- `server.py`: соединение кэшируется на поток (`_DBHandle`) — схема, миграции и FTS-триггеры не выполняются на каждый tool-call.
- Проверки: 314 → 334.

## 0.4.0 — 2026-10-14
Слой извлечения сущностей поверх индекса (сообщения остаются источником истины, сущности — пересчитываемая проекция):
- Новый `extract.py`: generic движок по декларативным пакетам `packs/`; курсор `extract:<chat>:<topic>:<pack>`, первое окно `backfill_days`, до 200 сообщений за прогон; операции new/update/close/reopen одной транзакцией (`entities`, `entity_events`, `extraction_runs`); сбой провайдера курсор не двигает; `EXTRACT_PROVIDER=stub|openai` (`EXTRACT_URL`/`EXTRACT_KEY`/`EXTRACT_MODEL`), `--pack`, `--dry-run`.
- Новый пакет `packs/construction.py` (PM для стройки): kinds decision/task/promise/question; каждая операция обязана evidence-цитатой, цитата проверяется подстрокой текста сохранённого сообщения (плюс транскрипты) — без валидного evidence операция отклоняется.
- MCP: инструментов стало 13 вместо 10 — новые `tg_entities` (фильтры pack/kind/status/project/chat_id), `tg_entity` (история событий с доказательствами), `tg_overdue` (просроченное и горизонт `days_ahead`). Сервер по-прежнему read-only и не импортирует extract/notify/media.
- Новый `monitor.py`: сводка просроченного и дедлайнов по проектам (`--days-ahead`, `--project`, `--send`); новый `notify.py`: отправка в Saved Messages — единственный пишущий в Telegram модуль, dry-run по умолчанию, реальная отправка только за `TG_ALLOW_SEND=1`, cap `TG_NOTIFY_CAP` (10).
- Новый `media.py`: очередь транскрипции голосовых/документов (`media_assets`) из sync/live; `MEDIA_PROVIDER=stub|openai` (`MEDIA_URL`/`MEDIA_KEY`/`MEDIA_MODEL`); транскрипты подмешиваются в промпт извлечения; openai без скачанных байтов — `NotImplementedError` (загрузка файлов — отдельный инкремент).
- Новый `export.py`: выгрузка сущностей во внешние системы; file-провайдер (JSONL, `--out`/`TG_EXPORT_PATH`), курсор в таблице `integrations`; 101/notion — структурированная ошибка без исключений.
- Мульти-проектность: тег `project` у строк watch-листа (`watch.py add --project`, подкоманда `project`); сущности наследуют проект от темы.
- Тесты: 314 офлайн-проверок.

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
