"use strict";
(() => {
  const W = window.WS,
    { $, esc, icon, get, post } = W;
  async function integrations(params, seq) {
    const s = await get("integration/101/status");
    if (!W.isCurrent(seq)) return;
    $("#view").innerHTML =
      `<div class="connection-grid"><section class="panel panel-pad stack"><div class="section-heading"><h2>101 · задачи проекта</h2>${W.badge("warning", s.credentials_configured ? "Контракт не проверен" : "Не настроено")}</div><div class="notice warning"><strong>Автоматическая отправка из интерфейса отключена.</strong><p>Сваггер описывает авторизацию и маршруты, но не тело задачи. Локальный предпросмотр не проверяет совместимость с настоящей 101.</p></div><dl class="definition-list"><div><dt>Данные для входа</dt><dd>${s.credentials_configured ? "Заданы в окружении сервера" : "Не заданы"}</dd></div><div><dt>Проверка соединения</dt><dd>Не выполнялась</dd></div><div><dt>Привязка проектов</dt><dd>${s.project_mapping_configured ? "Есть локальная настройка" : "Требует настройки"}</dd></div><div><dt>Исполнители в 101</dt><dd>${s.owner_mapping_configured ? "Есть локальная настройка" : "Требует настройки"}</dd></div></dl><h3>Что нужно подтвердить</h3><ul>${(s.blockers || []).map((b) => `<li>${esc(b)}</li>`).join("") || "<li>Получить пример создания задачи и проверить его на тестовом сервере.</li>"}</ul><h3>Настроенный маппинг полей</h3><pre class="code">${esc(JSON.stringify(s.field_map, null, 2))}</pre></section><section class="panel panel-pad stack"><h2>Безопасная проверка</h2><p class="muted">Посмотрите фактические запросы-кандидаты. Они не отправляются, курсоры экспорта не меняются.</p><form id="preview-form" class="stack"><div class="field"><label for="preview-project">Проект</label><select id="preview-project"><option value="">Все проекты</option></select></div><button class="button primary">${icon("search")}Показать запросы</button></form><div class="notice">Секреты и токены здесь не показываются. Наличие локального GUID не доказывает, что задача существует или актуальна в 101.</div></section></div><section class="section-gap" id="integration-preview" aria-live="polite"></section>`;
    const p = await get("projects");
    if (!W.isCurrent(seq)) return;
    $("#preview-project").innerHTML =
      '<option value="">Все проекты</option>' +
      p.items.map((p) => `<option>${esc(p.name)}</option>`).join("");
    $("#preview-form").onsubmit = (e) => {
      e.preventDefault();
      preview($("#preview-project").value, 0);
    };
  }
  async function preview(project, offset) {
    const node = $("#integration-preview");
    node.innerHTML =
      '<div class="loading">Готовим локальный предпросмотр…</div>';
    try {
      const d = await get("integration/101/preview", {
        project,
        offset,
        limit: 10,
      });
      node.innerHTML = `<div class="section-heading"><h2>Запросы-кандидаты · ${d.total}</h2><span class="badge warning">Только предпросмотр</span></div><div class="stack">${d.items.length ? d.items.map((e) => `<article class="panel panel-pad stack"><div class="section-heading"><button class="row-title" data-entity="${e.entity_id}">${esc(e.title)}</button>${W.badge(e.eligible ? "pending" : "warning", e.eligible ? "Кандидат" : "Есть препятствия")}</div><div class="row-meta">${esc(e.project || "Без проекта")} · ${e.pending_event_count} новых событий · ${esc(e.method)} ${esc(e.path)}</div>${e.blockers?.length ? `<div class="notice warning">${e.blockers.map(esc).join(" · ")}</div>` : ""}<pre class="code">${esc(JSON.stringify(e.payload, null, 2))}</pre></article>`).join("") : W.empty("Нет кандидатов", "Для выбранного проекта нет новых задач, подходящих под текущую конфигурацию.")}</div><div class="pagination"><span>${Math.min(offset + d.items.length, d.total)} из ${d.total}</span><div class="cluster"><button class="button" id="preview-prev" ${offset === 0 ? "disabled" : ""}>Назад</button><button class="button" id="preview-next" ${offset + d.items.length >= d.total ? "disabled" : ""}>Далее</button></div></div>`;
      $("#preview-prev").onclick = () =>
        preview(project, Math.max(0, offset - 10));
      $("#preview-next").onclick = () => preview(project, offset + 10);
    } catch (e) {
      node.innerHTML = W.error(e);
    }
  }
  async function settings(params, seq) {
    const [people, projects, alerts] = await Promise.all([
      get("people"),
      get("projects"),
      get("alerts"),
    ]);
    if (!W.isCurrent(seq)) return;
    const theme = W.saved("ws:theme", "dark");
    $("#view").innerHTML =
      `<section class="settings-section"><h2>Проекты и источники</h2><p>Сначала синхронизируйте чат в разделе «Переписка», затем назначьте его темы проекту. Уже назначенные темы можно перенести.</p><button class="button primary" id="setup-project">${icon("plus")}Подключить проект</button><div class="cluster section-gap">${projects.items.map((p) => `<button class="button" data-setup-project="${esc(p.name)}">${esc(p.name)}</button>`).join("")}</div></section>
      <section class="settings-section"><h2>Люди и роли</h2><p>Связи задаются вручную. Имя в переписке не считается подтверждённой личностью или исполнителем в 101.</p><div id="people-list" class="stack">${people.items.length ? people.items.map((p, i) => `<div class="attention-row"><div class="row-body"><strong>${esc(p.display_name || p.owner)}</strong><div class="row-meta">${esc(p.owner)}${p.role ? " · " + esc(p.role) : ""} · ${p.mapped ? "Связь задана" : "Без ручной связи"}</div></div><button class="button" data-person="${i}">Настроить</button></div>`).join("") : W.empty("Люди пока не определены", "Они появятся после извлечения ответственных из переписки.")}</div></section>
      <section class="settings-section"><h2>Живые алерты</h2><p>Слова-триггеры и упоминания действуют сразу, перезапуск движка не нужен. Общий выключатель TG_ALERTS и отправка TG_ALLOW_SEND остаются в окружении сервера.</p><form id="alerts-form" class="stack"><div class="field"><label for="alert-words">Слова-триггеры (через запятую)</label><input id="alert-words" value="${esc((alerts.words || []).join(", "))}" placeholder="Например: раздача, холява"></div><div class="field"><label for="alert-mentions">Упоминания (через запятую)</label><input id="alert-mentions" value="${esc((alerts.mentions || []).join(", "))}" placeholder="Например: myname"></div><div class="cluster"><button class="button primary" type="submit">Сохранить слова</button><span class="muted">Источник сейчас: ${alerts.source === "ui" ? "настройки" : "окружение"}</span></div><div id="alerts-result" role="status"></div></form></section>
      <section class="settings-section"><h2>Предпросмотр сводки</h2><p>Проверьте содержание уведомления без отправки. Получатели задаются в окружении сервера через TG_NOTIFY_ROUTES.</p><form id="notification-form" class="filters"><div class="field"><label for="notification-project">Проект</label><select id="notification-project"><option value="">Все проекты</option>${projects.items.map((p) => `<option>${esc(p.name)}</option>`).join("")}</select></div><button class="button">Посмотреть сводку</button></form><div id="notification-preview"></div></section>
      <section class="settings-section"><h2>Внешний вид</h2><div class="field"><label for="theme">Тема интерфейса</label><select id="theme"><option value="light" ${theme === "light" ? "selected" : ""}>Светлая</option><option value="dark" ${theme === "dark" ? "selected" : ""}>Тёмная</option><option value="system" ${theme === "system" ? "selected" : ""}>Как в системе</option></select></div></section>`;
    $("#theme").onchange = (e) => {
      W.store("ws:theme", e.target.value);
      W.applyTheme(e.target.value);
    };
    $("#alerts-form").onsubmit = async (e) => {
      e.preventDefault();
      const split = (v) =>
        v
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
      try {
        await post("settings/alerts", {
          words: split($("#alert-words").value),
          mentions: split($("#alert-mentions").value),
        });
        W.toast("Слова алертов сохранены");
      } catch (err) {
        $("#alerts-result").innerHTML = W.error(err);
      }
    };
    $("#setup-project").onclick = () => setupProject();
    document
      .querySelectorAll("[data-setup-project]")
      .forEach((b) => (b.onclick = () => setupProject(b.dataset.setupProject)));
    document
      .querySelectorAll("[data-person]")
      .forEach(
        (b) =>
          (b.onclick = () =>
            editPerson(people.items[Number(b.dataset.person)])),
      );
    $("#notification-form").onsubmit = async (e) => {
      e.preventDefault();
      const node = $("#notification-preview");
      try {
        const d = await get("notifications/preview", {
          project: $("#notification-project").value,
        });
        node.innerHTML = `<div class="notice">Только предпросмотр · ${d.destination_configured ? "получатель задан" : "получатель не задан"}. Сообщения не отправляются.</div><pre class="code">${esc(d.lines.join("\n"))}</pre>`;
      } catch (err) {
        node.innerHTML = W.error(err);
      }
    };
  }
  function closeModal() {
    $("#setup-dialog").close();
  }
  let guard = null;
  const setupDialog = $("#setup-dialog");
  function armGuard(form) {
    const g = (guard ||= { dirty: false, warned: false, discard: null });
    const mark = () => (g.dirty = true);
    form.addEventListener("input", mark);
    form.addEventListener("change", mark);
    $("#discard-changes").onclick = () => {
      $("#unsaved-guard").hidden = true;
      const discard = guard?.discard;
      guard = null;
      discard?.();
    };
    $("#keep-editing").onclick = () => {
      $("#unsaved-guard").hidden = true;
      g.warned = false;
    };
  }
  function warnUnsaved(discard) {
    if (!guard || !guard.dirty || guard.warned) return false;
    guard.warned = true;
    guard.discard = discard;
    $("#unsaved-guard").hidden = false;
    return true;
  }
  function requestClose() {
    if (warnUnsaved(closeModal)) return;
    closeModal();
  }
  setupDialog.addEventListener("cancel", (e) => {
    if (warnUnsaved(closeModal)) e.preventDefault();
  });
  setupDialog.addEventListener("click", (e) => {
    if (e.target !== setupDialog) return;
    const r = setupDialog.getBoundingClientRect();
    if (
      e.clientX >= r.left &&
      e.clientX <= r.right &&
      e.clientY >= r.top &&
      e.clientY <= r.bottom
    )
      return;
    if (warnUnsaved(closeModal)) return;
    if (guard) setupDialog.close();
  });
  setupDialog.addEventListener("close", () => {
    guard = null;
  });
  async function setupProject(name = "") {
    const dialog = $("#setup-dialog");
    guard = { dirty: false, warned: false, discard: null };
    $("#setup-content").innerHTML =
      '<h2 id="setup-title">Темы проекта</h2><div class="loading">Читаем индекс…</div><button class="button" id="setup-cancel">Закрыть</button>';
    if (!dialog.open) dialog.showModal();
    $("#setup-cancel").onclick = requestClose;
    try {
      const state = await W.api("/api/state");
      if (!dialog.open) return;
      $("#setup-content").innerHTML =
        `<h2 id="setup-title">${name ? "Изменить темы проекта" : "Подключить проект"}</h2><form id="project-form" class="stack"><div class="field"><label for="project-name">Название проекта</label><input id="project-name" name="project" required maxlength="120" value="${esc(name)}" placeholder="Например, Захарьевская"></div><div class="notice">Назначение темы другому проекту перенесёт её открытые сущности. Переписка и отметки прочтения сохранятся.</div><fieldset style="border:0;padding:0"><legend class="field-label">Выберите синхронизированные темы</legend>${
          state.chats
            .map(
              (c) =>
                `<details ${c.watched.some((w) => w.project === name) && name ? "open" : ""}><summary>${esc(c.title)}</summary>${c.topics
                  .map((t) => {
                    const w = c.watched.find((w) => w.topic_id === t.topic_id);
                    return `<label class="check-row"><input type="checkbox" name="topic" value="${c.id}:${t.topic_id}" ${w?.project === name && name ? "checked" : ""}><span>${esc(w?.alias || t.title)}${w?.project ? `<small> · ${esc(w.project)}</small>` : ""}</span></label>`;
                  })
                  .join("")}</details>`,
            )
            .join("") ||
          '<p class="muted">Нет синхронизированных тем. Сначала добавьте чат в разделе «Переписка».</p>'
        }</fieldset><div id="setup-error" role="status"></div><div class="notice warning" id="unsaved-guard" role="status" hidden>Изменения не сохранены<div class="cluster"><button type="button" class="button danger" id="discard-changes">Всё равно закрыть</button><button type="button" class="button" id="keep-editing">Продолжить правку</button></div></div><div class="modal-footer"><button class="button" type="button" id="setup-cancel">Отменить</button><button class="button primary" type="submit">Назначить выбранные темы</button></div></form>`;
      $("#setup-cancel").onclick = requestClose;
      armGuard($("#project-form"));
      $("#project-name").focus();
      $("#project-form").onsubmit = async (e) => {
        e.preventDefault();
        const topics = [...new FormData(e.currentTarget).getAll("topic")].map(
          (v) => {
            const [chat_id, topic_id] = v.split(":").map(Number);
            return { chat_id, topic_id };
          },
        );
        if (!topics.length) {
          $("#setup-error").textContent = "Выберите хотя бы одну тему.";
          return;
        }
        const project = $("#project-name").value.trim();
        e.submitter.disabled = true;
        try {
          await post("project/topics", { project, topics });
          closeModal();
          W.toast("Темы назначены проекту");
          W.go("project", { project });
        } catch (err) {
          $("#setup-error").innerHTML = W.error(err);
        } finally {
          e.submitter.disabled = false;
        }
      };
    } catch (e) {
      $("#setup-content").innerHTML =
        `<h2 id="setup-title">Проект недоступен</h2>${W.error(e)}<div class="modal-footer"><button class="button" id="setup-cancel">Закрыть</button></div>`;
      $("#setup-cancel").onclick = requestClose;
    }
  }
  function editPerson(p) {
    const d = $("#setup-dialog");
    guard = { dirty: false, warned: false, discard: null };
    $("#setup-content").innerHTML =
      `<h2 id="setup-title">${esc(p.owner)}</h2><form id="person-form" class="stack"><div class="field"><label for="person-name">Имя человека</label><input id="person-name" name="display_name" required value="${esc(p.display_name || p.owner)}"></div><div class="field"><label for="person-role">Роль в проекте</label><input id="person-role" name="role" value="${esc(p.role)}" placeholder="Например, прораб"></div><div class="field"><label for="person-ids">Telegram ID (через запятую)</label><input id="person-ids" name="ids" value="${esc((p.telegram_sender_ids || []).join(", "))}"></div><div class="field"><label for="person-crm">GUID участника в 101</label><input id="person-crm" name="crm_member_guid" value="${esc(p.crm_member_guid)}" placeholder="Необязательно"></div><p class="muted">Ручная справочная связь. Она не меняет исходного автора сообщений.</p><div id="person-error"></div><div class="notice warning" id="unsaved-guard" role="status" hidden>Изменения не сохранены<div class="cluster"><button type="button" class="button danger" id="discard-changes">Всё равно закрыть</button><button type="button" class="button" id="keep-editing">Продолжить правку</button></div></div><div class="modal-footer"><button class="button" type="button" id="cancel-person">Отменить</button><button class="button primary">Сохранить связь</button></div></form>`;
    d.showModal();
    armGuard($("#person-form"));
    $("#person-name").focus();
    $("#cancel-person").onclick = requestClose;
    $("#person-form").onsubmit = async (e) => {
      e.preventDefault();
      const f = Object.fromEntries(new FormData(e.currentTarget)),
        ids = f.ids
          .split(",")
          .map((v) => v.trim())
          .filter(Boolean)
          .map(Number);
      if (ids.some((n) => !Number.isSafeInteger(n))) {
        $("#person-error").textContent =
          "Telegram ID должны быть целыми числами.";
        return;
      }
      try {
        await post("people", {
          key: p.key,
          display_name: f.display_name,
          role: f.role,
          telegram_sender_ids: ids,
          crm_member_guid: f.crm_member_guid,
        });
        closeModal();
        W.toast("Связь сохранена");
        W.render();
      } catch (err) {
        $("#person-error").innerHTML = W.error(err);
      }
    };
  }
  W.screens.integrations = integrations;
  W.screens.settings = settings;
  W.setupProject = setupProject;
})();
