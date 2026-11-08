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

Все `tg_manage_*`, `tg_downloads_run`, `tg_downloads_forward`, `tg_send_message`
требуют `confirm=True` в каждом вызове — без него возвращают ошибку и ничего не
делают.

## Гейты демонов и конвейера

| Переменная | Что включает | Читает |
|---|---|---|
| `TG_ALERTS=1` | живые алерты `live.py` (слова-триггеры и упоминания → Saved Messages) | `alerts.py`, `live.py` |
| `TG_ENTITY_ALERTS=1` | сущностные алерты после шага extract (виды из `TG_ENTITY_ALERT_KINDS`, дефолт trial,question) | `entity_alerts.py`, `pipeline.py` |
| `TG_MODULES_OFF="downloads,…"` | убирает модуль из реестра: ни маршрутов, ни MCP-инструментов, ни схемы, ни исполнителя | `modules/__init__.py` |

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

Добавляешь новый гейт — впиши строку в первую таблицу и обнови счётчики
инструментов в тестах (см. `AGENTS.md`).
