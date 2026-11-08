"use strict";
(() => {
  const W = window.WS,
    { $, esc, icon, get, date, time, kinds } = W;
  const gapText = (g) =>
    `${esc(kinds[g.kind] || g.kind)} · ${esc(g.reason)}` +
    (g.owner ? ` · ${esc(g.owner)}` : "") +
    (g.due_ts ? ` · срок ${date(g.due_ts)}` : "");
  const msgRow = (m) => W.bubble(m, false);
  async function analysis(params, seq) {
    const chatId = Number(params.get("chat_id")) || null,
      topic = Number(params.get("topic_id")) || 1;
    if (!chatId) {
      $("#view").innerHTML = W.empty(
        "Не указан чат",
        "Откройте разбор из раздела «Переписка».",
        '<a class="button" href="#messages">Переписка</a>',
        "messages",
      );
      return;
    }
    const d = await get("analysis", {
      chat_id: chatId,
      topic_id: topic,
      entity_offset: params.get("entity_offset") || undefined,
    });
    if (!W.isCurrent(seq)) return;
    const back = `#messages?${W.query({ chat_id: chatId, topic_id: topic })}`,
      w = d.watch,
      topicTitle = d.topic?.title || "Общая тема";
    $("#page-actions").innerHTML = `<a class="button" href="${back}">${icon("messages")}Открыть в переписке</a>`;
    const summary = w
      ? `${esc(w.alias || topicTitle)} · проект: ${esc(w.project || "не назначен")} · ${w.new_since_read ? `${w.new_since_read} новых` : "прочитано"}`
      : `${esc(topicTitle)} · тема не отмечена для агента`;
    $("#view").innerHTML =
      `<div class="notice"><strong>${esc(d.chat.title || "Чат")}</strong><p>${summary}</p></div>` +
      (d.digests.length
        ? `<details class="notice"><summary>Дайджесты (${d.digests.length})</summary>${d.digests.map((g) => `<article class="timeline-item"><small>${time(g.ts)}</small><p>${esc(g.summary || "Пустой дайджест")}</p></article>`).join("")}</details>`
        : "") +
      `<div class="cluster" style="margin:var(--s4) 0"><span class="muted">Записей: ${d.entities.total} · Открытых: ${d.counts.open}</span>${Object.entries(d.counts.by_kind).map(([k, n]) => `<span class="badge">${esc(kinds[k] || k)}: ${n}</span>`).join("")}</div>` +
      `<section class="stack"><h3>Записи темы</h3>${
        d.entities.items.length
          ? W.register(d.entities.items)
          : W.empty("Записей нет", "Агент ещё не извлекал задачи или решения из этой темы.")
      }</section>` +
      `<section class="stack"><h3>Пробелы (${d.gaps.total})</h3>${
        d.gaps.items.length
          ? d.gaps.items.map((g) => `<div class="attention-row"><div class="row-body"><button class="row-title" data-entity="${g.id}">${esc(g.title)}</button><small>${gapText(g)}</small></div></div>`).join("")
          : '<p class="muted">Открытых вопросов без ответа и обещаний без срока или ответственного нет.</p>'
      }</section>` +
      `<section class="stack message-thread"><h3>Последние сообщения</h3>${
        d.messages.items.length
          ? d.messages.items.map(msgRow).join("")
          : W.empty("Сообщений нет", "Тема ещё не синхронизирована.")
      }</section>`;
    W.regroup(document.querySelector(".message-thread"));
    const items = d.entities.items;
    if (items.length) {
      const pagination = document.createElement("div");
      pagination.className = "pagination";
      pagination.innerHTML =
        `<span>${d.entities.offset + 1}–${d.entities.offset + items.length} из ${d.entities.total}</span><div class="cluster"><button class="button" id="analysis-prev" ${!d.entities.offset ? "disabled" : ""}>Назад</button><button class="button" id="analysis-next" ${d.entities.offset + items.length >= d.entities.total ? "disabled" : ""}>Далее</button></div>`;
      pagination.querySelector("#analysis-prev").onclick = () =>
        W.go("analysis", {
          chat_id: chatId,
          topic_id: topic,
          entity_offset: Math.max(0, d.entities.offset - d.entities.limit),
        });
      pagination.querySelector("#analysis-next").onclick = () =>
        W.go("analysis", {
          chat_id: chatId,
          topic_id: topic,
          entity_offset: d.entities.offset + d.entities.limit,
        });
      document.querySelector("#view .panel").after(pagination);
    }
  }
  W.screens.analysis = analysis;
})();
