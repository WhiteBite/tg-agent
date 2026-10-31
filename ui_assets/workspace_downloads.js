"use strict";
(() => {
  const W = window.WS,
    { $, esc, get, post } = W;
  const stateLabels = {
    planned: ["planned", "Запланировано"],
    running: ["in_progress", "Выполняется"],
    paused: ["warning", "Пауза"],
    done: ["done", "Готово"],
    failed: ["rejected", "Упало"],
    cancelled: ["cancelled", "Отменено"],
  };
  const skipLabels = {
    duplicate: "Дубль по id файла",
    "duplicate-content": "Дубль содержимого",
    "too-big": "Больше лимита размера",
    missing: "Сообщение недоступно",
    kind: "Не тот тип медиа",
    "chat-unavailable": "Чат недоступен",
    "forwards-restricted": "Пересылка запрещена каналом",
    other: "Прочее",
  };
  const kindLabels = { video: "Видео", videonote: "Кружок", photo: "Фото" };
  const ACTIVE = new Set(["planned", "running", "paused"]);
  const TABS = [
    ["jobs", "Задания"],
    ["files", "Файлы"],
    ["forwards", "Пересылки"],
    ["collections", "Подборки"],
  ];
  const modeLabel = (mode) => (mode === "forward" ? "Пересылка" : "Скачивание");
  const stateBadge = (state) => {
    const [cls, text] = stateLabels[state] || [state, state];
    return W.badge(cls, text);
  };
  const humanSize = (n) => {
    const b = Number(n) || 0;
    if (b >= 1073741824) return `${(b / 1073741824).toFixed(1)} ГБ`;
    if (b >= 1048576) return `${(b / 1048576).toFixed(1)} МБ`;
    if (b >= 1024) return `${Math.round(b / 1024)} КБ`;
    return `${b} Б`;
  };
  let pollTimer = null;
  async function downloads(params, seq) {
    clearTimeout(pollTimer);
    const id = Number(params.get("job")) || 0;
    if (id) {
      await jobDetail(id, seq);
      return;
    }
    const requested = params.get("tab");
    const tab = TABS.some(([k]) => k === requested) ? requested : "jobs";
    $("#page-actions").innerHTML = "";
    $("#view").innerHTML = `<div class="filter-tabs" role="tablist" aria-label="Разделы загрузок">${TABS.map(
      ([key, label]) =>
        `<button class="tab" data-dl-tab="${key}" role="tab" aria-selected="${key === tab}">${label}</button>`,
    ).join("")}</div><div id="downloads-body"></div>`;
    document
      .querySelectorAll("[data-dl-tab]")
      .forEach(
        (b) => (b.onclick = () => W.go("downloads", { tab: b.dataset.dlTab })),
      );
    if (tab === "files") await filesTab(seq);
    else if (tab === "forwards") await forwardsTab(seq);
    else if (tab === "collections") await collectionsTab(seq);
    else await jobsTab(seq);
  }
  function jobRow(j) {
    const p = j.params || {},
      c = j.counts || {};
    return `<tr><td><button class="row-title" data-job="${j.id}">Задание ${j.id}</button><small>${esc(p.sources || "Источники не указаны")}</small></td><td data-label="Режим">${esc(modeLabel(p.mode))}</td><td data-label="Состояние">${stateBadge(j.state)}</td><td data-label="Прогресс">${c.done || 0} готово · ${c.skipped || 0} пропущено · ${c.failed || 0} упало из ${c.total || 0}</td><td data-label="Создано" class="optional">${W.relative(j.created_ts)}</td><td data-label="Обновлено" class="optional">${W.relative(j.updated_ts || j.created_ts)}</td></tr>`;
  }
  const newJobForm = (cols) =>
    `<section class="panel panel-pad stack"><h2>Новое задание</h2><form id="dl-job-form" class="stack"><fieldset style="border:0;padding:0"><legend class="field-label">Режим</legend><label class="check-row"><input type="radio" name="dl-mode" value="download" checked><span>Скачивание</span></label><label class="check-row"><input type="radio" name="dl-mode" value="forward"><span>Пересылка</span></label></fieldset><div class="field"><label for="dl-sources">Источники</label><input id="dl-sources" placeholder="watch, all, collection:имя, chat:id — через запятую"></div><div class="cluster"><button class="button" type="button" id="dl-pick-watch">Отслеживаемые</button><button class="button" type="button" id="dl-pick-all">Все чаты</button><select id="dl-pick-collection"><option value="">Добавить подборку…</option>${cols
      .map((c) => `<option value="${esc(c.name)}">${esc(c.name)}</option>`)
      .join("")}</select></div><fieldset style="border:0;padding:0"><legend class="field-label">Типы медиа</legend><label class="check-row"><input type="checkbox" name="dl-kind" value="video" checked><span>Видео</span></label><label class="check-row"><input type="checkbox" name="dl-kind" value="videonote"><span>Кружки</span></label><label class="check-row"><input type="checkbox" name="dl-kind" value="photo"><span>Фото</span></label></fieldset><div class="filters"><div class="field"><label for="dl-days">Окно, дней</label><input id="dl-days" type="number" min="0" placeholder="Вся история"></div><div class="field"><label for="dl-max-mb">Лимит размера, МБ</label><input id="dl-max-mb" type="number" min="0" placeholder="По умолчанию; 0 — без лимита"></div></div><div class="field" id="dl-to-field" hidden><label for="dl-to">Адресат</label><input id="dl-to" placeholder="saved, @username или числовой id"></div><div class="cluster"><button class="button primary" type="submit" id="dl-submit">Создать и запустить</button></div><div id="dl-form-error" role="status"></div></form></section>`;
  function bindJobForm() {
    const form = $("#dl-job-form");
    form.querySelectorAll('input[name="dl-mode"]').forEach(
      (r) =>
        (r.onchange = () => {
          if (r.checked) $("#dl-to-field").hidden = r.value !== "forward";
        }),
    );
    const appendToken = (tok) => {
      const inp = $("#dl-sources");
      const parts = inp.value
        .split(",")
        .map((s) => s.trim())
        .filter((s) => s && s !== tok);
      parts.push(tok);
      inp.value = parts.join(",");
    };
    $("#dl-pick-watch").onclick = () => appendToken("watch");
    $("#dl-pick-all").onclick = () => appendToken("all");
    $("#dl-pick-collection").onchange = (ev) => {
      if (ev.target.value) appendToken(`collection:${ev.target.value}`);
      ev.target.value = "";
    };
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      const mode = form.querySelector('input[name="dl-mode"]:checked').value;
      const body = {
        sources: $("#dl-sources").value.trim(),
        kinds: [...form.querySelectorAll('input[name="dl-kind"]:checked')]
          .map((c) => c.value)
          .join(","),
        days: Number($("#dl-days").value) || 0,
        max_mb: $("#dl-max-mb").value.trim(),
      };
      const btn = $("#dl-submit");
      btn.disabled = true;
      $("#dl-form-error").innerHTML = "";
      try {
        let jid;
        if (mode === "forward") {
          body.to = $("#dl-to").value.trim();
          jid = (await post("downloads/forward", body)).job_id;
        } else {
          jid = (await post("downloads/job", body)).job_id;
          await post("downloads/run", { job_id: jid });
        }
        W.toast(`Задание ${jid} запущено`);
        W.go("downloads", { job: jid });
      } catch (e) {
        btn.disabled = false;
        $("#dl-form-error").innerHTML = W.error(e, "reload");
      }
    };
  }
  async function jobsTab(seq) {
    const d = await get("downloads/jobs");
    if (!W.isCurrent(seq)) return;
    let cols = [];
    try {
      cols = (await get("downloads/collections")).collections || [];
    } catch {
      cols = [];
    }
    if (!W.isCurrent(seq)) return;
    const jobs = d.jobs || [];
    $("#downloads-body").innerHTML = `<div class="stack">${newJobForm(cols)}${
      jobs.length
        ? `<div class="panel"><table class="register"><thead><tr><th class="title-col">Задание</th><th>Режим</th><th>Состояние</th><th>Прогресс</th><th class="optional">Создано</th><th class="optional">Обновлено</th></tr></thead><tbody>${jobs.map(jobRow).join("")}</tbody></table></div>`
        : W.empty(
            "Заданий пока нет",
            "Заполните форму выше и нажмите «Создать и запустить».",
            "",
            "downloads",
          )
    }</div>`;
    bindJobForm();
    document
      .querySelectorAll("[data-job]")
      .forEach(
        (b) => (b.onclick = () => W.go("downloads", { job: b.dataset.job })),
      );
  }
  const listFilters = (ns) =>
    `<form class="filters" id="${ns}-filters"><div class="field"><label for="${ns}-chat">Id чата</label><input id="${ns}-chat" type="number" placeholder="Все чаты"></div><div class="field"><label for="${ns}-limit">Лимит</label><input id="${ns}-limit" type="number" min="1" max="500" placeholder="100"></div><button class="button" type="submit">Показать</button></form>`;
  function listTab(seq, ns, endpoint, key, table, emptyHtml) {
    $("#downloads-body").innerHTML = `<div class="stack">${listFilters(ns)}<div id="${ns}-list"></div></div>`;
    const load = async () => {
      const params = {};
      const chat = Number($(`#${ns}-chat`).value);
      const limit = Number($(`#${ns}-limit`).value);
      if (chat) params.chat_id = chat;
      if (limit) params.limit = limit;
      try {
        const d = await get(endpoint, params);
        if (!W.isCurrent(seq)) return;
        const rows = d[key] || [];
        $(`#${ns}-list`).innerHTML = rows.length ? table(rows) : emptyHtml;
      } catch (e) {
        if (!W.isCurrent(seq)) return;
        $(`#${ns}-list`).innerHTML = W.error(e, "reload");
      }
    };
    $(`#${ns}-filters`).onsubmit = (ev) => {
      ev.preventDefault();
      load();
    };
    return load();
  }
  const filesTable = (rows) =>
    `<div class="panel"><table class="register"><thead><tr><th>Тип</th><th class="title-col">Файл</th><th>Размер</th><th class="optional">sha256</th><th>Источников</th><th class="optional">Путь</th></tr></thead><tbody>${rows
      .map(
        (f) =>
          `<tr><td data-label="Тип">${esc(kindLabels[f.kind] || f.kind || "—")}</td><td data-label="Файл">${esc(f.filename || "Без имени")}</td><td data-label="Размер">${humanSize(f.size)}</td><td data-label="sha256" class="optional">${esc((f.sha256 || "").slice(0, 8))}</td><td data-label="Источников">${Number(f.sources) || 0}</td><td data-label="Путь" class="optional">${esc(f.path || "")}</td></tr>`,
      )
      .join("")}</tbody></table></div>`;
  const filesTab = (seq) =>
    listTab(
      seq,
      "dl-files",
      "downloads/files",
      "files",
      filesTable,
      W.empty(
        "Файлов пока нет",
        "Скачайте медиа из отмеченных источников — библиотека появится здесь.",
        '<a class="button" href="#downloads">Создать задание</a>',
        "downloads",
      ),
    );
  const forwardsTable = (rows) =>
    `<div class="panel"><table class="register"><thead><tr><th>Id файла</th><th>Тип</th><th class="title-col">Адресат</th><th>Источников</th><th class="optional">Создано</th></tr></thead><tbody>${rows
      .map(
        (f) =>
          `<tr><td data-label="Id файла">${esc(f.doc_id)}</td><td data-label="Тип">${esc(kindLabels[f.kind] || f.kind || "—")}</td><td data-label="Адресат">${esc(f.target_chat_id)}</td><td data-label="Источников">${Number(f.sources) || 0}</td><td data-label="Создано" class="optional">${W.relative(f.created_ts)}</td></tr>`,
      )
      .join("")}</tbody></table></div>`;
  const forwardsTab = (seq) =>
    listTab(
      seq,
      "dl-fwd",
      "downloads/forwards",
      "forwards",
      forwardsTable,
      W.empty(
        "Пересылок пока нет",
        "Настройте пересылку медиа в свой чат — записи появятся здесь.",
        '<a class="button" href="#downloads">Создать пересылку</a>',
        "downloads",
      ),
    );
  async function collectionsTab(seq) {
    $("#downloads-body").innerHTML = `<div class="stack"><section class="panel panel-pad stack"><h2>Сохранить подборку</h2><form id="dl-col-form" class="stack"><div class="filters"><div class="field"><label for="dl-col-name">Название</label><input id="dl-col-name" maxlength="64" placeholder="Например, Видео-каналы"></div><div class="field"><label for="dl-col-chats">Id чатов</label><input id="dl-col-chats" placeholder="-1001234567890, -1009876543210"></div></div><div class="cluster"><button class="button primary" type="submit" id="dl-col-save">Сохранить</button></div><div id="dl-col-error" role="status"></div></form></section><div id="dl-col-list"></div></div>`;
    const paint = (cols) => {
      $("#dl-col-list").innerHTML = cols.length
        ? `<section class="panel panel-pad stack"><div class="section-heading"><h2>Подборки</h2></div>${cols
            .map(
              (c) =>
                `<article class="attention-row"><div class="row-body"><strong>${esc(c.name)}</strong><div class="row-meta">${
                  (c.chats || []).length
                    ? c.chats
                        .map(
                          (ch) =>
                            `<span class="badge">${esc(ch.title || `Чат ${ch.chat_id}`)}</span>`,
                        )
                        .join("")
                    : "Нет чатов"
                }</div></div><button class="button danger" data-col-delete="${esc(c.name)}">Удалить</button></article>`,
            )
            .join("")}</section>`
        : W.empty(
            "Подборок пока нет",
            "Подборка — сохранённая группа каналов для повторяемых заданий. Сохраните первую в форме выше.",
          );
      document.querySelectorAll("[data-col-delete]").forEach(
        (b) =>
          (b.onclick = async () => {
            b.disabled = true;
            try {
              await post("downloads/collections/delete", {
                name: b.dataset.colDelete,
              });
              W.toast("Подборка удалена");
              await load();
            } catch (e) {
              b.disabled = false;
              W.toast(e.message);
            }
          }),
      );
    };
    const load = async () => {
      try {
        const d = await get("downloads/collections");
        if (!W.isCurrent(seq)) return;
        paint(d.collections || []);
      } catch (e) {
        if (!W.isCurrent(seq)) return;
        $("#dl-col-list").innerHTML = W.error(e, "reload");
      }
    };
    $("#dl-col-form").onsubmit = async (ev) => {
      ev.preventDefault();
      const ids = $("#dl-col-chats")
        .value.split(",")
        .map((s) => s.trim())
        .filter(Boolean)
        .map(Number);
      if (ids.some((n) => !Number.isFinite(n))) {
        W.toast("Id чатов — числа через запятую");
        return;
      }
      const btn = $("#dl-col-save");
      btn.disabled = true;
      $("#dl-col-error").innerHTML = "";
      try {
        await post("downloads/collections", {
          name: $("#dl-col-name").value.trim(),
          chat_ids: ids,
        });
        W.toast("Подборка сохранена");
        $("#dl-col-name").value = "";
        $("#dl-col-chats").value = "";
        await load();
      } catch (e) {
        $("#dl-col-error").innerHTML = W.error(e, "reload");
      }
      btn.disabled = false;
    };
    await load();
  }
  function actionsHtml(j, r) {
    const a = [];
    if (j.state === "planned")
      a.push('<button class="button primary" data-job-action="run">Запустить</button>');
    if (j.state === "running")
      a.push('<button class="button" data-job-action="pause">Пауза</button>');
    if (j.state === "paused")
      a.push('<button class="button primary" data-job-action="resume">Продолжить</button>');
    if (j.state === "failed" && (r.failed_total || 0) > 0)
      a.push('<button class="button" data-job-action="retry">Дожать упавшие</button>');
    if (ACTIVE.has(j.state))
      a.push('<button class="button danger" data-job-action="cancel">Отменить</button>');
    return a.join("");
  }
  function paintJob(d, id, seq) {
    const j = d.job,
      r = d.report || {},
      c = r.counts || j.counts || {},
      p = j.params || {},
      failed = r.failed || [],
      skipped = Object.entries(r.skipped || {});
    $("#page-eyebrow").innerHTML = '<a href="#downloads">Задания</a>';
    $("#page-title").textContent = `Задание ${j.id}`;
    $("#page-actions").innerHTML = actionsHtml(j, r);
    $("#view").innerHTML = `<div class="stack">${
      j.error ? `<div class="notice danger">${esc(j.error)}</div>` : ""
    }<div class="metric-grid"><div class="metric"><span>Готово</span><strong>${c.done || 0}</strong></div><div class="metric"><span>Пропущено</span><strong>${c.skipped || 0}</strong></div><div class="metric ${c.failed ? "urgent" : ""}"><span>Упало</span><strong>${c.failed || 0}</strong></div><div class="metric"><span>Всего пунктов</span><strong>${c.total || 0}</strong>${c.planned ? `<small>Осталось ${c.planned}</small>` : ""}</div></div><section class="panel panel-pad stack"><div class="section-heading"><h2>Параметры</h2>${stateBadge(j.state)}</div><dl class="definition-list"><div><dt>Режим</dt><dd>${esc(modeLabel(p.mode))}</dd></div><div><dt>Источники</dt><dd>${esc(p.sources || "Не указаны")}</dd></div><div><dt>Типы медиа</dt><dd>${esc((p.kinds || []).map((k) => kindLabels[k] || k).join(", ") || "Не указаны")}</dd></div><div><dt>Окно</dt><dd>${p.days ? `${esc(p.days)} дн` : "Вся история"}</dd></div><div><dt>Лимит размера</dt><dd>${p.max_mb ? `${esc(p.max_mb)} МБ` : "Без лимита"}</dd></div>${p.mode === "forward" ? `<div><dt>Адресат</dt><dd>${esc(p.to || "Не указан")}</dd></div>` : ""}<div><dt>Создано</dt><dd>${W.time(j.created_ts)}</dd></div><div><dt>Обновлено</dt><dd>${W.relative(j.updated_ts || j.created_ts)}</dd></div></dl></section><section class="panel panel-pad stack"><h2>Причины пропусков</h2>${
      skipped.length
        ? `<div class="cluster">${skipped.map(([k, n]) => `<span class="badge">${esc(skipLabels[k] || k)} · ${n}</span>`).join("")}</div>`
        : '<p class="muted">Пропусков нет.</p>'
    }</section><section class="stack"><h2>Упавшие пункты${r.failed_total ? ` · ${r.failed_total}` : ""}</h2>${
      failed.length
        ? `<div class="panel">${failed.map((f) => `<article class="attention-row"><div class="row-body"><strong>Чат ${esc(f.chat_id)} · сообщение ${esc(f.msg_id)}</strong><div class="row-meta">${esc(f.detail || "Причина не указана")}</div></div></article>`).join("")}</div>${r.failed_total > failed.length ? `<p class="muted">Показаны ${failed.length} из ${r.failed_total}.</p>` : ""}`
        : '<p class="muted">Упавших пунктов нет.</p>'
    }</section></div>`;
    bindActions(id, seq);
  }
  function bindActions(id, seq) {
    document.querySelectorAll("[data-job-action]").forEach(
      (b) =>
        (b.onclick = async () => {
          b.disabled = true;
          const act = b.dataset.jobAction;
          try {
            if (act === "pause") {
              await post("downloads/job/pause", { job_id: id });
              W.toast("Задание поставлено на паузу");
            } else if (act === "run" || act === "resume") {
              await post("downloads/run", { job_id: id });
              W.toast(act === "run" ? "Задание запущено" : "Задание продолжается");
            } else if (act === "retry") {
              await post("downloads/run", { job_id: id, retry: true, force: true });
              W.toast("Дожимаем упавшие пункты");
            } else if (act === "cancel") {
              await post("downloads/job/cancel", { job_id: id });
              W.toast("Задание отменено");
            }
          } catch (e) {
            W.toast(e.message);
          }
          await refreshJob(id, seq);
        }),
    );
  }
  async function refreshJob(id, seq) {
    if (!W.isCurrent(seq)) return;
    try {
      const d = await get("downloads/job", { id });
      if (!W.isCurrent(seq)) return;
      paintJob(d, id, seq);
      if (ACTIVE.has(d.job.state)) schedulePoll(id, seq);
    } catch (e) {
      if (!W.isCurrent(seq)) return;
      $("#view").innerHTML = W.error(e, "reload");
    }
  }
  function onScreen(id) {
    return (
      W.route.name === "downloads" && Number(W.route.params.get("job")) === id
    );
  }
  function schedulePoll(id, seq) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (!W.isCurrent(seq) || !onScreen(id)) return;
      try {
        const d = await get("downloads/job", { id });
        if (!W.isCurrent(seq) || !onScreen(id)) return;
        paintJob(d, id, seq);
        if (ACTIVE.has(d.job.state)) schedulePoll(id, seq);
      } catch {
        if (W.isCurrent(seq) && onScreen(id)) schedulePoll(id, seq);
      }
    }, 2000);
  }
  async function jobDetail(id, seq) {
    let d;
    try {
      d = await get("downloads/job", { id });
    } catch (e) {
      if (!W.isCurrent(seq)) return;
      $("#page-actions").innerHTML = "";
      $("#view").innerHTML = `<div class="stack">${W.error(e, "reload")}<div class="cluster"><a class="button" href="#downloads">К списку заданий</a></div></div>`;
      return;
    }
    if (!W.isCurrent(seq)) return;
    paintJob(d, id, seq);
    if (ACTIVE.has(d.job.state)) schedulePoll(id, seq);
  }
  W.screens.downloads = downloads;
})();
