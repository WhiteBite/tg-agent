#!/usr/bin/env python3
"""Правка состава папок Telegram: добавить/убрать чаты.

    python folders.py list
    python folders.py add --folder 15 --chat @durov --chat -1002922797592
    python folders.py remove --folder 15 --chat @durov

Второй после notify.py модуль, который пишет в Telegram — но только состав
папки (messages.updateDialogFilter); сообщений не отправляет. После правки
состав перечитывается и складывается в индекс (folder_chats), а сами чаты
попадают в него синком: python sync.py --folder 15.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

import sync as sync_mod  # noqa: E402
from db import DB  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))


async def _current_filters(client: Any) -> list[Any]:
    from telethon.tl import functions
    res = await client(functions.messages.GetDialogFiltersRequest())
    rows = getattr(res, "filters", None)
    if rows is None:
        rows = res or []
    return rows


async def folder_edit(
    client: Any,
    folder_id: int,
    chats: list[str],
    mode: str = "add",
    db_path: Optional[str] = None,
) -> dict:
    """add/remove чатов в папке по id или @username. Возвращает отчёт;
    при db_path обновляет и локальную копию состава (folder_chats)."""
    from telethon.tl import functions
    if mode not in ("add", "remove"):
        return {"ok": False, "error": f"неизвестный режим: {mode}"}
    rows = await _current_filters(client)
    target = next((f for f in rows if getattr(f, "id", None) == folder_id), None)
    if target is None:
        return {"ok": False, "error": f"папка {folder_id} не найдена"}
    resolved: list[tuple[int, Any]] = []
    for name in chats:
        try:
            ref = int(name)
        except ValueError:
            ref = name
        try:
            ent = await client.get_entity(ref)
        except Exception as exc:
            return {"ok": False, "error": f"чат {name} не найден: {exc}"}
        try:
            peer = client.get_input_entity(ref)
            if asyncio.iscoroutine(peer):
                peer = await peer
        except Exception:
            peer = ent
        resolved.append((sync_mod.marked_id(ent), peer))
    have = {sync_mod._peer_marked(p) for p in (target.include_peers or [])}
    have.discard(None)
    peers = list(target.include_peers or [])
    changed = 0
    for marked, peer in resolved:
        if mode == "add" and marked not in have:
            peers.append(peer)
            have.add(marked)
            changed += 1
        elif mode == "remove" and marked in have:
            peers = [p for p in peers if sync_mod._peer_marked(p) != marked]
            have.discard(marked)
            changed += 1
    if not changed:
        return {"ok": True, "changed": 0, "folder": folder_id,
                "note": "состав уже актуален"}
    target.include_peers = peers
    await client(functions.messages.UpdateDialogFilterRequest(
        id=folder_id, filter=target))
    refreshed: Optional[list[dict]] = None
    if db_path:
        try:
            refreshed = await sync_mod.fetch_folders(client)
            with DB(db_path) as db:
                db.save_folders(refreshed)
        except Exception:
            refreshed = None
    return {"ok": True, "changed": changed, "folder": folder_id,
            "peers": len(peers), "refreshed": refreshed is not None}


async def _amain(args: argparse.Namespace) -> int:
    client = sync_mod.make_client()
    await client.start(**sync_mod.start_kwargs())
    try:
        if args.cmd == "list":
            for f in await sync_mod.fetch_folders(client):
                print(f"{f['id']:3} | {(f['title'] or '')[:38]:38} | чатов {len(f['peers'])}")
            return 0
        res = await folder_edit(client, args.folder, args.chat, mode=args.cmd,
                                db_path=DB_PATH)
    finally:
        await client.disconnect()
    print(res)
    return 0 if res.get("ok") else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Правка состава папок Telegram")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="папки и их размер")
    for name in ("add", "remove"):
        p = sub.add_parser(name)
        p.add_argument("--folder", type=int, required=True, help="id папки")
        p.add_argument("--chat", action="append", required=True,
                       help="@username или id; можно несколько раз")
        p.add_argument("--db", default=DB_PATH)
    sys.exit(asyncio.run(_amain(ap.parse_args())))


if __name__ == "__main__":
    main()
