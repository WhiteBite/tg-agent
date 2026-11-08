"use strict";
(() => {
  const W = window.WS,
    { $, esc, get, badge, relative } = W;
  const KINDS = {
    invite: "Инвайты",
    mention: "Упоминания",
    url: "Ссылки",
    forward_source: "Источники пересланных",
    forward: "Пересланные",
  };
  const STATUS = {
    ok: ["open", "жива"],
    member: ["open", "вы участник"],
    expired: ["done", "истекла"],
    invalid: ["warning", "невалидна"],
    error: ["warning", "ошибка"],
    unknown: ["pending", "не проверена"],
    alive: ["open", "жива"],
    dead: ["done", "удалена"],
    blocked: ["warning", "снята по жалобе"],
    duplicate: ["pending", "дубль"],
  };
  function statusBadge(s) {
    const [tone, label] = STATUS[s] || ["pending", s];
    return badge(tone, label);
  }
  async function links(params, seq) {
    const d = await get("links", {
      chat_id: params.get("chat_id") || undefined,
      days: params.get("days") || undefined,
    });
    if (!W.isCurrent(seq)) return;
    const groups = Object.entries(d.groups || {}).filter(([, v]) =>
      Array.isArray(v),
    );
    const statusOf = (r) => r.status || r.mega_status || r.mailru_status || "";
    const table = (kind, rows) =>
      `<section class="section-gap"><div class="section-heading"><h2>${
        KINDS[kind] || kind
      }</h2><span class="muted">${rows.length}</span></div><div class="panel"><table class="register"><thead><tr><th class="title-col">Значение</th><th>Раз</th><th class="optional">Последний</th><th>Статус</th></tr></thead><tbody>${rows
        .map(
          (r) =>
            `<tr><td class="title-col">${esc(r.value)}</td><td>${r.n}</td><td class="optional">${
              r.last_ts ? relative(r.last_ts) : "—"
            }</td><td>${statusOf(r) ? statusBadge(statusOf(r)) : "—"}</td></tr>`,
        )
        .join("")}</tbody></table></div></section>`;
    $("#view").innerHTML =
      `<form id="links-filters" class="filters"><div class="field"><label for="links-chat">Чат</label><select id="links-chat" name="chat_id"><option value="">Весь watch-лист</option>${(
        d.chats || []
      )
        .map(
          (c) =>
            `<option value="${c.id}" ${
              String(params.get("chat_id")) === String(c.id) ? "selected" : ""
            }>${esc(c.title)}</option>`,
        )
        .join(
          "",
        )}</select></div><div class="field"><label for="links-days">Окно, дней</label><input id="links-days" name="days" type="number" min="1" value="${esc(
        params.get("days"),
      )}"></div><button class="button primary">Показать</button></form>` +
      (groups.length
        ? groups.map(([kind, rows]) => table(kind, rows)).join("")
        : W.empty(
            "Ссылок нет",
            "Поделённые ссылки, инвайты и упоминания появятся после синхронизации.",
          ));
    $("#links-filters").onsubmit = (e) => {
      e.preventDefault();
      W.go("links", Object.fromEntries(new FormData(e.currentTarget)));
    };
  }
  W.screens.links = links;
})();
