"use strict";
(() => {
  const W = window.WS,
    { $, esc, get, post, icon, badge, date, relative } = W;
  async function catalog(params, seq) {
    const d = await get("catalog", {
      q: params.get("q") || undefined,
      status: params.get("status") || undefined,
      offset: params.get("offset") || undefined,
      starred: params.get("starred") || undefined,
    });
    if (!W.isCurrent(seq)) return;
    $("#page-actions").innerHTML = "";
    const kindLabel = (kinds) => kinds.map((k) => W.kinds[k] || k).join(" · ");
    const statusBadge = (s, nextDue) =>
      s === "active"
        ? badge("open", nextDue ? `жив, до ${date(nextDue)}` : "жив")
        : badge("done", "истёк");
    $("#view").innerHTML =
      `<form id="catalog-filters" class="filters"><div class="field flex"><label for="catalog-q">Сервис</label><input id="catalog-q" name="q" type="search" value="${esc(params.get("q"))}" placeholder="Название или домен"></div><div class="field"><label for="catalog-status">Состояние</label><select id="catalog-status" name="status"><option value="">Все</option><option value="active" ${params.get("status") === "active" ? "selected" : ""}>Живые</option><option value="expired" ${params.get("status") === "expired" ? "selected" : ""}>Истёкшие</option></select></div><label class="check-row"><input type="checkbox" name="starred" value="1" ${params.get("starred") ? "checked" : ""}><span>Только избранные</span></label><button class="button primary" type="submit">Показать</button></form>` +
      `<div class="cluster" style="margin-bottom:var(--s4)"><span class="muted">${d.total} сервисов после склейки дублей</span></div>` +
      (d.items.length
        ? `<div class="panel"><table class="register"><thead><tr><th class="star-col"></th><th class="title-col">Сервис</th><th>Состояние</th><th>Последняя активность</th><th class="optional">Чатов</th><th class="optional">Всплываний</th></tr></thead><tbody>${d.items.map((s) => `<tr data-entity="${s.entity_id}"><td class="star-col"><button class="star-toggle" data-catalog-star="${esc(s.key)}" aria-pressed="${s.starred ? "true" : "false"}" aria-label="${s.starred ? "Убрать из избранного" : "В избранное"}" title="${s.starred ? "Убрать из избранного" : "В избранное"}">${icon(s.starred ? "starFilled" : "star")}</button></td><td><button class="row-title">${esc(s.title)}</button><small>${esc(kindLabel(s.kinds))}${!s.key.startsWith("id:") && s.count > 1 ? " · склеен по ключу" : ""}</small></td><td data-label="Состояние">${statusBadge(s.status, s.next_due)}</td><td data-label="Активность">${relative(s.last_seen)}</td><td data-label="Чатов" class="optional" title="${esc(s.chats.join(", "))}">${s.chat_count}</td><td data-label="Всплываний" class="optional">${s.count}</td></tr>`).join("")}</tbody></table></div>`
        : W.empty("Сервисов нет", "Извлеки записи пакетом services или измени фильтр.")) +
      (d.total > d.offset + d.items.length || Number(d.offset) > 0
        ? `<div class="pagination"><span>${d.offset + 1}–${d.offset + d.items.length} из ${d.total}</span><div class="cluster"><button class="button" id="catalog-prev" ${!Number(d.offset) ? "disabled" : ""}>Назад</button><button class="button" id="catalog-next" ${d.offset + d.items.length >= d.total ? "disabled" : ""}>Далее</button></div></div>`
        : "");
    $("#catalog-filters").onsubmit = (e) => {
      e.preventDefault();
      W.go("catalog", Object.fromEntries(new FormData(e.currentTarget)));
    };
    $("#catalog-prev")?.addEventListener("click", () =>
      W.go("catalog", { ...Object.fromEntries(params), offset: Math.max(0, Number(d.offset) - Number(d.limit)) }));
    $("#catalog-next")?.addEventListener("click", () =>
      W.go("catalog", { ...Object.fromEntries(params), offset: Number(d.offset) + Number(d.limit) }));
    document.querySelectorAll("[data-catalog-star]").forEach((b) => {
      b.onclick = async (ev) => {
        ev.stopPropagation();
        try {
          await post("catalog/star", {
            key: b.dataset.catalogStar,
            starred: b.getAttribute("aria-pressed") !== "true",
          });
          W.render();
        } catch (err) {
          W.toast(err.message || "Не удалось изменить избранное");
        }
      };
    });
  }
  W.screens.catalog = catalog;
})();
