# Гейты и конфигурация

Все пишущие и сетевые возможности закрыты переменными окружения. По умолчанию
каждый гейт выключен: без переменной инструмент либо не регистрируется, либо
работает в dry-run. Значения-гейты включаются как `1` / `true` / `yes`
(регистр не важен).

Полное релевантное окружение — в `.env.example`; здесь — смысл каждого гейта и
кто его читает.

## Гейты MCP-сервера (`server.py`)

| Переменная | Что открывает | Запись? | Читает |
|---|---|---|---|
| `TG_ALLOW_LIVE=1` | `tg_live_search`, `tg_live_history`, `tg_live_topics`, `tg_live_check_invites`, `tg_live_check_mega`, `tg_live_check_mailru`, `tg_live_check_links` — живые запросы к Telegram | нет (только чтение) | `server.py` |
| `TG_ALLOW_SEND=1` | `tg_send_message` + реальная отправка `notify.py` в Saved Messages | да | `server.py`, `notify.py` |
| `TG_ALLOW_MANAGE=1` | `tg_manage_*` (создание/настройка чатов, участники, инвайты) и `tg_manage_post` | да | `server.py`, `manage.py`, `announce.py` |
| `TG_ALLOW_DOWNLOADS=1` | `tg_downloads_run/status/collections` — скачивание медиа на диск | да (на диск) | `server.py`, `modules/downloads.py` |
| `TG_ALLOW_FORWARDS=1` | `tg_downloads_forward` — серверная пересылка медиа в адресата | да | `server.py`, `modules/downloads.py` |
| `TG_ALLOW_ENGINE=1` | пуск/стоп демонов `pipeline --loop` и `live` из панели «Движок» | да (процессы) | `engine.py` |
| `TG_EXPOSE_SQL=1` | `tg_sql` — произвольный read-only SQL | нет | `server.py` |
| `TG_STRICT_WATCH=1` | жёсткий гейт: читалки и сущности только по темам watch-листа | нет | `server.py` (см. `_allowed` / `_denied`) |

Модуль может объявить в своей MCP-спеке любой гейт: `gate` — это либо
`"downloads"` / `"forwards"` / `"any"` (алиасы `TG_ALLOW_DOWNLOADS` /
`TG_ALLOW_FORWARDS`), либо имя произвольной env-переменной (напр. `TG_ALLOW_X`).
Инструмент регистрируется, когда соответствующий гейт включён.

Все `tg_manage_*`, `tg_downloads_run`, `tg_downloads_forward`, `tg_send_message`
требуют `confirm=True` в каждом вызове — без него возвращают ошибку и ничего не
делают.

## Гейты демонов и конвейера

| Переменная | Что включает | Читает |
|---|---|---|
| `TG_ALLOW_PULL=1` | применение обратной синхронизации `export.py --pull --apply` (импорт изменений из внешней системы в индекс) | `export.py` |
| `TG_ALERTS=1` | живые алерты `live.py` (слова-триггеры и упоминания → Saved Messages) | `alerts.py`, `live.py` |
| `TG_ENTITY_ALERTS=1` | сущностные алерты после шага extract (виды из `TG_ENTITY_ALERT_KINDS`, дефолт trial,question) | `entity_alerts.py`, `pipeline.py` |
| `TG_MODULES_OFF="downloads,…"` | убирает модуль из реестра: ни маршрутов, ни MCP-инструментов, ни схемы, ни исполнителя; файл модуля не импортируется | `modules/__init__.py` |
| `TG_EXTRA_MODULES=<путь>` | сторонний `.py` со словарём `MODULES` (или списком) — модули поверх встроенных, с той же валидацией и учётом `TG_MODULES_OFF` | `modules/__init__.py` |
| `TG_EXTRA_PACKS=<путь>` | сторонний `.py` со словарём `PACKS` — пакеты извлечения поверх встроенных, с той же валидацией | `packs/__init__.py` |

## Сопутствующий лимит

| Переменная | Смысл | Читает |
|---|---|---|
| `TG_NOTIFY_CAP=10` | максимум сообщений за один вызов `notify.py` / `tg_send_message` | `notify.py` |

## Конфигурация, не гейт

Это не гейты, но их полезно знать при запуске: `TG_DB` (путь к индексу),
`TELEGRAM_API_ID` / `TELEGRAM_API_HASH` / `TELEGRAM_SESSION_*` (креды и сессия),
`TG_UI_TOKEN` (обязателен при `HOST=0.0.0.0`), `TG_ENV_FILE` (что читать вместо
`.env`), `EXTRACT_*` / `DIGEST_*` / `MEDIA_*` (провайдеры LLM и ASR),
`TG_DOWNLOADS_DIR` / `TG_DOWNLOADS_MAX_MB`, `TG_EXPORT_PATH` / `EXPORT_101_*`,
`TG_NOTIFY_ROUTES` / `TG_ME_OWNERS` / `MONITOR_QUESTION_DAYS`.

`TG_BOT_TOKEN` / `TG_BOT_URL` — ингест через Telegram Bot API (`bot_ingest.py`):
читает Telegram в локальный индекс, в Telegram не пишет, поэтому это не гейт
`TG_ALLOW_*`, а та же категория, что `sync.py`. `NOTION_*` (`NOTION_TOKEN`,
`NOTION_DATABASE_ID`, `NOTION_MAP`, `NOTION_KINDS`, `NOTION_API_VERSION`,
`NOTION_DRY`, `NOTION_RETRIES`, `NOTION_BACKOFF`, `NOTION_TIMEOUT`) — внешняя
интеграция экспорта (`export.py --system notion`), тоже не гейт `TG_ALLOW_*`.
`shiftwatch.py` пишет только локальную БД (производная проекция), гейт не нужен.

## Профили конфигурации

Профиль — именованный набор env-значений, включаемый одной переменной. Это не
гейт: он лишь задаёт дефолты для ключей, которых нет в реальном окружении.

| Переменная | Смысл | Читает |
|---|---|---|
| `TG_PROFILE=<имя>` | включает профиль `<имя>`; не задана — полный no-op (ни чтения БД, ни изменений окружения) | `env.py` |
| `TG_PROFILE_FILE=<путь>` | env-файл профиля; по умолчанию `.env.<имя>` рядом с основным `.env` | `env.py` |

Два источника профиля, приоритет (выше → ниже): **окружение процесса** >
**env-файл профиля** > **meta-профиль** > **основной `.env`** > **встроенные
дефолты**. Env-файл профиля парсится тем же форматом, что `.env`; meta-профиль —
JSON-объект в ключе `config:profile:<имя>` (пишет `profile.py`).

Профиль применяется только к ключам, которых не было в окружении процесса до
`load_env()`: реальное окружение всегда побеждает, а профиль перекрывает
основной `.env`. Ключи `TG_DB`, `TG_PROFILE`, `TG_PROFILE_FILE`, `TG_ENV_FILE`
профиль задавать не может — они управляют его же разрешением и выбором базы
(иначе рекурсия и нестабильность). Meta-профиль читается read-only; нет базы
или ключа — молча пропускается, env-файл профиля всё равно применяется.

```bash
python3 profile.py set --name prod TG_ALLOW_SEND=1 EXTRACT_PROVIDER=openai
python3 profile.py set --name prod --json '{"TG_ALLOW_SEND":"1"}'
python3 profile.py list
python3 profile.py show --name prod
python3 profile.py remove --name prod
TG_PROFILE=prod python3 server.py
```

Добавляешь новый гейт — впиши строку в первую таблицу и обнови счётчики
инструментов в тестах (см. `AGENTS.md`).
