---
name: add-pack
description: Добавить декларативный пак извлечения сущностей в packs/. Use when creating a new extraction domain, a new entity kind, "новый пак", "новый вид сущностей", "packs/", adding a construction/services-like pack. Covers the pack contract, json_schema/ops consistency, evidence, scope_filter, dedup_scope and tests_packs_semantics.py.
---

# Новый пак извлечения

Пак описывает предметную область декларативно. Движок `extract.py` generic и не
меняется. Контракт проверяет `_validate` в `packs/__init__.py` на импорте.

## 1. Скопируй образец

Возьми `packs/construction.py` или `packs/services.py` за шаблон. Обязательные поля:

`id`, `version`, `kinds` (непустой dict), `prompt_template`, `ops`,
`json_schema`, `backfill_days` (int), `field_policies` (dict).

## 2. Согласуй промпт и схему

- `prompt_template` **обязан** содержать плейсхолдеры `{scope_header}`,
  `{messages}`, `{open_entities}` — валидатор упадёт без них.
- `ops` — подмножество `new / update / close / reopen / start / block / unblock`.
- В `json_schema` enum операции (`properties.ops.items.properties.op.enum`)
  обязан **точно** совпадать с `ops`, иначе пак не импортируется.

## 3. Задай область и дедуп

- `scope_filter` — где пак запускается: `require_match` (нужен непустой проект) и
  `exclude_project_regex` (изоляция от чужих тем, напр. `^сервисы$`).
- `dedup_scope` — `project` или `global`.
- Ключ дедупа `dedup_hint` (домен/слаг): строки с одним хинтом схлопываются в
  одну сущность.

## 4. Evidence — не ослабляй

Каждая операция несёт цитату (`minItems: 1`), цитата проверяется как подстрока
текста сохранённого сообщения. Операция без валидной цитаты отклоняется — это
инвариант движка, из пака его не обойти.

## 5. Зарегистрируй и протестируй

- добавь пак в `PACKS` в `packs/__init__.py`;
- семантика — `tests_packs_semantics.py` (due_text, изоляция scope, supersedes,
  схемы), схема — `tests_pack_schema.py`;
- прогон вручную: `EXTRACT_PROVIDER=stub python3 extract.py --pack <id> --dry-run`.

Проект у темы watch — `python3 watch.py add … --project "..."`; сущности наследуют
его от темы.
