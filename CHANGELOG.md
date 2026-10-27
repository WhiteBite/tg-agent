# Changelog

## 0.12.0 — 2026-10-28
Рабочее пространство в браузере (Вариант 0 переписан) и сигналы по вопросам:
- `workspace_store.py`/`workspace_views.py`/`workspace_write.py`/`workspace_search.py`/`workspace_api.py`: HTTP API поверх индекса в `ui.py` (`/api/workspace/*`, общий токен-гейт). Экран «Требует внимания» (просрочки, блокировки, открытые вопросы, обещания без срока/ответственного, непроверенные выводы), таблицы сущностей с фильтрами/сортировкой/пагинацией, карточка с событиями и сообщениями-доказательствами, единый поиск (FTS5 + LIKE-фоллбек), ручная правка полей с аудитом и 409 на устаревшую ревизию, проверка карточек (approved/rejected, новое событие сбрасывает), курсор просмотра проекта по event-id (не двигает `watch.last_read`), привязка тем к проекту, справочник людей; превью 101 и уведомлений — только локальные данные, без сети и отправки.
- `ui.html` + `ui_assets/`: новый фронт по `DESIGN.md` — светлая/тёмная темы, мобильная компоновка, раздел «Переписка» с деревом чатов/тем, watch, алиасами, отметками, FTS и копированием выдержки; старый инлайн-UI удалён. `ui.py` раздаёт `/assets/*` по whitelist; читалки `/api/state|messages|poll` больше не отдают удалённые сообщения.
- `extract.py`: сигналы по вопросам — ответ другого отправителя (`reply_received`) и реакции (`reaction_received`) помечают вопрос на проверку, но не закрывают; завершение — явная операция извлечения или ручная правка статуса.
- Тесты: 13 → 15 suites (`tests_workspace_api.py` — HTTP-тесты на реальном сервере, `tests_question_signals.py`), полный прогон 1275 проверок.

## 0.11.1 — 2026-10-27
Режим `forward` — пересылка медиа без скачивания:
- `modules/downloads.py` + `downloads.py` (CLI): `--mode forward --to saved|@username|<числовой id>` — серверная пересылка сообщений, байты медиа через клиент не идут (один API-вызов на видео). Реестры `forward_files`/`forward_sources`: дедуп по `(id файла, адресат)`, у записи накапливаются все сообщения-источники; контентный sha256-дедуп в этом режиме невозможен — перезалитая копия с другим id проходит как новое видео. Смена адресата — свои пересылки (в план берутся через `--all-messages`).
- Длинный FloodWait (>120 с) уводит задание в `pause`, короткий пережидается молча; каналы с запретом пересылки пропускаются (`forwards-restricted`).
- `db.py`: миграция v8 — `ensure_schema` доваривает таблицы модуля в существующие v7-базы.
- `server.py`: MCP-инструмент `tg_downloads_forward` за отдельным гейтом `TG_ALLOW_FORWARDS=1`, пересылка требует `confirm=True`; в контракте модуля у MCP-спеки появился опциональный `gate` (`downloads`/`forwards`/`any`) — статус и подборки регистрируются при любом из двух гейтов (инструменты: 16 / 19 / 19 / 20).
- `workspace_api.py`: маршруты `POST downloads/forward` и `GET downloads/forwards`.
- Тесты: те же 15 suites, `tests_downloads.py` + forward-проверки (дедуп по файлу и адресату, FloodWait-пауза, запрет пересылки, MCP-гейты, миграция v7→v8), проверки 1240 → 1275.

## 0.11.0 — 2026-10-27
Модульная архитектура и скачивание видео с дедупликацией:
- `modules/` — явный реестр функциональных модулей (контракт: id, version, schema, workspace-маршруты, MCP-спеки, runner); `TG_MODULES_OFF="downloads"` убирает модуль целиком — без маршрутов, инструментов и таблиц. Следующие модули добавляются без правки ядра.
- `modules/downloads.py` + `downloads.py` (CLI): скачивание video/videonote/photo из источников `watch|all|folder:<id>|collection:<имя>|chat:<id|@username>` (объединение через запятую). Дедуп точных дублей двумя слоями: до скачивания — по id файла Telegram (пересланный ролик не качается повторно), после — по sha256 содержимого (повторная загрузка того же видео алиасится на скачанный файл). Хранение контентно-адресное (`downloads/files/<sha[:2]>/<sha><ext>`), у файла — все сообщения-источники; повторный прогон не трогает собранное. Лимит размера — `TG_DOWNLOADS_MAX_MB` (дефолт 2000), каталог — `TG_DOWNLOADS_DIR`.
- `jobs.py`: сохраняемые задания (`jobs`/`job_items`) — snapshot пунктов на момент создания, claim с защитой от гонок, pause/resume/cancel, `--retry --force` дожимает упавшие; один пункт не обрывает пакет, причины пропусков и падения — в отчёте задания.
- `sources.py`: подборки каналов (`collections`) — сохранённые группы для повторяемых заданий; watch-лист и доступ агента не меняют.
- `db.py`: миграция v7 — таблицы заданий/подборок в ядре, DDL модулей выполняется централизованно из реестра. `workspace_api.py`: маршруты `downloads/*` (задание, запуск, пауза/отмена, файлы, подборки).
- `server.py`: MCP-инструменты `tg_downloads_run`/`tg_downloads_status`/`tg_downloads_collections` за гейтом `TG_ALLOW_DOWNLOADS=1`, запуск задания требует `confirm=True`; базовая поверхность не изменилась (16 инструментов).
- Тесты: 14 → 15 suites (`tests_downloads.py`, 110 проверок: дедуп doc-id/sha256 + алиасы, resume, пауза/отмена, подборки, реестр и `TG_MODULES_OFF`, миграция v6→v7, MCP-гейт, фоновый загрузчик), проверки 1130 → 1240.

## 0.10.2 — 2026-10-21
Релиз-посты: четвёртый пишущий сценарий поверх manage-гейта:
- `announce.py`: пост по YAML-шаблону (badge/title/what/inside/ps/warning/links/banner) с баннером PIL 1080x720, кросспост `--also`, реакция `--react`, пин `--pin`; readme темы или продукта из `org.yaml`; черновик шаблона из CHANGELOG репозитория (`draft`); правка отправленного поста (`update`). Без `--send` — dry-run; отправка за `TG_ALLOW_MANAGE=1`.
- `org.yaml`: реестр продуктов WhiteBite — репо, ссылки, эмодзи, разметка тем.
- `db.py`: миграция v5 — таблица `posts` (что уходило, в какой чат/тему, kind, хэш).
- `server.py`: MCP-инструмент `tg_manage_post` — регистрируется только при `TG_ALLOW_MANAGE=1`, требует `confirm=True`.
- Тесты: 11 → 12 suites (`tests_announce.py`), проверки 1076 → 1113.

## 0.10.0 — 2026-10-21
Третий пишущий модуль: создание и управление чатами, всё за гейтом `TG_ALLOW_MANAGE=1`:
- `manage.py`: создание групп/каналов/форумов с регистрацией в индексе (`created_by_me=1`, `my_role=owner`), настройки (title/about/photo/username, тумблеры pre_history/invites/signatures/forum, дефолтные права), темы форума (создание/правка/закрытие), участники (invite/admin/kick/ban) и инвайт-ссылки (expire/usage_limit, хранение в индексе). Без `TG_ALLOW_MANAGE=1` любая операция возвращает ошибку без запросов; контент не трогается.
- `db.py`: миграция v4 — колонки `created_by_me`/`my_role` у `chats`, таблица `invite_links`.
- `server.py`: MCP-инструменты `tg_manage_create`/`tg_manage_edit`/`tg_manage_topic`/`tg_manage_members`/`tg_manage_link` регистрируются только при `TG_ALLOW_MANAGE=1`; каждая операция требует явный `confirm=True`, иначе ошибка.
- Тесты: 10 → 11 suites (`tests_manage.py`), проверки 945 → 1076.

## 0.9.1 — 2026-10-20
Экспорт 101 под реальный REST-контракт (сваггер `101 API.yaml`):
- `export.py`: auth 101 через JWT — `EXPORT_101_TOKEN` либо `EXPORT_101_LOGIN`+`EXPORT_101_PASSWORD` (вход `/api/auth/sign-in/`, refresh пары на 401 через `/api/auth/refresh-token/`); сущности уходят задачами в `/api/tasks/v2/` (create POST, при `integrations.external_id` — PATCH `.../{guid}/`), поля под контракт — `EXPORT_101_MAP`, kinds — `EXPORT_101_KINDS`, комментарий события — `EXPORT_101_COMMENTS=1`, `EXPORT_101_DRY=1` — без отправки. Тело задачи в сваггере не описано — дефолтный маппинг консервативен и настраивается.
- `tests_export_101.py`: переписаны под контракт (sign-in, 401→refresh, create/PATCH/комментарий, dry-run, ретраи, continue-on-error).

## 0.9.0 — 2026-10-20
Офлайн-бэклог под PM-кейс: статусная модель, авто-закрытие вопросов, реакции, читалки, маршрутизация сводок, наблюдаемость:
- `db.py`/`extract.py`/`packs/`/`services_report.py`: статусы `open/in_progress/blocked/done` (терминал — `closed_ts`); ops `start/block/unblock` с проверкой переходов; `close`→`done`; миграция v3 `closed`→`done`; `update` не трогает статус.
- `extract.py`/`packs/construction.py`: авто-закрытие открытого вопроса ответом другого отправителя (op `auto_close`) и подтверждающими реакциями (`autoconfirm`).
- `live.py`/`db.py`: захват реакций в индекс (`handle_reactions`/`update_reactions`, агрегатные счётчики).
- `db.py`: читалки `entity_changes`/`project_brief`/`project_gaps` + фильтр `owner`.
- `server.py`: read-only инструменты `tg_changes`/`tg_project_brief`/`tg_gaps` (+`owner` в `tg_entities`) — 16 инструментов.
- `monitor.py`/`notify.py`: обещания без срока/ответственного и «застряло»; маршрутизация сводок по проектам (`TG_NOTIFY_ROUTES`, персональные хэш-ключи).
- `ui.py`/`ui.html`: панель здоровья проектов `/api/projects` + курсор визита.
- `doctor.py`: `--cost` (расход токенов по `extraction_runs`).
- `pipeline.py`/`watchdog.py`: штамп `pipeline:last_ok_ts` и проверка свежести прогона.
- `packs/__init__.py`: валидация схем паков на импорте; `export.py`: провайдеры из конфига (`TG_EXTRA_PROVIDERS`).
- Тесты: 6 → 10 suites (`tests_readers`/`tests_pack_schema`/`tests_watchdog`/`tests_ui_projects`), проверки 761 → 945.

## 0.8.0 — 2026-10-19
Миграции по user_version, срок понимает русские даты, утренник чинится, тесты срослись:
- `db.py`: миграции гейтятся `PRAGMA user_version` (DDL больше не повторяется на каждом открытии — штормы `database is locked` уходят); read-only путь `DB(path, readonly=True)` без миграций и с `query_only`; `deleted=0` учитывается во всех читалках, `mark_deleted` вычищает строку из FTS (удалённое не ищется, отредактированное — по новому тексту, триггер `AFTER UPDATE OF text`); `watch_set_project` каскадит открытые сущности; новые колонки `extraction_runs.rejects_json`, `integrations.external_id`; meta-ключ `dirty:{chat}:{topic}` копит id правок/удалений для перепрогона.
- Срок из due_text: русские названия месяцев («19 августа»), «N числа», относительные («сегодня/завтра/послезавтра», «через N дней/нед/месяц») и ISO — резолвятся по дате сообщения; deadline ставится на конец дня (23:59:59 UTC), update не понижает ранее точный `due_ts`.
- Из схем пакетов убраны `covered_upto_msg_id` (курсор всегда идёт на max msg_id пачки, overflow-clamp оставлен) и `confidence` (колонка есть, не заполняется); evidence — `minItems: 1`, нормализация цитаты сводит ё→е; причины отказа пишутся в `rejects_json` (до 50 за прогон).
- Scope: `require_match` — construction требует непустой проект и исключает `^сервисы$` (кросс-проекное загрязнение кончилось); `dedup_scope` — construction дедупится по проекту, services остаётся глобальным; dirty-сообщения перепрогоняются.
- `export.py` 101: курсор по `entity_events.id` вместо вторичного ts (односекундные события терялись молча), 3 ретрая с backoff на 5xx/сеть, continue-on-error со списком `failed`, `Idempotency-Key: {entity_id}-{max_event_id}`, ответный id в `integrations.external_id`, курсор только после 2xx. notion — по-прежнему заглушка.
- Ops: notify отправляет в worker-потоке на свежем loop — утренний `pipeline --send` работает внутри живого цикла (раньше всегда падал); `PIPELINE_STEP_BUDGET` (900с) — зависший шаг помечается упавшим; `run()` берёт proclock-лок (one-shot не гоняется параллельно с демоном); `--loop` жуёт extract окном (`until_done=False`); отчёт extract несёт `ops_rejected`/`ops_overflow`. Monitor: дедуп `--send` по хэшу отчёта (`TG_MONITOR_FORCE=1` обходит), блок зависших вопросов (`MONITOR_QUESTION_DAYS`, дефолт 3), ограниченные сканы. Digest: `DIGEST_MAX_ROWS` (дефолт 500). Doctor: `--providers` (opt-in health-check, мёртвый ключ = HTTP 401) и `--quality` (заполненность полей + гистограмма отказов).
- Медиа: `--sweep-pending` — разовый дренаж легаси-pending в skipped без скачивания. `tg_mark_read` отклоняет id выше максимума индекса темы — единственный мутирующий инструмент ограничен.
- Тесты: suites слиты в один прогон — `test_offline.py` запускает шесть подпроцессов (плюс `tests_ops_report.py`, `tests_media_ingest.py`, `tests_db_core.py`), общая temp-база убрана (`tempfile.mkdtemp`); проверки: 594 → 761.

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
