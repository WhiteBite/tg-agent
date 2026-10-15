#!/usr/bin/env python3
"""Мониторинг: просрочки и сводка по проектам из таблицы entities.

    python monitor.py                       # dry-run: сводка в консоль
    python monitor.py --days-ahead 7        # добавить сроки на неделю вперёд
    python monitor.py --project стройка     # один проект
    python monitor.py --db путь --send      # отправить сводку в Saved Messages

Отправка — только через notify.py (гейт TG_ALLOW_SEND=1); сам модуль Telethon
не импортирует. Просрочка = открытая сущность (closed_ts IS NULL) с due_ts в
прошлом; days_ahead задвигает границу вперёд для раздела «к сроку».
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import notify as notify_mod

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB  # noqa: E402

DB_PATH = os.environ.get("TG_DB", str(HERE / "tg_index.sqlite"))

NO_PROJECT = "без проекта"
LINE_MAX = 300


def _when(ts: int, now: int) -> str:
    delta = ts - now
    if delta < 0:
        return f"просрочено на {max((-delta) // 86400, 1)} дн."
    if delta < 86400:
        return "срок сегодня"
    return f"через {delta // 86400} дн."


def collect(db: str | Path, days_ahead: int = 0, project: str | None = None,
            news_hours: int = 24) -> dict:
    now = int(time.time())
    with DB(db) as d:
        overdue = d.entity_overdue(project=project, days_ahead=0, limit=500)
        window = d.entity_overdue(project=project, days_ahead=days_ahead, limit=500)
        open_rows = [dict(r) for r in d.conn.execute(
            "SELECT * FROM entities WHERE closed_ts IS NULL ORDER BY due_ts")]
        since = now - news_hours * 3600
        news = [dict(r) for r in d.conn.execute(
            "SELECT e.* FROM entities e WHERE e.created_ts >= ? OR EXISTS("
            "SELECT 1 FROM entity_events ev WHERE ev.entity_id = e.id AND ev.ts >= ? "
            "AND (ev.op IN ('new', 'merge') OR ev.field = 'dedup')) "
            "ORDER BY e.created_ts DESC LIMIT 200", (since, since))]
        updated = d.conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE updated_ts >= ? AND created_ts < ?",
            (now - news_hours * 3600, now - news_hours * 3600)).fetchone()["c"]
    overdue_ids = {e["id"] for e in overdue}
    upcoming = [e for e in window if e["id"] not in overdue_ids]
    projects: dict[str, dict] = {}
    for e in open_rows:
        name = e["project"] or NO_PROJECT
        if project is not None and name != project:
            continue
        p = projects.setdefault(name, {"open": 0, "by_kind": {}, "overdue": [], "next_due": None})
        p["open"] += 1
        p["by_kind"][e["kind"]] = p["by_kind"].get(e["kind"], 0) + 1
        if e["id"] in overdue_ids:
            p["overdue"].append(e)
        if e["due_ts"] and e["due_ts"] > now and (p["next_due"] is None or e["due_ts"] < p["next_due"]):
            p["next_due"] = e["due_ts"]
    return {"now_ts": now, "days_ahead": days_ahead, "projects": projects,
            "overdue": overdue, "upcoming": upcoming, "news": news,
            "news_hours": news_hours, "news_updated": updated}


def format_report(summary: dict) -> list[str]:
    now = summary["now_ts"]
    date = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    header = f"Мониторинг {date}: проектов {len(summary['projects'])}, просрочено {len(summary['overdue'])}"
    if summary["days_ahead"]:
        header += f", к сроку в ближайшие {summary['days_ahead']} дн.: {len(summary['upcoming'])}"
    if summary.get("news"):
        header += (f", нового за {summary['news_hours']}ч: {len(summary['news'])}"
                   + (f" (+{summary['news_updated']} обновлений)" if summary.get("news_updated") else ""))
    lines = [header]
    for e in summary["news"][:10]:
        age = max((now - e["created_ts"]) // 3600, 1)
        lines.append(f"  новое: [{e['pack']}/{e['kind']}] {e['title'][:120]} ({age}ч назад)")
    for name, p in sorted(summary["projects"].items()):
        parts = [f"{name}: открытых {p['open']}"]
        kinds = ", ".join(f"{k} {c}" for k, c in sorted(p["by_kind"].items()))
        if kinds:
            parts.append(kinds)
        if p["overdue"]:
            titles = "; ".join(f"{e['title']} ({_when(e['due_ts'], now)})" for e in p["overdue"])
            parts.append(f"просрочено: {titles}")
        if p["next_due"]:
            parts.append(f"ближайший срок {_when(p['next_due'], now)}")
        lines.append(" | ".join(parts)[:LINE_MAX])
    return lines


def run(db_path: str | Path = DB_PATH, days_ahead: int = 0, project: str | None = None,
        send: bool = False, dry_run: bool = True, news_hours: int = 24) -> dict:
    summary = collect(db_path, days_ahead=days_ahead, project=project,
                      news_hours=news_hours)
    lines = format_report(summary)
    result = notify_mod.notify(lines, dry_run=dry_run) if send else None
    return {"projects": summary["projects"], "overdue": summary["overdue"],
            "upcoming": summary["upcoming"], "news": summary["news"],
            "lines": lines, "notify": result}


def _arg_value(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main() -> None:
    args = sys.argv[1:]
    send = "--send" in args and "--dry-run" not in args
    res = run(_arg_value(args, "--db") or DB_PATH,
              days_ahead=int(_arg_value(args, "--days-ahead") or 0),
              project=_arg_value(args, "--project"), send=send, dry_run=not send,
              news_hours=int(_arg_value(args, "--news-hours") or 24))
    for line in res["lines"]:
        print(line)
    if send:
        print(res["notify"])


if __name__ == "__main__":
    main()
