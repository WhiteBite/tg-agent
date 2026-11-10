"use strict";
(() => {
  const W = window.WS,
    { $, esc, icon, get, post, badge, time } = W;
  const reasonLabels = {
    overdue: "Просрочено",
    blocked: "Заблокировано",
    missing_due: "Нет срока",
    missing_owner: "Нет ответственного",
    question: "Ожидает ответа",
    review_pending: "Нужна проверка",
    reply_received: "Есть ответ, нужна проверка",
    reaction_received: "Есть реакции, нужна проверка",
  };
  async function attention(params, seq) {
    const d = await get("home", { project: params.get("project") });
    if (!W.isCurrent(seq)) return;
    const c = d.counts || {};
    $("#page-actions").innerHTML =
      `<button class="button primary" id="new-project">${icon("plus")}Подключить проект</button>`;
    const rows = d.attention || [];
    $("#view").innerHTML =
      `<div class="metric-grid"><button class="metric urgent" data-home-filter="overdue"><span>Просрочено</span><strong>${c.overdue || 0}</strong><small>Срок прошёл, запись открыта</small></button><button class="metric" data-home-filter="upcoming"><span>Истекает</span><strong>${c.upcoming || 0}</strong><small>На ближайшей неделе</small></button><button class="metric" data-home-filter="blocked"><span>Заблокировано</span><strong>${c.blocked || 0}</strong><small>Нужно убрать препятствие</small></button><button class="metric" data-home-filter="questions"><span>Без ответа</span><strong>${c.open_questions || 0}</strong><small>Открытые вопросы</small></button></div>
      <div class="section-heading"><h2>Очередь внимания</h2><a href="#tasks?due=overdue">Все задачи ${icon("arrow")}</a></div>
      <div class="panel">${rows.length ? rows.map((e) => `<article class="attention-row"><div class="row-icon ${e.reasons?.includes("overdue") ? "danger" : ""}">${icon(e.kind === "question" ? "questions" : "clock")}</div><div class="row-body"><button class="row-title" data-entity="${e.id}">${esc(e.title)}</button><div class="row-meta"><span>${esc(e.project || "Без проекта")}</span><span>${esc(e.owner || "Ответственный не определён")}</span><span>${esc((e.reasons || []).map((r) => reasonLabels[r] || r).join(" · "))}</span></div></div>${badge(e.status)}${e.due_ts ? `<span class="row-due">${W.date(e.due_ts)}</span>` : ""}</article>`).join("") : W.empty("Сейчас ничего не требует внимания", "Это данные локального индекса. Убедитесь, что переписка синхронизируется.", '<span id="attention-empty-action"><a class="button" href="#settings">Проверить подключение</a></span>')}</div>
      <div class="section-heading section-gap"><h2>Последние изменения</h2><a href="#projects">По проектам</a></div><div class="panel">${
        (d.recent_changes || []).length
          ? d.recent_changes
              .slice(0, 8)
              .map(
                (ev) =>
                  `<article class="attention-row"><div class="row-body"><button class="row-title" data-entity="${ev.entity_id}">${esc(ev.title)}</button><div class="row-meta"><span>${esc(ev.project || "Без проекта")}</span><span>${ev.field ? esc(W.fieldLabel(ev.field)) : "Запись"}: ${esc(W.valueLabel(ev.to_val || ev.op, ev.field))}</span><span>${time(ev.ts)}</span></div></div></article>`,
              )
              .join("")
          : W.empty(
              "История пока пуста",
              "После извлечения здесь появятся новые задачи и изменения.",
            )
      }</div>`;
    $("#new-project").onclick = () => W.setupProject();
    if (!rows.length) W.offerAddChat("attention-empty-action");
    document.querySelectorAll("[data-home-filter]").forEach(
      (b) =>
        (b.onclick = () => {
          const f = b.dataset.homeFilter;
          if (f === "questions") W.go("questions");
          else if (f === "upcoming")
            W.go("tasks", { due: "upcoming", kinds: W.allKinds });
          else
            W.go(
              "tasks",
              f === "overdue"
                ? { due: "overdue", kinds: W.allKinds }
                : { status: "blocked", kinds: W.allKinds },
            );
        }),
    );
  }
  async function projects(params, seq) {
    const d = await get("projects");
    if (!W.isCurrent(seq)) return;
    $("#page-actions").innerHTML =
      `<button class="button primary" id="new-project">${icon("plus")}Подключить проект</button>`;
    $("#view").innerHTML =
      `<section id="projpanel" class="project-list" aria-label="Проекты">${d.items.length ? d.items.map((p) => `<article class="project-item"><div><button class="row-title" data-project="${esc(p.name)}">${esc(p.name)}</button><small>${p.topics?.length || 0} тем · ${p.new_events ? `${p.new_events} изменений не просмотрено` : "Нет непросмотренных изменений"}</small></div><div><div class="number">${p.open || 0}</div><small>Открыто</small></div><div><div class="number ${p.overdue ? "row-due" : ""}">${p.overdue || 0}</div><small>Просрочено</small></div><div><div class="number">${p.open_questions || 0}</div><small>Вопросов</small></div><button class="button" data-project="${esc(p.name)}">Открыть ${icon("arrow")}</button></article>`).join("") : W.empty("Подключите первый проект", "Выберите уже синхронизированные темы и задайте название объекта.", '<button class="button primary" data-action="setup">Подключить проект</button>')}</section>`;
    $("#new-project").onclick = () => W.setupProject();
    document
      .querySelectorAll("[data-project]")
      .forEach(
        (b) =>
          (b.onclick = () => W.go("project", { project: b.dataset.project })),
      );
    document
      .querySelector('[data-action="setup"]')
      ?.addEventListener("click", () => W.setupProject());
  }
  async function project(params, seq) {
    const name = params.get("project");
    if (!name) {
      W.go("projects");
      return;
    }
    const p = await get("project", { project: name });
    if (!W.isCurrent(seq)) return;
    $("#page-title").textContent = p.name;
    $("#page-eyebrow").innerHTML = '<a href="#projects">Все проекты</a>';
    const tab = params.get("tab") || "overview",
      counts = p.counts || {};
    $("#page-actions").innerHTML =
      `<button class="button" id="project-topics">${icon("settings")}Темы проекта</button><a class="button primary" href="#tasks?${W.query({ project: name })}">Открыть задачи</a>`;
    const tabs = [
      ["overview", "Обзор"],
      ["changes", "Изменения"],
      ["tasks", "Задачи"],
      ["decisions", "Решения"],
      ["questions", "Вопросы"],
      ["messages", "Переписка"],
    ];
    $("#view").innerHTML =
      `<div class="filter-tabs" role="tablist" aria-label="Разделы проекта">${tabs.map(([key, label]) => `<button class="tab" data-project-tab="${key}" role="tab" aria-selected="${key === tab}">${label}</button>`).join("")}</div><div id="project-body"></div>`;
    document.querySelectorAll("[data-project-tab]").forEach(
      (b) =>
        (b.onclick = () => {
          const t = b.dataset.projectTab;
          if (["tasks", "decisions", "questions"].includes(t))
            W.go(t, { project: name });
          else if (t === "messages") W.go("messages", { project: name });
          else W.go("project", { project: name, tab: t });
        }),
    );
    $("#project-topics").onclick = () => W.setupProject(name);
    if (tab === "changes") {
      await changes(name, p, params);
      return;
    }
    const gaps = await get("entities", {
      project: name,
      limit: 8,
      review_state: "pending",
    });
    if (!W.isCurrent(seq)) return;
    $("#project-body").innerHTML =
      `<div class="metric-grid"><div class="metric"><span>Открыто</span><strong>${p.open || 0}</strong><small>Включая обещания и вопросы</small></div><div class="metric"><span>В работе</span><strong>${counts.in_progress || 0}</strong></div><div class="metric"><span>Заблокировано</span><strong>${counts.blocked || 0}</strong></div><div class="metric urgent"><span>Просрочено</span><strong>${p.overdue || 0}</strong></div></div><div class="section-heading"><h2>Нужна проверка</h2><a href="#tasks?${W.query({ project: name, review_state: "pending" })}">Все записи</a></div>${W.register(gaps.items)}<section class="panel panel-pad section-gap stack"><h2>Источники проекта</h2>${(p.topics || []).map((t) => `<a href="#messages?${W.query({ chat_id: t.chat_id, topic_id: t.topic_id })}">${esc(t.chat_title || "Чат")} / ${esc(t.alias || t.topic_title || "Общая тема")}</a>`).join("") || '<p class="muted">Нет привязанных тем. Добавьте их через «Темы проекта».</p>'}<div class="cluster"><button class="button" id="open-project-changes">${p.new_events || 0} изменений с прошлой проверки ${icon("arrow")}</button></div></section>`;
    $("#open-project-changes").onclick = () =>
      W.go("project", { project: name, tab: "changes" });
  }
  async function changes(name, p, params) {
    const after = params.get("after_event_id") || p.reviewed_event_id || 0;
    const d = await get("changes", {
      project: name,
      after_event_id: after,
      offset: params.get("offset") || 0,
      limit: 30,
    });
    const rows = d.items || [];
    $("#project-body").innerHTML =
      `<div class="notice">Отметка просмотра изменений не помечает переписку прочитанной и не подтверждает правильность извлечения.</div><div class="section-heading section-gap"><h2>${d.total} событий после прошлой проверки</h2><button class="button primary" id="review-project" ${!rows.length ? "disabled" : ""}>Отметить показанное просмотренным</button></div><div class="panel">${rows.length ? rows.map((ev) => `<article class="attention-row"><div class="row-body"><button class="row-title" data-entity="${ev.entity_id}">${esc(ev.title)}</button><div class="row-meta"><span>${ev.field ? esc(W.fieldLabel(ev.field)) : "Запись"}: ${esc(W.valueLabel(ev.from_val || "—", ev.field))} → ${esc(W.valueLabel(ev.to_val || ev.op, ev.field))}</span><span>${time(ev.ts)}</span></div></div></article>`).join("") : W.empty("Вы просмотрели изменения", "Новые события появятся после следующего извлечения.")}</div><div class="pagination"><span>${Math.min(Number(d.offset) + rows.length, d.total)} из ${d.total}</span><div class="cluster"><button class="button" id="changes-prev" ${!Number(d.offset) ? "disabled" : ""}>Назад</button><button class="button" id="changes-next" ${Number(d.offset) + rows.length >= d.total ? "disabled" : ""}>Далее</button></div></div>`;
    $("#changes-prev").onclick = () =>
      W.go("project", {
        project: name,
        tab: "changes",
        after_event_id: after,
        offset: Math.max(0, Number(d.offset) - 30),
      });
    $("#changes-next").onclick = () =>
      W.go("project", {
        project: name,
        tab: "changes",
        after_event_id: after,
        offset: Number(d.offset) + 30,
      });
    $("#review-project").onclick = async () => {
      try {
        const through = Math.max(...rows.map((e) => Number(e.id)));
        await post("project/review", {
          project: name,
          through_event_id: through,
        });
        W.toast("Отметка просмотра сохранена");
        W.go("project", { project: name, tab: "changes" });
      } catch (e) {
        W.toast(e.message);
      }
    };
  }
  async function search(params, seq) {
    const q = params.get("q") || "",
      scope = params.get("scope") || "all";
    $("#global-query").value = q;
    const d = await get("search", { ...Object.fromEntries(params), limit: 20 });
    if (!W.isCurrent(seq)) return;
    const projects = await get("projects");
    if (!W.isCurrent(seq)) return;
    const recent = W.saved("ws:searches", []);
    if (q) {
      W.store("ws:searches", [q, ...recent.filter((s) => s !== q)].slice(0, 8));
    }
    const section = (title, items, total, render) =>
      `<section class="section-gap"><div class="section-heading"><h2>${title}</h2><span class="muted">${total} результатов</span></div><div class="panel">${items.length ? items.map(render).join("") : W.empty("Нет совпадений", "Уточните запрос или измените фильтры.")}</div></section>`;
    $("#view").innerHTML =
      `<form id="search-form" class="filters"><div class="field flex"><label for="search-query">Запрос</label><input id="search-query" name="q" type="search" value="${esc(q)}" placeholder="Что найти"></div><div class="field"><label for="search-scope">Искать в</label><select id="search-scope" name="scope"><option value="all">Везде</option><option value="entities" ${scope === "entities" ? "selected" : ""}>Задачи и решения</option><option value="messages" ${scope === "messages" ? "selected" : ""}>Сообщения</option></select></div><div class="field"><label for="search-project">Проект</label><select id="search-project" name="project"><option value="">Все проекты</option>${projects.items.map((p) => `<option ${params.get("project") === p.name ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></div><div class="field"><label for="search-owner">Ответственный</label><input id="search-owner" name="owner" value="${esc(params.get("owner"))}" size="14"></div><div class="field"><label for="search-from">Создано с</label><input id="search-from" type="date" name="created_from" value="${esc(params.get("created_from"))}"></div><div class="field"><label for="search-to">До</label><input id="search-to" type="date" name="created_to" value="${esc(params.get("created_to"))}"></div><button class="button primary">Искать</button></form>${!q && recent.length ? `<div class="cluster"><span class="muted">Недавние:</span>${recent.map((s) => `<button class="button" data-recent="${esc(s)}">${esc(s)}</button>`).join("")}</div>` : ""}
      ${scope !== "messages" ? section("Задачи и решения", d.entities.items, d.entities.total, (e) => `<article class="search-result"><button class="row-title" data-entity="${e.id}">${esc(e.title)}</button><p>${esc(e.body || "")}</p><div class="row-meta">${esc(e.project || "Без проекта")} · ${esc(e.owner || "Не определён")} ${badge(e.status)}</div></article>`) : ""}
      ${scope !== "entities" ? section("Сообщения", d.messages.items, d.messages.total, (m) => `<article class="search-result"><div class="row-meta">${esc(m.sender_name || "Автор неизвестен")} · ${time(m.ts)}</div><p>${esc(m.text)}</p><a class="button" href="#messages?${W.query({ chat_id: m.chat_id, topic_id: m.topic_id, msg_id: m.msg_id })}">Открыть с контекстом ${icon("arrow")}</a></article>`) : ""}
      <div class="cluster section-gap">${d.entities.total > Number(d.entities.offset) + d.entities.items.length ? '<button class="button" id="more-entities">Следующие задачи</button>' : ""}${d.messages.total > Number(d.messages.offset) + d.messages.items.length ? '<button class="button" id="more-messages">Следующие сообщения</button>' : ""}</div>`;
    $("#search-form").onsubmit = (e) => {
      e.preventDefault();
      W.go("search", Object.fromEntries(new FormData(e.currentTarget)));
    };
    document
      .querySelectorAll("[data-recent]")
      .forEach(
        (b) => (b.onclick = () => W.go("search", { q: b.dataset.recent })),
      );
    $("#more-entities")?.addEventListener("click", () =>
      W.go("search", {
        ...Object.fromEntries(params),
        entity_offset: Number(d.entities.offset) + 20,
      }),
    );
    $("#more-messages")?.addEventListener("click", () =>
      W.go("search", {
        ...Object.fromEntries(params),
        message_offset: Number(d.messages.offset) + 20,
      }),
    );
  }
  W.screens.attention = attention;
  W.screens.projects = projects;
  W.screens.project = project;
  W.screens.search = search;
})();
