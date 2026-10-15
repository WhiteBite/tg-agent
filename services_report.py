#!/usr/bin/env python3
"""Человекочитаемый каталог сервисов из сущностей пака.

    python services_report.py                       # markdown в stdout
    python services_report.py --days 90 --out catalog_90d.md
    python services_report.py --pack services --query vpn

Сущности остаются источником; отчёт — проекция: группировка по kind,
источник (чат/тема/сообщение) и первая evidence-цитата по каждой.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

KIND_ORDER = ["service", "trial", "promo"]
KIND_TITLES = {"service": "Сервисы", "trial": "Триалы", "promo": "Промо и скидки"}


def _date(ts: int | None) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def collect(db_path: str | Path = DB_PATH, pack: str = "services",
            days: int | None = None, query: str | None = None) -> dict:
    since = int(time.time()) - days * 86400 if days else 0
    sql = (
        "SELECT e.*, c.title chat_title, t.title topic_title, w.alias "
        "FROM entities e "
        "LEFT JOIN chats c ON c.id = e.chat_id "
        "LEFT JOIN topics t ON t.chat_id = e.chat_id AND t.topic_id = e.topic_id "
        "LEFT JOIN watch w ON w.chat_id = e.chat_id AND w.topic_id = e.topic_id "
        "WHERE e.pack = ?")
    args: list = [pack]
    if since:
        sql += " AND COALESCE(e.updated_ts, e.created_ts, 0) >= ?"
        args.append(since)
    if query:
        sql += " AND (e.title LIKE ? OR e.body LIKE ? OR e.dedup_key LIKE ?)"
        like = f"%{query}%"
        args += [like, like, like]
    sql += " ORDER BY e.kind, COALESCE(e.updated_ts, e.created_ts) DESC"
    with DB(db_path) as db:
        rows = [dict(r) for r in db.conn.execute(sql, args)]
        ev = {r["entity_id"]: (r["chat_id"], r["msg_id"], r["quote"])
              for r in db.conn.execute(
                  "SELECT entity_id, chat_id, msg_id, quote FROM entity_events "
                  "WHERE op='new' AND quote IS NOT NULL AND quote != '' "
                  "ORDER BY id")}
    for r in rows:
        r["source"] = (r["alias"] or r["topic_title"] or r["chat_title"]
                       or str(r["chat_id"]))
        r["evidence"] = ev.get(r["id"])
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    return {"pack": pack, "days": days, "query": query,
            "total": len(rows), "by_kind": by_kind,
            "generated_ts": int(time.time())}


def format_markdown(summary: dict) -> list[str]:
    lines = [f"# Каталог: {summary['pack']}", ""]
    window = f" за {summary['days']}д" if summary.get("days") else ""
    q = f" по запросу «{summary['query']}»" if summary.get("query") else ""
    lines.append(f"Сущностей: {summary['total']}{window}{q} "
                 f"(сгенерировано {_date(summary['generated_ts'])})")
    kinds = KIND_ORDER + [k for k in summary["by_kind"] if k not in KIND_ORDER]
    for kind in kinds:
        rows = summary["by_kind"].get(kind)
        if not rows:
            continue
        lines += ["", f"## {KIND_TITLES.get(kind, kind)} ({len(rows)})", ""]
        for r in rows:
            status = "" if r["status"] == "open" else f" [{r['status']}]"
            body = f" — {r['body']}" if r.get("body") else ""
            lines.append(f"- **{r['title']}**{status}{body}")
            meta = f"  источник: {r['source']}, {_date(r.get('updated_ts') or r.get('created_ts'))}"
            if r.get("dedup_key"):
                meta += f", ключ: {r['dedup_key']}"
            lines.append(meta)
            if r.get("evidence"):
                chat_id, msg_id, quote = r["evidence"]
                lines.append(f"  цитата (msg {msg_id}): «{quote}»")
    return lines


def run(db_path: str | Path = DB_PATH, pack: str = "services",
        days: int | None = None, query: str | None = None,
        out: str | None = None) -> dict:
    summary = collect(db_path, pack=pack, days=days, query=query)
    text = "\n".join(format_markdown(summary)) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return summary


def _arg_value(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def main() -> None:
    args = sys.argv[1:]
    days = _arg_value(args, "--days")
    run(_arg_value(args, "--db") or DB_PATH,
        pack=_arg_value(args, "--pack") or "services",
        days=int(days) if days else None,
        query=_arg_value(args, "--query"),
        out=_arg_value(args, "--out"))


if __name__ == "__main__":
    main()
