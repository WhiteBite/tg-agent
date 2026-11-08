---
name: add-module
description: Добавить функциональный модуль в реестр modules/ tg-agent (как downloads). Use when building a new pluggable feature with its own schema, HTTP routes, MCP tools and background runner, "новый модуль", "modules/", "следующий модуль после downloads", "TG_MODULES_OFF". Covers the dict contract, the three core wiring points and reuse of jobs.py/sources.py.
---

# Новый функциональный модуль

Модуль — самодостаточная фича со своим контрактом. Ядро тонкое. Образец —
`modules/downloads.py`. Правило валидации — `_validate` в `modules/__init__.py`.

## Контракт (dict)

| Поле | Тип | Смысл |
|---|---|---|
| `id` | `str` | непустой id |
| `version` | `int` | версия схемы |
| `schema` | `fn(conn)` | идемпотентный DDL, зовётся из миграции `db.py` |
| `workspace` | `{(method, path): fn}` | `GET` → `fn(db, params)`, `POST` → `fn(db, body)` |
| `mcp` | `[{name, description, fn, gate?}]` | `name` на `tg_`, `gate` ∈ `None/downloads/forwards/any` |
| `runner` | `fn(job_id) -> dict` | фоновый исполнитель задания |

Ключ маршрута — строго `(method, path)`, путь без ведущего/хвостового слэша,
`confirm=True` живёт в самой функции, а не в декларации.

## Три точки подключения ядра

1. `db.py` — миграция вызывает `modules.ensure_schema(conn)` (твои таблицы
   создаются там, без отдельного `_migrate_*`).
2. `workspace_api.py` — маршруты из `modules.workspace_routes()`.
3. `server.py` — инструменты из `modules.mcp_specs()` под своим гейтом (`_gate_on`).

Плюс зарегистрируй сам модуль в списке в `modules/__init__.py`.

## Переиспользуй общее

- сохраняемые задания — `jobs.py` (`jobs`/`job_items`, claim, pause/resume/cancel,
  retry), не пиши своё;
- выбор источников и подборки — `sources.py`.

## Отключение

`TG_MODULES_OFF="downloads,<id>"` убирает модуль целиком: ни маршрутов, ни
инструментов, ни исполнителя, ни схемы. Проверь, что модуль не оставляет следов в
ядре при выключении.

## Тест

По образцу `tests_downloads.py`: реестр, `TG_MODULES_OFF`, MCP-гейты, миграции,
дедуп/resume, фоновый исполнитель. Прогон — `python test_offline.py`.
