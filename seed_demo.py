#!/usr/bin/env python3
"""Набивает demo.sqlite данными «как в жизни», чтобы посмотреть визуал без Telegram.

Структура повторяет реальные форумы: темы с тысячами сообщений, закрытые темы,
канал с историей в пару лет, непрочитанное в отмеченных темах.
Контент сообщений — синтетический; имена групп/тем — как в демо-скриншоте.
"""

from __future__ import annotations

import asyncio
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_offline import (  # noqa: E402
    Channel, FakeClient, ForumTopicsRes, MessageActionTopicCreated, Reactions,
    MessageMediaPhoto, MessageReplyHeader, MessageServiceMsg, Msg, Topic, User,
)
import sync as sync_mod  # noqa: E402
from db import DB  # noqa: E402

NOW = datetime.now(timezone.utc)
R = random.Random(42)

USERS = [
    User(101, "Максим"), User(102, "Alexey", username="alexey"),
    User(103, "Vadim"), User(104, "Muhammad"), User(105, "Pavel"),
    User(106, "Nazli"), User(107, "Artem", username="artem_hil"),
    User(108, "Rose"), User(109, "En1gman"), User(110, "Joja"),
]

LINES = {
    "Codex": [
        "codex опять зависает на большом репо, кто-то ловил?", "откат на 0.92 помог",
        "а у меня наоборот после апдейта полетело", "кто конфиг для mono-repo собирал, поделитесь",
        "gpt-5 в codex ест токены как не в себя", "поставил лимит на контекст, стало терпимо",
        "у кого codex в CI живёт? стоит?", "я прогнал через pre-commit, норм",
        "ошибка 429 каждые 10 минут, это лимиты?", "да, подними tier",
        "новый sandbox ругается на сеть", "разреши домены в allowlist",
    ],
    "Claude": [
        "важно: внятная однозначность задач от оператора", "согласен, без ТЗ он фантазирует",
        "claude code стал медленнее отвечать?", "у меня после обновления наоборот",
        "кто-то сравнивал с opencode?", "opencode шустрее на больших файлах",
        "закинул ему рефакторинг на 40 файлов, справился", "а у меня половину тестов поломал",
    ],
    "Халва": [
        "кидаешь находку в тему, боты считают реакции", "выплата только за новые посты",
        "не привязал телегу — бонус сгорит?", "нет, привяжешь и начислят",
        "мне $0.2 упало, работает", "реакции ботов не считаются, только уникальные люди",
    ],
    "Мемы": ["pavel: фотография", "ахаха", "это в точку", "сохранил", "ещё кидай"],
    "Новости": ["обзор ИИ за неделю: главное", "google выкатил новую серию моделей",
                "openai опять переименовали продукт", "сводка: релизы, слухи, бумаги"],
    "Ваши проекты": ["заразы: сделал бота для алертов по логам", "код в гит, смотри PR",
                     "кто потестит мой MCP для логов?", "выглядит полезно, гляну"],
    "Полезные ссылки": ["сцена региона freebuff в США, лимиты и прочее",
                        "подборка датасетов для RU-оценки", "статья про кэш эмбеддингов"],
    "General": ["всем привет", "кто тут по ночам пишет?", "пин админа: правила в закрепе",
                "обсудим в темах, тут оффтоп"],
    "ОБСУЖДАЕМ ХАЛЯВУ": ["курсор про промокод, не проверял, увидел китайца в твиттере",
                         "дай код, проверим", "не работает, увы"],
    "Sources": ["слив: датасет на 40гб, зеркало внутри", "осторожно, может быть малварь"],
    "Инфополе": ["опять пишут про регулирование ИИ", "ничего нового"],
    "ML": ["hey there, welcome to ML chat", "как вам новая серия моделей?",
           "бенчмарки подозрительно хорошие", "жду независимых оценок",
           "кто-нибудь файнтюнил 8b?", "на lora за вечер можно"],
    "Sky": ["new stock added: codes available", "price update: stock 14",
            "карты/подписки, наличие в стоке", "пополнение: 21 позиция"],
    "Стройка": [
        "решено: фасад — клинкер, не штукатурка",
        "договорились: кровля из мембраны, не металлочерепица",
        "надо завести кирпич на площадку до 18.11.2026",
        "задача: закрыть контур отопления у Alexey до 20.11.2026",
        "задача: залить отмостку у Максим до 05.12.2026",
        "обещаю прислать смету по фундаменту до пятницы",
        "сделаю акт скрытых работ по армокаркасу",
        "вопрос: когда привезут арматуру?",
        "вопрос: кто согласует проект электрики с сетевой?",
        "решено: меняем предыдущее решение — котельная пристроенная, не встроенная",
        "задача: смонтировать ливнёвку у Vadim до 12.12.2026",
        "обещаю вывести подрядчика на объект во вторник",
        "вопрос: когда начнётся кладка второго этажа?",
    ],
}


def mk_group(cid, title, username, topics, counts, forum=True, days=45):
    """topics: [(topic_id, title, closed)], counts: {topic_id: n}"""
    entity = Channel(cid, title, username, forum=forum)
    if not forum:
        entity.forum = False
    msgs, tl = [], []
    # как в настоящем Telegram: service-сообщение «тема создана» имеет id == topic_id
    if forum:
        for tid, ttitle, closed in topics:
            msgs.append(MessageServiceMsg(tid, NOW - timedelta(days=days), USERS[0],
                                          MessageActionTopicCreated()))
            tl.append(Topic(tid, ttitle, tid, closed=closed))
        mid = max(t.id for t in tl) + 1
    else:
        mid = 1
    span = days * 86400
    for tid, ttitle, closed in (topics if forum else [(1, "General", False)]):
        # закреп = «readme» темы: правила + ссылки + контакт
        reply = MessageReplyHeader(tid, True) if forum and tid != 1 else None
        msgs.append(Msg(mid, NOW - timedelta(days=days - 1), USERS[1],
                        f"📌 {ttitle}: правила и годнота тут https://example.com/{tid} , "
                        f"вопросы — @moderator", pinned=True, reply_to=reply))
        mid += 1
        n = counts.get(tid, 10)
        step = max(span // max(n, 1), 60)
        for i in range(n):
            ts = NOW - timedelta(seconds=span - i * step - R.randint(0, step // 2))
            pool = LINES.get(ttitle, LINES["General"])
            media = MessageMediaPhoto() if R.random() < 0.06 else None
            reply = MessageReplyHeader(tid, True) if forum and tid != 1 else None
            fwd = None
            if R.random() < 0.08:
                from test_offline import FwdFrom
                fwd = FwdFrom(channel_id=R.randint(900000, 900999))
            msgs.append(Msg(mid, ts, R.choice(USERS), R.choice(pool), media=media,
                            reply_to=reply, fwd_from=fwd))
            mid += 1
    # без темы (General в форуме)
    if forum:
        gn = counts.get(1, 5)
        gstep = max(span // max(gn, 1), 60)
        for i in range(gn):
            ts = NOW - timedelta(seconds=span - i * gstep - R.randint(0, gstep // 2))
            msgs.append(Msg(mid, ts, R.choice(USERS), R.choice(LINES["General"])))
            mid += 1
    msgs.sort(key=lambda m: m.id)
    return entity, msgs, tl


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("demo.sqlite")
    for suf in ("", "-wal", "-shm"):
        f = out.with_name(out.name + suf)
        if f.exists():
            f.unlink()

    e1, m1, t1 = mk_group(-100501, "Vibe Dev", "vibedev",
                          [(1, "General", False), (11, "Новости", False), (12, "Codex", False),
                           (13, "Халва", False), (14, "Claude", False), (15, "Мемы", False),
                           (16, "Ваши проекты", False), (17, "Полезные ссылки", False),
                           (18, "OMP", True)],
                          {12: 320, 14: 140, 13: 24, 15: 40, 11: 30, 16: 60, 17: 49, 1: 35, 18: 12})
    e2, m2, t2 = mk_group(-100502, "AI HIL ENGINEERING", None,
                           [(1, "General", False), (2, "ОБСУЖДАЕМ ХАЛЯВУ", False),
                            (3, "Sources", False), (4, "Инфополе", False),
                            (5, "Стройка", False)],
                           {2: 30, 3: 25, 4: 20, 1: 15, 5: 24})
    e3, m3, _ = mk_group(-100503, "ML Chat (Offline)", "mlchat", [], {1: 120}, forum=False)
    # канал с историей в ~2 года
    e4 = Channel(-100504, "Sky | AI Stock", "sky_stock", forum=False)
    e4.broadcast, e4.megagroup = True, False
    m4 = []
    for i in range(140):
        ts = NOW - timedelta(days=R.randint(0, 720), hours=R.randint(0, 23))
        m4.append(Msg(1 + i, ts, USERS[6], R.choice(LINES["Sky"])))
    m4.sort(key=lambda m: m.id)

    # немного реакций для топа в статистике
    for m in m1 + m2 + m3 + m4:
        if R.random() < 0.15:
            m.reactions = Reactions((R.choice(["🔥", "❤", "👍", "🤯"]), R.randint(1, 25)),
                                    (R.choice(["👍", "🤝"]), R.randint(1, 8)))

    client = FakeClient({
        e1.id: {"entity": e1, "messages": m1, "topics": t1},
        e2.id: {"entity": e2, "messages": m2, "topics": t2},
        e3.id: {"entity": e3, "messages": m3, "topics": []},
        e4.id: {"entity": e4, "messages": m4, "topics": []},
    }, folders={7: [e1.id, e3.id], 3: [e2.id]})  # папки как в TG Desktop

    rep = asyncio.run(sync_mod.run(db_path=str(out), client=client))
    # каналы-вещания берутся только по явному имени
    rep2 = asyncio.run(sync_mod.run(db_path=str(out), client=client, names=["@sky_stock"]))
    rep["chats"] += rep2["chats"]
    print(f"синхронизировано чатов: {len(rep['chats'])}, сообщений: "
          f"{sum(c['new_messages'] for c in rep['chats'])}")

    with DB(out) as db:
        db.watch_add(e1.id, 13, alias="Халва (бонусы)", pinned=True)
        db.watch_add(e1.id, 12, alias="Codex")
        db.watch_add(e2.id, 2)
        db.watch_add(e2.id, 5, alias="Стройка (ПМ)", project="стройка")
        # «прочитано до» чуть ниже верхушки — будет непрочитанное
        for chat, tid in [(e1.id, 13), (e1.id, 12), (e2.id, 2), (e2.id, 5)]:
            row = db.conn.execute(
                "SELECT MAX(msg_id) m FROM messages WHERE chat_id=? AND COALESCE(topic_id,1)=?",
                (chat, tid)).fetchone()
            if row and row["m"]:
                db.watch_mark_read(chat, tid, max(0, row["m"] - 4))
    print(f"демо-база: {out}")


if __name__ == "__main__":
    main()
