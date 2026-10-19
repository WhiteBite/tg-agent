#!/usr/bin/env python3
"""Диагностика перед первым запуском: python3 doctor.py [--live]

Проверяет окружение офлайн (библиотеки, креды, сессия, БД, FTS5, self-test
маскировки). С --live дополнительно пробует подключиться к Telegram.
--providers: одна мини-компляция к настроенному extract/digest-провайдеру
(сеть, только по явному запросу; мёртвый ключ виден как HTTP 401).
--quality: заполненность полей сущностей по пакетам и гистограмма причин
отказов из extraction_runs.rejects_json.
Код возврата: 0 — можно работать (предупреждения допустимы), 1 — есть ошибки.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from env import load_env  # noqa: E402

load_env()


def _provider_check(prefix: str) -> dict:
    from llm_http import call_with_deadline, extra_body, key_list, post_json_keys

    label = f"{prefix.lower()}-провайдер (live)"
    provider = os.environ.get(f"{prefix}_PROVIDER", "stub")
    if provider != "openai":
        return {"name": label, "ok": None,
                "detail": f"provider={provider} — сетевая проверка неприменима"}
    url = os.environ.get(f"{prefix}_URL")
    if not url:
        return {"name": label, "ok": False, "detail": f"нет {prefix}_URL"}
    body = {
        "model": os.environ.get(f"{prefix}_MODEL", "gpt-4o-mini"),
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    body.update(extra_body(url))
    try:
        call_with_deadline(post_json_keys, 60,
                           url.rstrip("/") + "/chat/completions",
                           key_list(os.environ.get(f"{prefix}_KEY")), body, 30)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"[:200]
        if "HTTP 401" in msg or "HTTP 403" in msg:
            return {"name": label, "ok": False, "detail": f"ключ мёртв — {msg}"}
        return {"name": label, "ok": False, "detail": msg}
    return {"name": label, "ok": True, "detail": f"{url} отвечает"}


def _count_rejects(raw: str, hist: Counter) -> None:
    try:
        data = json.loads(raw)
    except Exception:
        hist["<нечитаемо>"] += 1
        return
    if isinstance(data, dict):
        for reason, cnt in data.items():
            hist[str(reason)] += int(cnt) if isinstance(cnt, (int, float)) else 1
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                reason = item.get("reason") or item.get("error")
                hist[str(reason) if reason else "<без причины>"] += 1
            elif item is not None:
                hist[str(item)] += 1


def quality_checks(db_path: str | Path | None = None) -> list[dict]:
    out: list[dict] = []
    path = Path(db_path or os.environ.get("TG_DB", str(HERE / "tg_index.sqlite")))
    if not path.exists():
        return [{"name": "качество извлечения", "ok": None, "detail": f"нет БД {path}"}]
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        for r in conn.execute(
                "SELECT pack, COUNT(*) n, "
                "SUM(owner IS NOT NULL AND owner != '') owner_n, "
                "SUM(due_ts IS NOT NULL) due_n, "
                "SUM(body IS NOT NULL AND body != '') body_n "
                "FROM entities GROUP BY pack ORDER BY pack"):
            n = r["n"] or 1

            def pct(v, n=n) -> str:
                return f"{100 * (v or 0) // n}%"

            out.append({"name": f"заполненность [{r['pack']}]", "ok": None,
                        "detail": f"n={r['n']}: owner {pct(r['owner_n'])}, "
                                  f"due {pct(r['due_n'])}, body {pct(r['body_n'])}"})
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(extraction_runs)")}
        if "rejects_json" not in cols:
            out.append({"name": "причины отказов", "ok": None,
                        "detail": "нет колонки extraction_runs.rejects_json — "
                                  "гистограмма недоступна"})
            return out
        hist: Counter = Counter()
        for r in conn.execute(
                "SELECT rejects_json FROM extraction_runs "
                "WHERE rejects_json IS NOT NULL AND rejects_json != ''"):
            _count_rejects(r["rejects_json"], hist)
        detail = (", ".join(f"{k}: {v}" for k, v in hist.most_common(10))
                  if hist else "отказов не зафиксировано")
        out.append({"name": "причины отказов", "ok": None, "detail": detail})
    finally:
        conn.close()
    return out


def run_checks(live: bool = False, providers: bool = False) -> list[dict]:
    out: list[dict] = []

    def add(name: str, ok, detail: str = "") -> None:
        out.append({"name": name, "ok": ok, "detail": detail})

    v = sys.version_info
    add("python >= 3.10", v >= (3, 10), f"{v.major}.{v.minor}.{v.micro}")

    try:
        import telethon  # noqa
        add("telethon", True, telethon.__version__)
    except Exception as exc:
        add("telethon", False, f"не установлен: {exc}")

    try:
        import mcp  # noqa
        add("mcp", True, getattr(mcp, "__version__", "ok"))
    except Exception as exc:
        add("mcp", False, f"не установлен: {exc}")

    try:
        import cryptg  # noqa
        add("cryptg (ускорение)", None, "есть")
    except Exception:
        add("cryptg (ускорение)", None, "нет — будет работать, но медленнее")

    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        add("sqlite FTS5", True, "поиск по истории будет быстрым")
    except Exception as exc:
        add("sqlite FTS5", False, f"недоступен, поиск уедет на LIKE: {exc}")

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    add("TELEGRAM_API_ID / API_HASH", bool(api_id and api_hash),
        "заданы" if api_id and api_hash else "нет: нужен https://my.telegram.org/apps")

    sess_env = os.environ.get("TELEGRAM_SESSION_STRING")
    sess_name = os.environ.get("TELEGRAM_SESSION_NAME", "tg_agent_session")
    if sess_env:
        add("сессия", True, "TELEGRAM_SESSION_STRING задана")
    else:
        sp = Path(sess_name + ".session")
        if not sp.is_absolute():
            sp = HERE / sp
        if sp.exists():
            mode = oct(sp.stat().st_mode & 0o777)
            warn = "" if mode == "0o600" else f" — права {mode}, лучше chmod 600"
            add("сессия", None, f"{sp}{warn}")
        else:
            add("сессия", None, f"{sp} нет — sync.py создаст при первом логине")

    db_path = Path(os.environ.get("TG_DB", str(HERE / "tg_index.sqlite")))
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        probe = db_path.parent / ".tg_doctor_probe"
        probe.write_text("x")
        probe.unlink()
        size = db_path.stat().st_size if db_path.exists() else 0
        add("индекс БД", True, f"{db_path} ({size} байт)" if size else f"{db_path} (пуста)")
    except Exception as exc:
        add("индекс БД", False, f"не пишется: {exc}")

    try:
        from redact import redact
        masked, kinds = redact("ключ sk-" + "a" * 40 + " и ghp_" + "b" * 40)
        add("redact self-test", "скрыто" in masked and len(kinds) == 2,
            f"маскирует: {sorted(kinds)}")
    except Exception as exc:
        add("redact self-test", False, str(exc))

    try:
        from sanitize import sanitize_user_content
        cleaned = sanitize_user_content("тест\u202eинъекция")
        add("sanitize self-test", "\u202e" not in cleaned, "bidi-символы вычищаются")
    except Exception as exc:
        add("sanitize self-test", False, str(exc))

    if live:
        try:
            import asyncio

            from telethon import TelegramClient
            from telethon.sessions import StringSession

            sess = StringSession(sess_env) if sess_env else sess_name
            client = TelegramClient(sess, int(api_id), api_hash)

            async def probe_tg():
                await client.start()
                me = await client.get_me()
                await client.disconnect()
                return me

            me = asyncio.run(probe_tg())
            add("Telegram (live)", True, f"подключён как id={me.id}")
        except Exception as exc:
            add("Telegram (live)", False, str(exc)[:200])

    if providers:
        for prefix in ("EXTRACT", "DIGEST"):
            out.append(_provider_check(prefix))

    return out


def main() -> int:
    live = "--live" in sys.argv
    providers = "--providers" in sys.argv
    quality = "--quality" in sys.argv
    checks = run_checks(live=live, providers=providers)
    if quality:
        checks.extend(quality_checks())
    failed = False
    for c in checks:
        mark = "✅" if c["ok"] is True else ("⚠️ " if c["ok"] is None else "❌")
        failed = failed or c["ok"] is False
        print(f"{mark} {c['name']}: {c['detail']}")
    print()
    print("ЕСТЬ ОШИБКИ — см. ❌ выше" if failed else "Можно работать: python3 sync.py --dry-run")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
