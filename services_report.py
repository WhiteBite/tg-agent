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
        if since:
            first_ts = {r["entity_id"]: r["m"] for r in db.conn.execute(
                "SELECT ee.entity_id, MIN(m.ts) m FROM entity_events ee "
                "JOIN messages m ON m.chat_id = ee.chat_id AND m.msg_id = ee.msg_id "
                "GROUP BY ee.entity_id")}
            rows = [r for r in rows
                    if (first_ts.get(r["id"]) or r.get("created_ts") or 0) >= since]
        src_map: dict[int, list[str]] = {}
        for s in db.conn.execute(
                "SELECT es.entity_id, COALESCE(w.alias, t.title, c.title, "
                "CAST(es.chat_id AS TEXT)) src "
                "FROM entity_sources es "
                "LEFT JOIN chats c ON c.id = es.chat_id "
                "LEFT JOIN topics t ON t.chat_id = es.chat_id AND t.topic_id = es.topic_id "
                "LEFT JOIN watch w ON w.chat_id = es.chat_id AND w.topic_id = es.topic_id "
                "ORDER BY es.rowid"):
            lst = src_map.setdefault(s["entity_id"], [])
            if s["src"] not in lst:
                lst.append(s["src"])
    groups: dict[str, dict] = {}
    merged: list[dict] = []
    for r in rows:
        srcs = src_map.get(r["id"]) or [
            r["alias"] or r["topic_title"] or r["chat_title"] or str(r["chat_id"])]
        g = {"kind": r["kind"], "title": r["title"], "body": r.get("body"),
             "status": r["status"], "dedup_key": r.get("dedup_key"),
             "sources": list(srcs), "created_ts": r.get("created_ts"),
             "updated_ts": r.get("updated_ts"), "evidence": ev.get(r["id"]),
             "count": 1}
        key = r.get("dedup_key")
        if not key:
            merged.append(g)
            continue
        prev = groups.get(key)
        if prev is None:
            groups[key] = g
        else:
            prev["count"] += 1
            for s in srcs:
                if s not in prev["sources"]:
                    prev["sources"].append(s)
            for f, agg in (("created_ts", min), ("updated_ts", max)):
                vals = [v for v in (prev.get(f), g.get(f)) if v]
                if vals:
                    prev[f] = agg(vals)
            if prev["status"] == "done" and g["status"] != "done":
                prev["status"] = g["status"]
            if not prev.get("body") and g.get("body"):
                prev["body"] = g["body"]
            if not prev.get("evidence") and g.get("evidence"):
                prev["evidence"] = g["evidence"]
    all_groups = list(groups.values()) + merged
    by_kind: dict[str, list[dict]] = {}
    for g in all_groups:
        by_kind.setdefault(g["kind"], []).append(g)
    for kind in by_kind:
        by_kind[kind].sort(key=lambda g: (-g["count"],
                                          -(g.get("updated_ts") or 0)))
    return {"pack": pack, "days": days, "query": query,
            "total": len(rows), "groups": len(all_groups), "by_kind": by_kind,
            "generated_ts": int(time.time())}


def format_markdown(summary: dict) -> list[str]:
    lines = [f"# Каталог: {summary['pack']}", ""]
    window = f" за {summary['days']}д" if summary.get("days") else ""
    q = f" по запросу «{summary['query']}»" if summary.get("query") else ""
    limit = ""
    if summary.get("top"):
        limit += f", top-{summary['top']} в секции"
    if summary.get("hide_closed"):
        limit += ", без закрытых"
    lines.append(f"Сущностей: {summary['total']}, групп после склейки: "
                 f"{summary['groups']}{window}{q}{limit} "
                 f"(сгенерировано {_date(summary['generated_ts'])})")
    kinds = KIND_ORDER + [k for k in summary["by_kind"] if k not in KIND_ORDER]
    for kind in kinds:
        rows = summary["by_kind"].get(kind)
        if not rows:
            continue
        lines += ["", f"## {KIND_TITLES.get(kind, kind)} ({len(rows)})", ""]
        for g in rows:
            status = "" if g["status"] == "open" else f" [{g['status']}]"
            body = f" — {g['body']}" if g.get("body") else ""
            mult = f" ×{g['count']}" if g["count"] > 1 else ""
            lines.append(f"- **{g['title']}**{status}{mult}{body}")
            src = ", ".join(g["sources"][:5])
            if len(g["sources"]) > 5:
                src += f" …(+{len(g['sources']) - 5})"
            meta = (f"  источники: {src}, "
                    f"{_date(g.get('updated_ts') or g.get('created_ts'))}")
            if g.get("dedup_key"):
                meta += f", ключ: {g['dedup_key'][:12]}"
            lines.append(meta)
            if g.get("evidence"):
                _, msg_id, quote = g["evidence"]
                lines.append(f"  цитата (msg {msg_id}): «{quote}»")
    return lines


def run(db_path: str | Path = DB_PATH, pack: str = "services",
        days: int | None = None, query: str | None = None,
        out: str | None = None, top: int | None = None,
        hide_closed: bool = False) -> dict:
    summary = collect(db_path, pack=pack, days=days, query=query)
    if hide_closed:
        summary["by_kind"] = {k: [g for g in v if g["status"] != "done"]
                              for k, v in summary["by_kind"].items()}
    if top:
        summary["by_kind"] = {k: v[:top] for k, v in summary["by_kind"].items()}
    summary["groups"] = sum(len(v) for v in summary["by_kind"].values())
    summary["top"] = top
    summary["hide_closed"] = hide_closed
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
    top = _arg_value(args, "--top")
    run(_arg_value(args, "--db") or DB_PATH,
        pack=_arg_value(args, "--pack") or "services",
        days=int(days) if days else None,
        query=_arg_value(args, "--query"),
        out=_arg_value(args, "--out"),
        top=int(top) if top else None,
        hide_closed="--hide-closed" in args)


if __name__ == "__main__":
    main()
