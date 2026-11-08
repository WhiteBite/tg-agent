"use strict";
(() => {
  const W = window.WS,
    { $, esc, badge } = W;
  const KINDS = { channel: "Канал", group: "Группа", user: "Пользователь" };
  async function global(params, seq) {
    const q = params.get("q") || "",
      kind = params.get("kind") || "";
    $("#global-query").value = q;
    let results = null,
      error = "";
    if (q) {
      try {
        const d = await W.api(
          "/api/search_global" +
            W.query({ q, kind: kind || undefined, limit: 20 }),
        );
        results = d.results || [];
      } catch (e) {
        error = e.message;
      }
    }
    if (!W.isCurrent(seq)) return;
    const row = (r) => {
      const handle = r.username ? `@${esc(r.username)}` : "без @username";
      const people =
        r.participants != null ? ` · ${r.participants} подписчиков` : "";
      return `<article class="search-result"><div class="section-heading"><strong>${esc(
        r.title,
      )}</strong>${badge("pending", KINDS[r.kind] || r.kind)}${
        r.verified ? badge("open", "verified") : ""
      }</div><div class="row-meta">${handle}${people}</div></article>`;
    };
    $("#view").innerHTML =
      `<form id="global-form" class="filters"><div class="field flex"><label for="global-q">Запрос</label><input id="global-q" name="q" type="search" value="${esc(
        q,
      )}" placeholder="Нейросети, RAG, логи"></div><div class="field"><label for="global-kind">Тип</label><select id="global-kind" name="kind"><option value="">Все</option><option value="channel" ${
        kind === "channel" ? "selected" : ""
      }>Каналы</option><option value="group" ${
        kind === "group" ? "selected" : ""
      }>Группы</option><option value="user" ${
        kind === "user" ? "selected" : ""
      }>Пользователи</option></select></div><button class="button primary">Искать</button></form>` +
      `<div class="notice">Поиск по публичным чатам Telegram. Найденное не читается и не индексируется: чтобы следить, добавьте чат в «Переписке». Требует TG_ALLOW_LIVE=1 на сервере.</div>` +
      (error
        ? W.error(error)
        : results
          ? results.length
            ? `<section class="section-gap"><div class="section-heading"><h2>Найдено</h2><span class="muted">${results.length}</span></div><div class="panel">${results
                .map(row)
                .join("")}</div></section>`
            : W.empty("Ничего не найдено", "Измените запрос или тип.")
          : "");
    $("#global-form").onsubmit = (e) => {
      e.preventDefault();
      W.go("global", Object.fromEntries(new FormData(e.currentTarget)));
    };
  }
  W.screens.global = global;
})();
