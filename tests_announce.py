#!/usr/bin/env python3
"""announce.py: рендер баннера, сборка текста, отправка с кросспостом/реакцией/пином,
readme тем из org.yaml, черновик из CHANGELOG, правка поста, гейт TG_ALLOW_MANAGE."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ["TG_ENV_FILE"] = ""

sys.path.insert(0, str(Path(__file__).parent))

from db import DB  # noqa: E402

import announce as announce_mod  # noqa: E402

FAILURES: list[str] = []

ORG = {
    "forum": "@wb_forum",
    "links": {"чат": "https://t.me/wb_forum"},
    "products": {
        "tg-agent": {"emoji": "🤖", "title": "tg-agent",
                     "repo": "https://github.com/WhiteBite/tg-agent",
                     "about": "Telegram-агент с MCP"},
        "stitch": {"emoji": "🪡", "title": "StitchWB",
                   "repo": "https://github.com/StitchWB/Stitch-Manager",
                   "about": "Менеджер аккаунтов AI IDE"},
    },
    "topic_products": {
        "🤖 AI-агенты и MCP": ["tg-agent"],
    },
}

TPL = {
    "badge": "🎃",
    "title": "1967 ДЖЕЙЛОВ",
    "what": "архив jailbreak-промптов под 27 моделей",
    "inside": ["выбор по модели", "живой рейтинг"],
    "ps": "бот только в бете",
    "warning": "ВАЖНО: на свой страх и риск",
    "kind": "release",
    "banner": {"title": "1967 ДЖЕЙЛОВ", "stats": "238 ПРОВЕРЕНО LIVE",
               "badges": ["27 МОДЕЛЕЙ", "БЕЗ РЕГИ"], "handle": "@jb_bot"},
}


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAILURES.append(label)


class FakeClient:
    def __init__(self) -> None:
        self.sent: list = []
        self.requests: list = []
        self._next = 500

    async def send_message(self, peer, text, file=None, parse_mode=None, reply_to=None):
        self._next += 1
        self.sent.append({"peer": peer, "text": text, "file": file,
                          "parse_mode": parse_mode, "reply_to": reply_to})
        return SimpleNamespace(id=self._next)

    async def edit_message(self, peer, msg_id, text, file=None, parse_mode=None):
        self.sent.append({"peer": peer, "msg_id": msg_id, "text": text,
                          "file": file, "edit": True})
        return SimpleNamespace(id=msg_id)

    async def get_entity(self, ref):
        return ref

    async def __call__(self, req):
        self.requests.append(req)
        return SimpleNamespace()


def _gate(on: bool):
    if on:
        os.environ["TG_ALLOW_MANAGE"] = "1"
    else:
        os.environ.pop("TG_ALLOW_MANAGE", None)


def test_render(tmp: Path) -> None:
    print("=== announce: рендер баннера ===")
    out = announce_mod.render_banner(TPL["banner"], tmp / "banner.png")
    size = None
    try:
        from PIL import Image
        with Image.open(out) as img:
            size = img.size
    except Exception as exc:  # noqa: BLE001
        size = f"ошибка: {exc}"
    check(out.is_file(), "render: файл баннера создан", str(out))
    check(size == (1080, 720), "render: размер 1080x720", str(size))


def test_compose(tmp: Path) -> None:
    print("=== announce: сборка текста ===")
    text = announce_mod.compose_text(TPL, ORG, links_product="tg-agent")
    check("🎃 1967 ДЖЕЙЛОВ" in text and "⚪ ЧТО ЭТО" in text
          and "архив jailbreak" in text,
          "compose: заголовок и секция ЧТО ЭТО", text[:60])
    check("• выбор по модели" in text and "• живой рейтинг" in text,
          "compose: буллеты ЧТО ВНУТРИ")
    check("Ps: бот только в бете" in text and "ВАЖНО: на свой страх и риск" in text,
          "compose: ps и warning")
    check("[tg-agent](https://github.com/WhiteBite/tg-agent)" in text
          and "[чат](https://t.me/wb_forum)" in text,
          "compose: ссылочная строка продукта и org links", text[-90:])


def test_post_dry(tmp: Path) -> None:
    print("=== announce: post без --send ===")
    tpl_path = tmp / "post.yaml"
    import yaml
    tpl_path.write_text(yaml.safe_dump(TPL, allow_unicode=True), encoding="utf-8")
    res = asyncio.run(announce_mod.post_file(str(tpl_path), "@chan", send=False, org=ORG))
    check(res.get("ok") is True and res.get("dry_run") is True
          and Path(res["banner"]).is_file(),
          "post dry-run: превью и баннер, без отправки", str(res)[:90])


def test_post_send(tmp: Path) -> None:
    print("=== announce: post с отправкой ===")
    p = tmp / "post_send.sqlite"
    _gate(True)
    try:
        client = FakeClient()
        res = asyncio.run(announce_mod.post(
            client, TPL, -1002000001, topic_id=323,
            also=("@news",), react="🔥", pin=True,
            links_product="tg-agent", db_path=str(p), org=ORG))
        first = client.sent[0] if client.sent else {}
        names = [type(r).__name__ for r in client.requests]
        row = None
        with DB(p) as db:
            row = db.post_get(-1002000001, res.get("msg_id", 0))
        check(res.get("ok") is True and len(client.sent) == 2,
              "post: отправка в чат и кросспост --also", str(len(client.sent)))
        check(first.get("reply_to") == 323 and first.get("parse_mode") == "md"
              and Path(first.get("file") or "").is_file(),
              "post: reply_to в тему, markdown, баннер файлом", str(first.get("reply_to")))
        check(client.sent[1].get("reply_to") is None,
              "post: кросспост без reply_to")
        check(names == ["SendReactionRequest", "UpdatePinnedMessageRequest"],
              "post: реакция и пин запросами", str(names))
        check(row is not None and row["kind"] == "release"
              and row["product"] == "tg-agent" and row["topic_id"] == 323
              and row["tpl_hash"],
              "post: строка posts с kind/product/topic/hash",
              str(dict(row)) if row else "нет строки")
    finally:
        _gate(False)


def test_readme(tmp: Path) -> None:
    print("=== announce: readme темы из org.yaml ===")
    p = tmp / "readme.sqlite"
    _gate(True)
    try:
        client = FakeClient()
        res = asyncio.run(announce_mod.readme(
            client, "🤖 AI-агенты и MCP", ORG, -1002000001,
            topic_id=327, send=True, db_path=str(p)))
        text = client.sent[0]["text"] if client.sent else ""
        names = [type(r).__name__ for r in client.requests]
        row = None
        with DB(p) as db:
            row = db.post_get(-1002000001, res.get("msg_id", 0))
        dry = asyncio.run(announce_mod.readme(None, "tg-agent", ORG, "@x", send=False))
        check("https://github.com/WhiteBite/tg-agent" in text
              and "Что обсуждаем в этой теме" in text,
              "readme: ссылки продуктов темы и шапка", text[:80])
        check(names == ["UpdatePinnedMessageRequest"],
              "readme: пин по умолчанию", str(names))
        check(row is not None and row["kind"] == "readme",
              "readme: kind=readme в posts", str(dict(row)) if row else "нет строки")
        check(dry.get("dry_run") is True and "StitchWB" not in dry.get("text", "")
              and "tg-agent" in dry.get("text", ""),
              "readme продукта: dry-run только про продукт", dry.get("text", "")[:60])
    finally:
        _gate(False)


def test_draft(tmp: Path) -> None:
    print("=== announce: черновик из CHANGELOG ===")
    repo = tmp / "repo"
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text(
        "## 0.10.0\n- фича один\n- фича два\n\n## 0.9.0\n- старьё\n",
        encoding="utf-8")
    tpl = announce_mod.draft(repo, "0.10.0", ORG, product_key="tg-agent")
    check(tpl.get("inside") == ["фича один", "фича два"],
          "draft: буллеты секции тега", str(tpl.get("inside")))
    check("0.10.0" in tpl.get("title", "") and "tg-agent" in tpl.get("title", ""),
          "draft: заголовок из продукта и тега", str(tpl.get("title")))
    check(tpl.get("badge") == "🤖" and tpl["banner"]["stats"] == "2 ИЗМЕНЕНИЙ",
          "draft: эмодзи продукта и статистика", str(tpl["banner"]))
    first = announce_mod.draft(repo, None, ORG)
    check(first.get("inside") == ["фича один", "фича два"],
          "draft: без тега — первая секция", str(first.get("inside")))
    missing = announce_mod.draft(tmp / "norepo", "1.0", ORG)
    check(missing.get("ok") is False, "draft: нет CHANGELOG -> ошибка", str(missing))


def test_update(tmp: Path) -> None:
    print("=== announce: правка поста ===")
    _gate(True)
    try:
        client = FakeClient()
        res = asyncio.run(announce_mod.update(client, TPL, -1002000001, 42, org=ORG))
        edit = client.sent[0] if client.sent else {}
        check(res.get("ok") is True and edit.get("msg_id") == 42
              and "1967 ДЖЕЙЛОВ" in edit.get("text", "") and edit.get("edit"),
              "update: edit_message с новым текстом и баннером", str(edit)[:90])
    finally:
        _gate(False)


def test_gate(tmp: Path) -> None:
    print("=== announce: гейт TG_ALLOW_MANAGE ===")
    _gate(False)
    try:
        client = FakeClient()
        import asyncio
        res = asyncio.run(announce_mod.post(client, TPL, -1002000001, org=ORG))
        check(res.get("ok") is False and "TG_ALLOW_MANAGE" in res.get("error", "")
              and client.sent == [],
              "gate off: post заблокирован без отправок", str(res))
    finally:
        _gate(False)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    tmp = Path(tempfile.mkdtemp(prefix="tg_announce_"))
    try:
        test_render(tmp)
        test_compose(tmp)
        test_post_dry(tmp)
        test_post_send(tmp)
        test_readme(tmp)
        test_draft(tmp)
        test_update(tmp)
        test_gate(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
