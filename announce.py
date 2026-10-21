#!/usr/bin/env python3
"""Релиз-посты и readme тем: баннер, шаблон, отправка в форум/канал.

    python announce.py render --tpl post.yaml --out banner.png
    python announce.py post --tpl post.yaml --chat @whitebite_devsoft --topic 323
    python announce.py post --tpl post.yaml --chat @chan --send --also @news --react 🔥 --pin
    python announce.py readme --topic-title "🪡 Stitch · ядро" --chat @forum --topic 325 --send
    python announce.py readme --product tg-agent --chat @forum --send
    python announce.py draft --repo ../Stitch-Manager --tag 0.10.0 --product stitch --tpl-out post.yaml
    python announce.py update --tpl post.yaml --chat @chan --msg 42 --send

Шаблон post.yaml: badge, title, what, inside (список), ps, warning, links
(словарь имя->url), banner (title, stats, badges, handle), kind.
Ссылочная строка: --links <product> добавляет репо продукта из org.yaml,
плюс org links. Без --send всё печатается и не отправляется; реальная
отправка только при TG_ALLOW_MANAGE=1.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

import sync as sync_mod  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

ALLOWED = ("1", "true", "yes")


def _allowed() -> bool:
    return os.environ.get("TG_ALLOW_MANAGE", "").strip().lower() in ALLOWED


def _blocked() -> dict:
    return {"ok": False, "error": "announce заблокирован: нужен TG_ALLOW_MANAGE=1"}


def load_org(path: Optional[str] = None) -> dict:
    import yaml
    p = Path(path) if path else HERE / "org.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _load_tpl(path: str) -> dict:
    import yaml
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _wrap(d: Any, text: str, font: Any, max_w: int) -> list:
    lines, cur = [], ""
    for w in text.split():
        probe = (cur + " " + w).strip()
        if not cur or d.textlength(probe, font=font) <= max_w:
            cur = probe
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_banner(spec: dict, out: Path) -> Path:
    import random
    from PIL import Image, ImageDraw, ImageFont

    def font(size: int) -> Any:
        for p in ("C:/Windows/Fonts/consola.ttf",
                  "C:/Windows/Fonts/CascadiaMono.ttf",
                  "C:/Windows/Fonts/lucon.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
            if Path(p).is_file():
                return ImageFont.truetype(p, size)
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    W, H = 1080, 720
    img = Image.new("RGB", (W, H), (10, 10, 12))
    d = ImageDraw.Draw(img)
    title = spec.get("title") or ""
    rng = random.Random(title)
    for _ in range(400):
        x = rng.randrange(W // 2, W - 4)
        y = rng.randrange(0, H - 4)
        shade = rng.choice((255, 200, 150, 100))
        d.rectangle([x, y, x + 4, y + 4], fill=(shade, shade, shade))
    d.text((48, 36), "// WB RELEASE", font=font(26), fill=(120, 120, 120))
    f_title = font(72)
    lines = _wrap(d, title, f_title, 620)
    if len(lines) > 2:
        f_title = font(48)
        lines = _wrap(d, title, f_title, 620)[:2]
    y = 96
    for ln in lines:
        d.text((48, y), ln, font=f_title, fill=(255, 255, 255))
        y += 84
    if spec.get("stats"):
        d.text((48, y + 8), spec["stats"], font=font(34), fill=(170, 170, 170))
        y += 60
    x, y = 48, y + 24
    f_badge = font(28)
    for b in spec.get("badges") or []:
        w = d.textlength(b, font=f_badge) + 28
        d.rectangle([x, y, x + w, y + 44], outline=(200, 200, 200))
        d.text((x + 14, y + 8), b, font=f_badge, fill=(230, 230, 230))
        x += w + 12
    if spec.get("handle"):
        d.text((48, H - 72), spec["handle"], font=font(30), fill=(140, 140, 140))
    out = Path(out)
    img.save(out, format="PNG")
    return out


def compose_text(tpl: dict, org: dict, links_product: Optional[str] = None) -> str:
    text = (f"{tpl.get('badge', '📦')} {tpl.get('title', '')}\n\n"
            f"⚪ ЧТО ЭТО\n{tpl.get('what', '')}\n\n"
            f"⚪ ЧТО ВНУТРИ\n" + "\n".join("• " + x for x in tpl.get("inside") or []))
    if tpl.get("ps"):
        text += f"\n\nPs: {tpl['ps']}"
    if tpl.get("warning"):
        text += f"\n\n{tpl['warning']}"
    links = []
    if links_product:
        prod = (org.get("products") or {}).get(links_product)
        if prod:
            links.append(f"[{prod.get('title')}]({prod.get('repo')})")
    extra = tpl.get("links") or org.get("links") or {}
    pairs = list(extra.items()) if isinstance(extra, dict) else [(n, u) for n, u in extra]
    links.extend(f"[{n}]({u})" for n, u in pairs)
    if links:
        text += "\n\n" + " | ".join(links)
    return text


async def post(client: Any, tpl: dict, chat: Any, *, topic_id: Optional[int] = None,
               also: tuple = (), react: Optional[str] = None, pin: bool = False,
               links_product: Optional[str] = None, db_path: Optional[str] = None,
               org: Optional[dict] = None) -> dict:
    if not _allowed():
        return _blocked()
    org = org if org is not None else load_org()
    tmp = Path(tempfile.mkdtemp(prefix="announce_"))
    banner = render_banner(tpl.get("banner") or {}, tmp / "banner.png")
    text = compose_text(tpl, org, links_product)
    reply_to = topic_id if topic_id not in (None, 1) else None
    msg = await client.send_message(chat, text, file=str(banner),
                                    parse_mode="md", reply_to=reply_to)
    for a in also:
        await client.send_message(a, text, file=str(banner), parse_mode="md")
    if react:
        from telethon.tl import functions, types
        await client(functions.messages.SendReactionRequest(
            peer=chat, msg_id=msg.id, reaction=[types.ReactionEmoji(emoticon=react)]))
    if pin:
        from telethon.tl import functions
        await client(functions.messages.UpdatePinnedMessageRequest(peer=chat, id=msg.id))
    chat_id = chat if isinstance(chat, int) else sync_mod.marked_id(await client.get_entity(chat))
    if db_path:
        import db as db_mod
        tpl_hash = hashlib.sha256(
            json.dumps(tpl, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with db_mod.DB(db_path) as db:
            db.post_add(chat_id, msg.id, topic_id=topic_id,
                        kind=tpl.get("kind", "release"),
                        product=links_product, tpl_hash=tpl_hash)
    return {"ok": True, "msg_id": msg.id, "chat_id": chat_id, "banner": str(banner)}


async def post_file(tpl_path: str, chat: Any, *, topic_id: Optional[int] = None,
                    also: tuple = (), react: Optional[str] = None, pin: bool = False,
                    links_product: Optional[str] = None, db_path: Optional[str] = None,
                    org: Optional[dict] = None, send: bool = False) -> dict:
    tpl = _load_tpl(tpl_path)
    org = org if org is not None else load_org()
    if not send:
        banner = render_banner(tpl.get("banner") or {}, Path(tpl_path).with_suffix(".png"))
        print(compose_text(tpl, org, links_product))
        return {"ok": True, "dry_run": True, "banner": str(banner)}
    if not _allowed():
        return _blocked()
    client = sync_mod.make_client()
    await client.start(**sync_mod.start_kwargs())
    try:
        return await post(client, tpl, chat, topic_id=topic_id, also=also, react=react,
                          pin=pin, links_product=links_product, db_path=db_path, org=org)
    finally:
        await client.disconnect()


async def readme(client: Any, ref: str, org: dict, chat: Any, *,
                 topic_id: Optional[int] = None, send: bool = False,
                 db_path: Optional[str] = None) -> dict:
    tpl = readme_tpl(ref, org)
    if tpl is None:
        return {"ok": False, "error": f"нет в org.yaml продукта или темы: {ref!r}"}
    if not send:
        return {"ok": True, "dry_run": True, "text": compose_text(tpl, org)}
    return await post(client, tpl, chat, topic_id=topic_id, pin=True,
                      db_path=db_path, org=org)


def readme_tpl(ref: str, org: dict) -> Optional[dict]:
    products = org.get("products") or {}
    topic_products = org.get("topic_products") or {}
    if ref in products:
        prod = products[ref]
        title, what = prod["title"], prod.get("about", "")
        inside = [f"{prod.get('emoji', '📦')} {prod['title']} — {what}"]
        links = {prod["title"]: prod["repo"]}
    elif ref in topic_products:
        title, what = ref, "Что обсуждаем в этой теме"
        keys = [k for k in topic_products[ref] if k in products]
        inside = [f"{products[k].get('emoji', '📦')} {products[k]['title']} — "
                  f"{products[k].get('about', '')}" for k in keys]
        links = {products[k]["title"]: products[k]["repo"] for k in keys}
    else:
        return None
    return {"badge": "📌", "title": title, "what": what, "inside": inside,
            "links": links, "kind": "readme",
            "banner": {"title": title, "stats": "WHITEBITE · README ТЕМЫ",
                       "handle": org.get("forum")}}


def draft(repo_path: Path, tag: Optional[str], org: dict,
          product_key: Optional[str] = None) -> dict:
    changelog = Path(repo_path) / "CHANGELOG.md"
    if not changelog.is_file():
        return {"ok": False, "error": f"нет CHANGELOG.md в {repo_path}"}
    sections: list = []
    cur = None
    for ln in changelog.read_text(encoding="utf-8").splitlines():
        if ln.startswith("## "):
            cur = (ln[3:].strip(), [])
            sections.append(cur)
        elif cur is not None:
            cur[1].append(ln)
    if not sections:
        return {"ok": False, "error": "в CHANGELOG нет секций ##"}
    pick = next((s for s in sections if tag and tag in s[0]), None) or sections[0]
    bullets = []
    for ln in pick[1]:
        s = ln.strip().lstrip("-*").strip()
        if s:
            bullets.append(s)
    prod = (org.get("products") or {}).get(product_key or "")
    label = tag or pick[0]
    return {"badge": (prod or {}).get("emoji", "📦"),
            "title": f"{prod['title']} — {label}" if prod else label,
            "what": (prod or {}).get("about", ""), "inside": bullets,
            "kind": "release",
            "banner": {"title": label, "stats": f"{len(bullets)} ИЗМЕНЕНИЙ",
                       "badges": [], "handle": (prod or {}).get("repo")}}


async def update(client: Any, tpl: dict, chat: Any, msg_id: int, *,
                 links_product: Optional[str] = None,
                 org: Optional[dict] = None) -> dict:
    if not _allowed():
        return _blocked()
    org = org if org is not None else load_org()
    tmp = Path(tempfile.mkdtemp(prefix="announce_"))
    banner = render_banner(tpl.get("banner") or {}, tmp / "banner.png")
    await client.edit_message(chat, msg_id, compose_text(tpl, org, links_product),
                              file=str(banner), parse_mode="md")
    return {"ok": True, "msg_id": msg_id}


async def _amain(args: argparse.Namespace) -> int:
    org = load_org()
    if args.cmd == "render":
        res = {"ok": True, "path": str(render_banner(_load_tpl(args.tpl).get("banner") or {},
                                                     Path(args.out)))}
    elif args.cmd == "draft":
        tpl = draft(Path(args.repo), args.tag, org, product_key=args.product)
        if "error" in tpl:
            res = tpl
        else:
            if args.tpl_out:
                import yaml
                Path(args.tpl_out).write_text(
                    yaml.safe_dump(tpl, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
            res = {"ok": True, "tpl": tpl}
    elif args.cmd == "post":
        res = await post_file(args.tpl, args.chat, topic_id=args.topic,
                              also=tuple(args.also or ()), react=args.react,
                              pin=args.pin, links_product=args.links,
                              db_path=args.db, org=org, send=args.send)
        if res.get("dry_run"):
            print(res.get("banner", ""))
    elif args.cmd == "readme":
        if not args.send:
            res = await readme(None, args.product or args.topic_title, org,
                               args.chat, topic_id=args.topic, send=False)
            print(res.get("text", res.get("error", "")))
        elif not _allowed():
            res = _blocked()
        else:
            client = sync_mod.make_client()
            await client.start(**sync_mod.start_kwargs())
            try:
                res = await readme(client, args.product or args.topic_title, org,
                                   args.chat, topic_id=args.topic, send=True,
                                   db_path=args.db)
            finally:
                await client.disconnect()
    else:
        tpl = _load_tpl(args.tpl)
        if not args.send:
            print(compose_text(tpl, org, args.links))
            res = {"ok": True, "dry_run": True}
        elif not _allowed():
            res = _blocked()
        else:
            client = sync_mod.make_client()
            await client.start(**sync_mod.start_kwargs())
            try:
                res = await update(client, tpl, args.chat, args.msg,
                                   links_product=args.links, org=org)
            finally:
                await client.disconnect()
    print(res)
    return 0 if res.get("ok") else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Релиз-посты и readme тем")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="рендер баннера без отправки")
    r.add_argument("--tpl", required=True)
    r.add_argument("--out", required=True)

    p = sub.add_parser("post", help="пост по шаблону")
    p.add_argument("--tpl", required=True)
    p.add_argument("--chat", required=True)
    p.add_argument("--topic", type=int)
    p.add_argument("--also", action="append", help="доп. чат для кросспоста")
    p.add_argument("--react")
    p.add_argument("--pin", action="store_true")
    p.add_argument("--links", help="product из org.yaml для ссылочной строки")
    p.add_argument("--send", action="store_true")
    p.add_argument("--db", default=DB_PATH)

    rd = sub.add_parser("readme", help="readme-пост темы или продукта из org.yaml")
    rd.add_argument("--product")
    rd.add_argument("--topic-title")
    rd.add_argument("--chat", required=True)
    rd.add_argument("--topic", type=int)
    rd.add_argument("--send", action="store_true")
    rd.add_argument("--db", default=DB_PATH)

    d = sub.add_parser("draft", help="черновик из CHANGELOG репозитория")
    d.add_argument("--repo", required=True)
    d.add_argument("--tag")
    d.add_argument("--product")
    d.add_argument("--tpl-out")

    u = sub.add_parser("update", help="правка отправленного поста")
    u.add_argument("--tpl", required=True)
    u.add_argument("--chat", required=True)
    u.add_argument("--msg", type=int, required=True)
    u.add_argument("--links")
    u.add_argument("--send", action="store_true")

    args = ap.parse_args()
    if args.cmd == "readme" and not (args.product or args.topic_title):
        ap.error("readme: нужен --product или --topic-title")
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
