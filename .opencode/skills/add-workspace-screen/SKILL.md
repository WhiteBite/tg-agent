---
name: add-workspace-screen
description: Добавить экран в браузерное рабочее пространство tg-agent (ui_assets + ui.py + workspace_api). Use when adding a workspace screen, a new UI section, frontend tab, "новый экран", "раздел UI", "workspace_*.js", wiring an asset into ui.html. Covers the four wiring points, DESIGN.md tokens and tests_workspace_api.py.
---

# Новый экран рабочего пространства

Фронтенд — ванильный JS без фреймворка: `ui.html` + `ui_assets/`. Экран сам себя
не появится — его надо подключить в четырёх местах.

## Четыре точки проводки

1. **Ассет**: создай `ui_assets/workspace_<name>.js` рядом с существующими.
2. **Whitelist `ui.py`**: добавь имя файла в множество `allowed` в обработчике
   `/assets/` (иначе отдаст 404).
3. **`ui.html`**: подключи `<script>` и пункт навигации.
4. **Маршруты**: добавь серверные ручки в `workspace_api.py` /
   `workspace_views.py` (чтение из индекса) и, если нужно, `workspace_write.py`.

## Дизайн

Источник истины — `DESIGN.md`. Не вводи третий акцент, держи pill-геометрию,
светлую/тёмную/системную темы, брейкпоинты 900/600px и тач-цели ≥ 44px.
Никаких внешних CDN/шрифтов — данные и шрифт Inter локальны.

## Доступность и состояния

Каждый экран обязан иметь: пустое состояние с причиной и действием, состояние
загрузки, обработку ошибок рядом с упавшим контентом, фокус-ring и Escape для
диалогов. Не показывай выдуманные итоги.

## Тест

- `tests_workspace_api.py` поднимает настоящий `ui.py` (ThreadingHTTPServer,
  токен): добавь проверки новых маршрутов, фильтров, 401/415;
- прогон — `python test_offline.py`;
- визуально — в `.playwright-mcp/` артефактах / `visual-qa`.

Аккуратно с токенами в строках: имена полей и значения локализуй словарями
`W.fieldLabel` / `W.valueLabel`, а не сырыми машинными значениями.
