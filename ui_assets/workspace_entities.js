"use strict";
(() => {
  const W = window.WS,
    { $, esc, icon, get, post, badge, date, time, kinds, statuses } = W;
  const reviews = {
    pending: "Не проверено",
    approved: "Проверено",
    rejected: "Отклонено",
  };
  const ops = {
    new: "Извлечено из переписки",
    update: "Изменено",
    close: "Завершено",
    reopen: "Переоткрыто",
    start: "Взято в работу",
    block: "Заблокировано",
    unblock: "Блок снят",
    supersede: "Решение заменено",
    merge: "Объединено",
    human_edit: "Исправлено человеком",
    human_review: "Проверено человеком",
    auto_close: "Закрыто автоматически",
    reply_received: "Получен ответ",
    reaction_received: "Получены реакции",
  };
  const fields = {
    title: "Название",
    body: "Описание",
    owner: "Ответственный",
    due_ts: "Срок",
    status: "Статус",
    kind: "Тип",
    review_state: "Проверка",
  };
  W.eventLabel = (value) => ops[value] || value;
  W.fieldLabel = (value) =>
    fields[value] || { response: "Ответ требует проверки" }[value] || value;
  function options(values, current, blank) {
    return (
      `<option value="">${esc(blank)}</option>` +
      Object.entries(values)
        .map(
          ([key, label]) =>
            `<option value="${esc(key)}" ${key === current ? "selected" : ""}>${esc(label)}</option>`,
        )
        .join("")
    );
  }
  function pageLink(params, updates) {
    W.go(W.route.name, { ...Object.fromEntries(params), ...updates });
  }
  async function filters(params) {
    const projects = await get("projects");
    return `<form id="entity-filters" class="filters"><div class="field flex"><label for="filter-q">Название или описание</label><input id="filter-q" name="q" type="search" value="${esc(params.get("q"))}" placeholder="Например, смета"></div>
      <div class="field"><label for="filter-project">Проект</label><select id="filter-project" name="project">${options(Object.fromEntries(projects.items.map((p) => [p.name, p.name])), params.get("project"), "Все проекты")}</select></div>
      <div class="field"><label for="filter-owner">Ответственный</label><input id="filter-owner" name="owner" value="${esc(params.get("owner"))}" placeholder="Имя в переписке" size="16"></div>
      <div class="field"><label for="filter-status">Статус</label><select id="filter-status" name="status">${options({ open: "Открыто", in_progress: "В работе", blocked: "Заблокировано", done: "Завершено" }, params.get("status"), "Все статусы")}</select></div>
      <div class="field"><label for="filter-due">Срок</label><select id="filter-due" name="due">${options({ overdue: "Просрочено", upcoming: "На неделе", missing: "Без срока" }, params.get("due"), "Любой срок")}</select></div>
      <button class="button primary" type="submit">Применить</button><button class="button" type="button" id="reset-filters">Сбросить</button></form>`;
  }
  function register(items) {
    if (!items.length)
      return W.empty(
        "Ничего не найдено",
        "Измените фильтры или подключите темы проекта.",
        '<a class="button" href="#settings">Настроить проект</a>',
      );
    return `<div class="panel"><table class="register"><thead><tr><th class="title-col"><button data-sort="title">Название ↕</button></th><th>Ответственный</th><th><button data-sort="due_ts">Срок ↕</button></th><th>Статус</th><th class="optional">Проверка</th><th class="optional">101</th></tr></thead><tbody>${items.map((e) => `<tr><td><button class="row-title" data-entity="${e.id}">${esc(e.title)}</button><small>${esc(e.project || "Без проекта")} · ${esc(kinds[e.kind] || e.kind)}</small></td><td data-label="Ответственный">${esc(e.owner || "Не определён")}</td><td data-label="Срок"><span class="${e.due_ts && !e.closed_ts && e.due_ts < Date.now() / 1000 ? "row-due" : ""}">${date(e.due_ts)}</span></td><td data-label="Статус">${badge(e.status)}</td><td data-label="Проверка" class="optional">${badge(e.review_state || "pending", reviews[e.review_state || "pending"])}</td><td data-label="101" class="optional"><small>${e.external_id ? "Есть локальная запись" : "Нет локальной записи"}</small></td></tr>`).join("")}</tbody></table></div>`;
  }
  async function entities(params, seq) {
    const forced = { decisions: "decision", questions: "question" }[
      W.route.name
    ];
    const p = Object.fromEntries(params);
    const kind = forced || p.kind;
    const data = await get("entities", {
      ...p,
      kind,
      kinds: !kind && W.route.name === "tasks" ? "task,promise" : undefined,
      limit: 30,
    });
    const filterHtml = await filters(params);
    if (!W.isCurrent(seq)) return;
    const saved = W.saved("ws:views", []).filter(
      (v) => v.route === W.route.name,
    );
    $("#view").innerHTML =
      `${filterHtml}<div class="cluster" style="margin-bottom:var(--s4)"><span class="muted">${data.total} записей</span><button class="text-button" id="save-view">Сохранить представление</button>${saved.map((v, i) => `<button class="button saved-view" data-saved-view="${i}">${esc(v.name)}</button>`).join("")}</div>${register(data.items)}<div class="pagination"><span>${data.total ? Number(data.offset) + 1 : 0}–${Math.min(Number(data.offset) + data.items.length, data.total)} из ${data.total}</span><div class="cluster"><button class="button" id="prev-page" ${!Number(data.offset) ? "disabled" : ""}>Назад</button><button class="button" id="next-page" ${Number(data.offset) + data.items.length >= data.total ? "disabled" : ""}>Далее</button></div></div>`;
    $("#entity-filters").onsubmit = (e) => {
      e.preventDefault();
      pageLink(params, {
        ...Object.fromEntries(new FormData(e.currentTarget)),
        offset: 0,
      });
    };
    $("#reset-filters").onclick = () => W.go(W.route.name);
    $("#prev-page").onclick = () =>
      pageLink(params, { offset: Math.max(0, Number(data.offset) - 30) });
    $("#next-page").onclick = () =>
      pageLink(params, { offset: Number(data.offset) + 30 });
    document
      .querySelectorAll("[data-sort]")
      .forEach(
        (b) =>
          (b.onclick = () =>
            pageLink(params, {
              sort: b.dataset.sort,
              order: params.get("order") === "asc" ? "desc" : "asc",
              offset: 0,
            })),
      );
    $("#save-view").onclick = () => {
      const name = prompt(
        "Название представления",
        params.get("project") || "Мой список",
      );
      if (!name) return;
      const all = W.saved("ws:views", []);
      all.push({
        name,
        route: W.route.name,
        params: Object.fromEntries(params),
      });
      W.store("ws:views", all);
      W.toast("Представление сохранено в браузере");
      W.render();
    };
    document
      .querySelectorAll("[data-saved-view]")
      .forEach(
        (b) =>
          (b.onclick = () =>
            W.go(W.route.name, saved[Number(b.dataset.savedView)].params)),
      );
    if (params.get("entity")) openEntity(Number(params.get("entity")));
  }
  async function openEntity(id) {
    const focus = document.activeElement,
      dialog = $("#entity-drawer");
    $("#drawer-content").innerHTML =
      '<div class="drawer-header"><h2 id="drawer-title">Карточка</h2><button class="icon-button" data-close-drawer aria-label="Закрыть карточку">' +
      icon("close") +
      '</button></div><div class="loading">Загружаем историю и доказательства…</div>';
    if (!dialog.open) dialog.showModal();
    dialog.onclose = () => {
      focus?.focus();
    };
    try {
      const d = await get("entity", { id }),
        e = d.entity;
      if (!dialog.open) return;
      const reviewState = e.review_state || "pending";
      const showValue = (field, value) =>
        field === "status"
          ? statuses[value] || value
          : field === "due_ts"
            ? date(value)
            : (value ?? "Не указано");
      $("#drawer-content").innerHTML =
        `<div class="drawer-header"><h2 id="drawer-title" tabindex="-1">${esc(e.title)}</h2><button class="icon-button" data-close-drawer aria-label="Закрыть карточку">${icon("close")}</button></div><div class="drawer-body">
      <div class="cluster">${badge(e.status)}${badge(reviewState, reviews[reviewState])}<button class="text-button" id="copy-entity-link">Скопировать ссылку</button></div>
      <dl class="property-grid"><div><dt>Проект</dt><dd>${esc(e.project || "Без проекта")}</dd></div><div><dt>Тип</dt><dd>${esc(kinds[e.kind] || e.kind)}</dd></div><div><dt>Ответственный</dt><dd>${esc(e.owner || "Не определён")}</dd></div><div><dt>Срок</dt><dd>${date(e.due_ts)}</dd></div></dl>
      <div class="notice">${esc(e.body || "Дополнительного описания нет.")}</div>
      <section class="stack"><h3>Проверка человеком</h3><p class="muted">Исправления и проверка сохраняются отдельно от отметок прочтения.</p><div class="field"><label for="review-note">Комментарий к проверке</label><textarea id="review-note" placeholder="Почему нужно исправить или отклонить"></textarea></div><div class="cluster"><button class="button primary" id="approve-entity">${icon("check")}Подтвердить</button><button class="button" id="edit-entity">Исправить</button><button class="button danger" id="reject-entity">Отклонить</button></div><p class="muted" style="font-size:var(--text-xs)">Отклонение — локальная отметка. Экспортёр пока не использует её как запрет отправки.</p><div id="review-result" role="status"></div></section>
      <section class="stack"><h3>Сообщения-доказательства</h3>${d.evidence.length ? d.evidence.map((ev) => `<article class="evidence"><div class="row-meta"><strong>${esc(ev.sender_name || "Автор неизвестен")}</strong><time>${time(ev.ts)}</time></div><blockquote>${esc(ev.quote || ev.text || "Цитата отсутствует")}</blockquote>${ev.missing ? '<p class="error-text">Сообщения нет в индексе</p>' : ev.deleted ? '<p class="error-text">Сообщение удалено. Цитата сохранена в истории.</p>' : `<a class="button" href="#messages?${W.query({ chat_id: ev.chat_id, topic_id: ev.topic_id || e.topic_id, msg_id: ev.msg_id })}">${icon("source")}Открыть в переписке</a>`}</article>`).join("") : W.empty("Нет доказательств", "Для этой записи пока нет сохранённых цитат.")}</section>
      <section class="stack"><h3>История изменений</h3><div class="timeline">${d.events.length ? d.events.map((ev) => `<article class="timeline-item"><strong>${esc(ops[ev.op] || ev.op)}</strong>${ev.field ? `<p>${esc(fields[ev.field] || ev.field)}: ${esc(showValue(ev.field, ev.from_val))} → ${esc(showValue(ev.field, ev.to_val))}</p>` : ""}<small>${time(ev.ts)}</small>${ev.quote ? `<p>«${esc(ev.quote)}»</p>` : ""}</article>`).join("") : '<p class="muted">История пуста.</p>'}</div></section>
      <section class="notice"><strong>101: ${d.integration?.external_id ? "есть локальная запись" : "не выгружалось"}</strong><p>Совместимость с API не подтверждена. Наличие GUID не доказывает актуальность данных в 101.</p></section></div>`;
      $("#drawer-title").focus();
      $("#copy-entity-link").onclick = async () => {
        try {
          await navigator.clipboard.writeText(
            location.href.split("#")[0] +
              "#tasks?" +
              W.query({ entity: e.id, project: e.project }),
          );
          W.toast("Ссылка скопирована");
        } catch {
          W.toast("Не удалось скопировать ссылку");
        }
      };
      async function review(state) {
        const result = $("#review-result");
        try {
          await post("entity/review", {
            id: e.id,
            state,
            note: $("#review-note").value.trim(),
            expected_revision: d.revision,
          });
          W.toast(
            state === "approved"
              ? "Проверка сохранена"
              : "Запись отмечена отклонённой",
          );
          openEntity(e.id);
        } catch (err) {
          result.innerHTML = W.error(err);
        }
      }
      $("#approve-entity").onclick = () => review("approved");
      $("#reject-entity").onclick = () => review("rejected");
      $("#edit-entity").onclick = () => editEntity(d);
    } catch (e) {
      $("#drawer-content").innerHTML =
        `<div class="drawer-header"><h2 id="drawer-title">Карточка недоступна</h2><button class="icon-button" data-close-drawer aria-label="Закрыть карточку">${icon("close")}</button></div><div class="drawer-body">${W.error(e)}</div>`;
    }
  }
  function editEntity(d) {
    const e = d.entity,
      body = $("#entity-drawer .drawer-body");
    body.innerHTML = `<form id="entity-edit" class="stack"><h3>Исправление записи</h3><div class="field"><label for="edit-title">Название</label><input id="edit-title" name="title" required maxlength="500" value="${esc(e.title)}"></div><div class="field"><label for="edit-body">Описание</label><textarea id="edit-body" name="body">${esc(e.body)}</textarea></div><div class="field"><label for="edit-owner">Ответственный</label><input id="edit-owner" name="owner" value="${esc(e.owner)}"></div><div class="field"><label for="edit-due">Срок (местная дата)</label><input id="edit-due" name="due" type="date" value="${e.due_ts ? new Date(e.due_ts * 1000).toISOString().slice(0, 10) : ""}"></div><div class="field"><label for="edit-status">Статус</label><select id="edit-status" name="status">${options({ open: "Открыто", in_progress: "В работе", blocked: "Заблокировано", done: "Завершено" }, e.status, "Выберите статус")}</select></div><div class="cluster"><button class="button primary" type="submit">Сохранить исправления</button><button class="button" type="button" id="cancel-edit">Отменить</button></div><div id="edit-error" role="status"></div></form>`;
    $("#edit-title").focus();
    $("#cancel-edit").onclick = () => openEntity(e.id);
    $("#entity-edit").onsubmit = async (ev) => {
      ev.preventDefault();
      const data = Object.fromEntries(new FormData(ev.currentTarget));
      const submit = ev.submitter;
      submit.disabled = true;
      try {
        await post("entity/edit", {
          id: e.id,
          expected_revision: d.revision,
          fields: {
            title: data.title,
            body: data.body || null,
            owner: data.owner || null,
            status: data.status,
            due_ts: data.due
              ? Math.floor(new Date(data.due + "T23:59:59").getTime() / 1000)
              : null,
          },
        });
        W.toast("Исправления сохранены в истории");
        openEntity(e.id);
      } catch (err) {
        $("#edit-error").innerHTML = W.error(err);
      } finally {
        submit.disabled = false;
      }
    };
  }
  document.addEventListener("click", (e) => {
    const close = e.target.closest("[data-close-drawer]");
    if (close) {
      $("#entity-drawer").close();
      return;
    }
    const b = e.target.closest("[data-entity]");
    if (b) openEntity(Number(b.dataset.entity));
  });
  $("#entity-drawer").addEventListener("click", (e) => {
    if (e.target === $("#entity-drawer")) {
      const r = e.target.getBoundingClientRect();
      if (e.clientX < r.left) e.target.close();
    }
  });
  W.screens.tasks = entities;
  W.screens.decisions = entities;
  W.screens.questions = entities;
  W.openEntity = openEntity;
  W.register = register;
})();
