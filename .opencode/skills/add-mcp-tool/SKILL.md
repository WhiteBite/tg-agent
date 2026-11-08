---
name: add-mcp-tool
description: Добавить новый MCP-инструмент (tg_*) в server.py tg-agent. Use when adding a new agent tool, registering an MCP tool, exposing a new read or write capability to the opencode agent, "новый MCP-инструмент", "инструмент агента", "добавить tg_". Covers the read-only vs gated-write rules, confirm=True, STRICT_WATCH, and the test-counter updates.
---

# Новый MCP-инструмент

MCP-сервер — `server.py`. Инструменты объявляются декоратором на функции,
возвращающей строку JSON.

## 1. Реши: чтение или запись

- **Чтение** (идет в индекс или в Telegram только на чтение) — регистрируется
  безусловно (или под своим read-гейтом, если ходит в сеть: `TG_ALLOW_LIVE`).
- **Запись** (пишет в Telegram, на диск, или пересылает) — строго за гейтом
  `TG_ALLOW_*` и **требует `confirm=True` в каждом вызове**. Инструмент без гейта —
  дефект безопасности. Список гейтов — `docs/gates.md`.

## 2. Объяви функцию

Read-only:

```python
@mcp.tool(
    name="tg_my_tool",
    description="Что делает, каким алиасам/полям отвечает.",
)
def tg_my_tool(chat_id: int, limit: int = 100) -> str:
    with _db() as db:
        rows = db.some_reader(chat_id=chat_id, limit=limit)
    return _j({"rows": rows})
```

- `_j(obj)` — сериализация в JSON (единый выход всех инструментов).
- **Не бросай исключения наружу** — ошибку возвращай как `_j({"error": "..."})`.
- Если инструмент читает watch-темы, оберни гейт `TG_STRICT_WATCH` (см. `_allowed` /
  `_denied`): вне watch-листа верни `access_denied` с подсказкой.

Пишущий — внутри блока гейта, рядом с аналогичными (`tg_manage_*`):

```python
if ALLOW_MYTHING:
    @mcp.tool(name="tg_my_write", description="...")
    def tg_my_write(target: str, confirm: bool = False) -> str:
        if not confirm:
            return _CONFIRM_ERR
        ...
```

## 3. Обнови счётчики в тестах

Инструменты считаются в нескольких местах — новый инструмент их сдвигает:

- `test_offline.py` (проверки `tools/list`);
- `tests_manage.py` (`TG_ALLOW_MANAGE`);
- `tests_downloads.py` (`TG_ALLOW_DOWNLOADS` / `TG_ALLOW_FORWARDS`);
- `tests_announce.py`.

Найди `len(` рядом с `tools` и поправь ожидаемое число. Прогони
`python test_offline.py` — счётчики обязаны сойтись.

## 4. Документация

- таблица инструментов в `README.md`;
- если инструмент за новым гейтом — строка в `docs/gates.md`;
- если у инструмента тексты — они помечаются как данные, не команды.

## 5. Проверка

```bash
python server.py --list      # инструмент в списке
python test_offline.py       # зелёный
```
