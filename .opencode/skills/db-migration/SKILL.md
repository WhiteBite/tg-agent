---
name: db-migration
description: Добавить миграцию схемы индекса tg-agent (db.py, PRAGMA user_version). Use when changing the SQLite schema, adding a table or column, "миграция", "схема БД", "user_version", "add column", bump SCHEMA_VERSION. Covers the idempotency rule, the _migrate_* chain and tests_db_core.py.
---

# Миграция схемы

Схема индекса живёт в `db.py`. Версия — `SCHEMA_VERSION`, шаг вперёд идёт только
через миграцию; DDL на каждом открытии запрещён.

## Механика

`DB.__init__` читает `PRAGMA user_version`. Если `version >= SCHEMA_VERSION` —
пропускает всё. Иначе гоняет базовый `SCHEMA`, затем цепочку
`if version < N: self._migrate_vN()` и в конце пишет
`PRAGMA user_version = SCHEMA_VERSION`. Свежая база приходит с `user_version=0`,
поэтому **каждый шаг миграции обязан быть идемпотентным**.

## Шаги

1. Подними `SCHEMA_VERSION = N` → `N+1` в `db.py`.
2. Добавь метод `_migrate_vN(self)` рядом с `_migrate_v10` и т.п. Внутри —
   идемпотентный DDL: `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN`
   под проверкой существования колонки (`PRAGMA table_info`), без потери данных.
3. Добавь вызов в цепочку: `if version < N: self._migrate_vN()` — строго после
   предыдущего шага, строго до `PRAGMA user_version = SCHEMA_VERSION`.
4. Если миграцию триггерит модуль — она уже вызывается через
   `modules.ensure_schema(conn)`; отдельный шаг не нужен (см. `modules/AGENTS.md`).

## Тест

Добавь `check` в `tests_db_core.py`:

- миграция старой базы (создать базу на прошлой версии, открыть — версия
  поднялась, данные целы);
- идемпотентность (повторное открытие ничего не ломает);
- свежая база сразу на `SCHEMA_VERSION`.

```bash
python test_offline.py       # зелёный — условие коммита
```

Read-only соединение (`DB(path, readonly=True)`) миграции **не** запускает —
учитывай это, если читаешь новую колонку из читалок.
