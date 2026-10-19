#!/usr/bin/env python3
"""Fuzzy-дедуп сущностей: слияние открытых дублей по нормализованному title.

    python fuzzy_dedup.py                      # слить в пределах pack+kind+norm-title
    python fuzzy_dedup.py --dry-run            # только показать кластеры
    python fuzzy_dedup.py --pack services --max-merges 100

Точный дедуп по dedup_key живёт в extract/миграции; этот проход ловит дрейф
формата, когда один сервис записан под разными ключами («OpenAI Ключи» и
«openai  ключи!»). Keeper — более наполненная сущность, при равенстве —
старейшая; слияние через db.merge_entities (reason='fuzzy-title'), поэтому
события, источники и интеграции loser'а переносятся, а старый id остаётся
в entity_aliases. Закрытые сущности и разные kind не смешиваются.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB  # noqa: E402
from packs import PACKS  # noqa: E402

_RICH_FIELDS = ("title", "body", "owner", "due_ts", "project", "attrs_json")


def norm_title(title: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zа-яё]+", " ",
                                      (title or "").casefold())).strip()


def richness(row: dict) -> int:
    return sum(1 for f in _RICH_FIELDS if row.get(f) not in (None, ""))


def clusters(db: DB, pack: str | None) -> list[list[dict]]:
    sql = ("SELECT id, pack, kind, title, body, owner, due_ts, project, attrs_json, "
           "created_ts, updated_ts FROM entities WHERE closed_ts IS NULL")
    args: tuple = ()
    if pack:
        sql += " AND pack=?"
        args = (pack,)
    groups: dict[tuple, list[dict]] = {}
    for r in db.conn.execute(sql + " ORDER BY id", args):
        nt = norm_title(r["title"])
        if nt:
            gkey = (r["pack"], r["kind"], nt)
            if (PACKS.get(r["pack"]) or {}).get("dedup_scope") == "project":
                gkey = gkey + (r["project"] or "",)
            groups.setdefault(gkey, []).append(dict(r))
    return [g for g in groups.values() if len(g) > 1]


def run(db_path: str, pack: str | None = None, max_merges: int = 500,
        dry_run: bool = False) -> dict:
    now = int(time.time())
    merged = 0
    detail: list[dict] = []
    with DB(db_path) as db:
        for group in clusters(db, pack):
            if merged >= max_merges:
                break
            group.sort(key=lambda r: (-richness(r), r["created_ts"] or 0, r["id"]))
            keeper, losers = group[0], [r["id"] for r in group[1:]]
            detail.append({"keeper": keeper["id"], "title": keeper["title"],
                           "losers": losers})
            if not dry_run:
                db.merge_entities(keeper["id"], losers, "fuzzy-title", now)
                db.conn.commit()
                merged += len(losers)
    return {"clusters": len(detail), "merged": merged, "dry_run": dry_run,
            "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=os.environ.get("TG_DB",
                                                   str(HERE / "tg_index.sqlite")))
    ap.add_argument("--pack", help="только этот пакет (по умолчанию все)")
    ap.add_argument("--max-merges", type=int, default=500,
                    help="предел слияний за прогон (дефолт 500)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    rep = run(a.db, pack=a.pack, max_merges=a.max_merges, dry_run=a.dry_run)
    suffix = " (dry-run)" if rep["dry_run"] else ""
    print(f"кластеров: {rep['clusters']}, слито сущностей: {rep['merged']}{suffix}")
    for c in rep["detail"][:20]:
        print(f"  keeper {c['keeper']} {c['title']!r} <- {c['losers']}")


if __name__ == "__main__":
    main()
