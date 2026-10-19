#!/usr/bin/env python3
"""Мониторинг: просрочки и сводка по проектам из таблицы entities.

    python monitor.py                       # dry-run: сводка в консоль
    python monitor.py --days-ahead 7        # добавить сроки на неделю вперёд
    python monitor.py --project стройка     # один проект
    python monitor.py --db путь --send      # отправить сводку в Saved Messages

Отправка — только через notify.py (гейт TG_ALLOW_SEND=1); сам модуль Telethon
не импортирует. Просрочка = открытая сущность (closed_ts IS NULL) с due_ts в
прошлом; days_ahead задвигает границу вперёд для раздела «к сроку».
Неизменившаяся сводка повторно не отправляется (hash в meta);
TG_MONITOR_FORCE=1 обходит проверку.
"""

from __future__ import annotations

import hashlib
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
OPEN_SCAN_LIMIT = 2000
LAST_HASH_KEY = "monitor:last_hash"


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
    q_days = int(os.environ.get("MONITOR_QUESTION_DAYS") or 3)
    with DB(db) as d:
        overdue = d.entity_overdue(project=project, days_ahead=0, limit=500)
        window = d.entity_overdue(project=project, days_ahead=days_ahead, limit=500)
        open_rows = [dict(r) for r in d.conn.execute(
            "SELECT * FROM entities WHERE closed_ts IS NULL "
            "ORDER BY due_ts LIMIT ?", (OPEN_SCAN_LIMIT,))]
        q_sql = ("SELECT * FROM entities WHERE kind='question' AND closed_ts IS NULL "
                 "AND created_ts < ?")
        q_args: list = [now - q_days * 86400]
        if project is not None:
            q_sql += " AND project = ?"
            q_args.append(project)
        q_sql += " ORDER BY created_ts LIMIT 10"
        questions = [dict(r) for r in d.conn.execute(q_sql, q_args)]
        since = now - news_hours * 3600
        news = [dict(r) for r in d.conn.execute(
            "SELECT e.* FROM entities e WHERE e.created_ts >= ? OR EXISTS("
            "SELECT 1 FROM entity_events ev WHERE ev.entity_id = e.id AND ev.ts >= ? "
            "AND (ev.op IN ('new', 'merge') OR ev.field = 'dedup')) "
            "ORDER BY e.created_ts DESC LIMIT 200", (since, since))]
        updated = d.conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE updated_ts >= ? AND created_ts < ?",
            (now - news_hours * 3600, now - news_hours * 3600)).fetchone()["c"]
        rej = d.conn.execute(
            "SELECT COALESCE(SUM(ops_rejected),0) r, COALESCE(SUM(ops_overflow),0) o "
            "FROM extraction_runs WHERE ts >= ?", (since,)).fetchone()
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
            "news_hours": news_hours, "news_updated": updated,
            "questions": questions, "question_days": q_days,
            "ops_rejected": rej["r"], "ops_overflow": rej["o"]}


def format_report(summary: dict) -> list[str]:
    now = summary["now_ts"]
    date = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    header = f"Мониторинг {date}: проектов {len(summary['projects'])}, просрочено {len(summary['overdue'])}"
    if summary["days_ahead"]:
        header += f", к сроку в ближайшие {summary['days_ahead']} дн.: {len(summary['upcoming'])}"
    if summary.get("news"):
        header += (f", нового за {summary['news_hours']}ч: {len(summary['news'])}"
                   + (f" (+{summary['news_updated']} обновлений)" if summary.get("news_updated") else ""))
    if summary.get("questions"):
        header += (f", открытых вопросов >{summary['question_days']} дн.: "
                   f"{len(summary['questions'])}")
    lines = [header]
    for e in summary["news"][:10]:
        age = max((now - e["created_ts"]) // 3600, 1)
        lines.append(f"  новое: [{e['pack']}/{e['kind']}] {e['title'][:120]} ({age}ч назад)")
    for e in summary.get("questions") or []:
        age = max((now - e["created_ts"]) // 86400, 1)
        lines.append(f"  вопрос: [{e['pack']}] {e['title'][:120]} ({age} дн. открыт)")
    if summary.get("ops_rejected"):
        tail = (f", сверх лимита {summary['ops_overflow']}"
                if summary.get("ops_overflow") else "")
        lines.append(f"  извлечение: отклонено операций {summary['ops_rejected']}"
                     f" за {summary['news_hours']}ч{tail}")
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
    result = None
    if send:
        if dry_run:
            result = notify_mod.notify(lines, dry_run=True)
        else:
            force = os.environ.get("TG_MONITOR_FORCE", "").strip().lower() in notify_mod.ALLOWED
            report_hash = hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()
            with DB(db_path) as d:
                prev = d.get_meta(LAST_HASH_KEY)
            if prev == report_hash and not force:
                result = {"ok": True, "skipped": True, "reason": "сводка не изменилась"}
            else:
                result = notify_mod.notify(lines, dry_run=False)
                if result.get("ok"):
                    with DB(db_path) as d:
                        d.set_meta(LAST_HASH_KEY, report_hash)
    return {"projects": summary["projects"], "overdue": summary["overdue"],
            "upcoming": summary["upcoming"], "news": summary["news"],
            "questions": summary["questions"], "lines": lines, "notify": result}


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
