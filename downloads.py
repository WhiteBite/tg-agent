#!/usr/bin/env python3
"""CLI модуля скачивания медиа: план, прогон, статус, файлы, подборки, задания.

    python downloads.py plan --sources watch --days 90
    python downloads.py run --sources watch            # создать и выполнить
    python downloads.py run --job 3 --retry --force    # продолжить/дожать
    python downloads.py run --sources watch --mode forward --to saved
    python downloads.py status [--job 3]
    python downloads.py files [--chat -100123]
    python downloads.py forwards [--chat -100123]
    python downloads.py collections save Видео --chats -100111,-100222
    python downloads.py job pause|resume|cancel 3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()

from db import DB  # noqa: E402
import jobs  # noqa: E402
import sources  # noqa: E402
from modules.downloads import ROOT, run_async, status, files, forwards, plan_job  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Скачивание и пересылка медиа из индекса с дедупликацией (точные дубли — по id файла и sha256)")
    ap.add_argument("--db", default=os.environ.get("TG_DB", str(HERE / "tg_index.sqlite")))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="создать задание без запуска")
    p_plan.add_argument("--sources", default="watch",
                        help="watch | all | folder:<id> | collection:<имя> | chat:<id|@username>, через запятую")
    p_plan.add_argument("--kinds", default="video", help="video, videonote, photo")
    p_plan.add_argument("--days", type=int, default=0, help="окно в днях (0 — вся история)")
    p_plan.add_argument("--max-mb", type=int, default=None)
    p_plan.add_argument("--all-messages", action="store_true",
                        help="включить уже скачанные сообщения")
    p_plan.add_argument("--mode", choices=("download", "forward"),
                        default="download", help="download — на диск, forward — пересылка")
    p_plan.add_argument("--to", default="",
                        help="адресат пересылки: saved | @username | id (для mode=forward)")

    p_run = sub.add_parser("run", help="прогон: новое задание из --sources или продолжить --job")
    p_run.add_argument("--job", type=int, default=None)
    p_run.add_argument("--sources", default="watch")
    p_run.add_argument("--kinds", default="video")
    p_run.add_argument("--days", type=int, default=0)
    p_run.add_argument("--max-mb", type=int, default=None)
    p_run.add_argument("--all-messages", action="store_true")
    p_run.add_argument("--mode", choices=("download", "forward"), default="download")
    p_run.add_argument("--to", default="")
    p_run.add_argument("--force", action="store_true",
                       help="забрать зависшее running/failed задание")
    p_run.add_argument("--retry", action="store_true",
                       help="сбросить failed-пункты в planned перед прогоном")

    p_status = sub.add_parser("status", help="задания и библиотека файлов")
    p_status.add_argument("--job", type=int, default=0)

    p_files = sub.add_parser("files", help="скачанные файлы с числом источников")
    p_files.add_argument("--chat", type=int, default=None)
    p_files.add_argument("--limit", type=int, default=100)

    p_fwd = sub.add_parser("forwards", help="пересланные медиа с числом источников")
    p_fwd.add_argument("--chat", type=int, default=None)
    p_fwd.add_argument("--limit", type=int, default=100)

    p_col = sub.add_parser("collections", help="подборки каналов")
    col = p_col.add_subparsers(dest="col", required=True)
    col.add_parser("list")
    c_save = col.add_parser("save")
    c_save.add_argument("name")
    c_save.add_argument("--chats", required=True, help="chat_id через запятую")
    c_del = col.add_parser("delete")
    c_del.add_argument("name")

    p_job = sub.add_parser("job", help="pause | resume | cancel")
    job = p_job.add_subparsers(dest="op", required=True)
    for op in ("pause", "resume", "cancel"):
        job.add_parser(op).add_argument("job_id", type=int)

    args = ap.parse_args()

    if args.cmd == "plan":
        try:
            with DB(args.db) as db:
                res = plan_job(db, args.sources, kinds=args.kinds, days=args.days,
                               max_mb=args.max_mb,
                               include_downloaded=args.all_messages,
                               mode=args.mode, to=args.to)
        except ValueError as exc:
            print(f"ошибка: {exc}", file=sys.stderr)
            sys.exit(1)
        mode_note = f", mode={args.mode}, to={args.to}" if args.mode == "forward" else ""
        print(f"задание {res['job_id']}: {res['items']} сообщений из "
              f"{len(res['chats'])} чатов (kinds={args.kinds}, days={args.days}"
              f"{mode_note})")
        print(f"запуск: python downloads.py run --job {res['job_id']}")

    elif args.cmd == "run":
        try:
            rep = asyncio.run(run_async(
                args.db, args.job, sources_spec=args.sources, kinds=args.kinds,
                days=args.days, max_mb=args.max_mb,
                include_downloaded=args.all_messages, mode=args.mode, to=args.to,
                force=args.force, retry=args.retry))
        except (ValueError, RuntimeError) as exc:
            print(f"ошибка: {exc}", file=sys.stderr)
            sys.exit(1)
        counts = rep["counts"]
        word = "переслано" if rep.get("mode") == "forward" else "скачано"
        print(f"задание {rep['job_id']}: {word} {counts.get('done', 0)}, "
              f"пропущено {counts.get('skipped', 0)} {rep['skipped']}, "
              f"упало {counts.get('failed', 0)}")
        for f in rep["failed"]:
            print(f"  FAIL {f['chat_id']}/{f['msg_id']}: {f['detail']}")

    elif args.cmd == "status":
        with DB(args.db) as db:
            res = status(db, args.job)
        if "error" in res:
            print(res["error"], file=sys.stderr)
            sys.exit(1)
        if "job" in res:
            job = res["job"]
            print(f"задание {job['id']} [{job['state']}] "
                  f"{job['counts'].get('done', 0)}/{job['counts'].get('total', 0)}")
            print(f"  параметры: {job['params']}")
            print(f"  отчёт: {res['report']['counts']} пропуски={res['report']['skipped']}")
        else:
            for job in res["jobs"]:
                c = job["counts"]
                print(f"  #{job['id']} [{job['state']}] done={c.get('done', 0)} "
                      f"skip={c.get('skipped', 0)} fail={c.get('failed', 0)} "
                      f"из {c.get('total', 0)}  {job['params'].get('sources')}")
            f = res["files"]
            print(f"библиотека: {f['count']} файлов, {f['bytes']} байт; по kind: {f['by_kind']}")
            fw = res["forwards"]
            print(f"пересылки: {fw['count']} медиа; по адресатам: {fw['by_target']}")

    elif args.cmd == "files":
        with DB(args.db) as db:
            rows = files(db, chat_id=args.chat, limit=args.limit)
        for r in rows:
            print(f"{r['id']:>6} {r['kind'] or '?':<9} {r['size'] or 0:>12} "
                  f"src={r['sources']} {r['filename'] or r['sha256'][:16]}")
            print(f"        {ROOT / r['path']}")

    elif args.cmd == "forwards":
        with DB(args.db) as db:
            rows = forwards(db, chat_id=args.chat, limit=args.limit)
        for r in rows:
            print(f"{r['doc_id']:<12} {r['kind'] or '?':<9} "
                  f"->{r['target_chat_id']}/{r['target_msg_id']} "
                  f"src={r['sources']} {r['size'] or 0} байт")

    elif args.cmd == "collections":
        with DB(args.db) as db:
            if args.col == "list":
                for c in sources.collection_list(db):
                    chats = ", ".join(str(x["chat_id"]) for x in c["chats"])
                    print(f"{c['name']}: {chats or '—'}")
            elif args.col == "save":
                try:
                    ids = [int(x) for x in args.chats.split(",") if x.strip()]
                    res = sources.collection_save(db, args.name, ids)
                except ValueError as exc:
                    print(f"ошибка: {exc}", file=sys.stderr)
                    sys.exit(1)
                print(f"подборка {res['name']}: {len(res['chats'])} чатов")
            else:
                ok = sources.collection_delete(db, args.name)
                print("удалена" if ok else "не найдена")
                sys.exit(0 if ok else 1)

    elif args.cmd == "job":
        with DB(args.db) as db:
            fn = {"pause": jobs.pause, "resume": jobs.resume,
                  "cancel": jobs.cancel}[args.op]
            ok = fn(db, args.job_id)
        print(f"{args.op}: {'ок' if ok else 'не выполнено (состояние задания)'}")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
