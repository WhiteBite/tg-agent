---
name: release
description: Оформить релиз tg-agent и релиз-пост в Telegram (CHANGELOG + announce.py + org.yaml). Use when cutting a release, bumping the version, publishing a release post or topic readme, "релиз", "релиз-пост", "announce", "CHANGELOG", "новая версия". Covers the CHANGELOG entry, the announce.py draft/post/readme flows and the TG_ALLOW_MANAGE gate.
---

# Релиз

## 1. Версия и CHANGELOG

- Подними версию и добавь запись в `CHANGELOG.md` (формат как в истории:
  `## X.Y.Z — YYYY-MM-DD`, подзаголовок и список изменений по файлам).
- Значимые пользовательские фичи оформляй записью в разделе текущей версии, а не
  только в коммитах.

## 2. Проверка

```bash
python test_offline.py       # зелёный до релиза
git log --oneline <last-tag>..HEAD   # что вошло
```

## 3. Релиз-пост

`announce.py` собирает пост из YAML-шаблона (баннер PIL 1080x720, если задан).
Без `--send` любой режим — dry-run: печатает текст и сохраняет баннер, в Telegram
не ходит.

```bash
python3 announce.py draft --repo ../tg-agent --tag vX.Y.Z --product tg-agent --tpl-out post.yaml
python3 announce.py post --tpl post.yaml --chat @whitebite_devsoft --topic 323 --send --react 🔥 --pin
python3 announce.py readme --topic-title "…" --chat @whitebite_devsoft --topic 325 --send
```

Реестр продуктов (репо, ссылки, эмодзи, разметка тем) — `org.yaml`; `links_product`
берёт ключ продукта оттуда.

## 4. Гейт

Реальная отправка (кросспост `--also`, реакция `--react`, пин `--pin`) — только
при `TG_ALLOW_MANAGE=1`. У агента зеркало — `tg_manage_post`, тот же гейт и
`confirm=True` (см. `docs/gates.md`).

## 5. Коммит

Один коммит на задачу, формат как в `git log` (`docs:`, `release:`). Без атрибуции
агента/модели. Живые посты в Telegram в CI не проверяются — это ручной шаг.
