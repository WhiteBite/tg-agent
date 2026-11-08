"use strict";
(() => {
  const W = window.WS,
    { $, esc, icon, api, time } = W;
  let state = { chats: [], folders: [] },
    selected = new Map(),
    current = null,
    oldest = null;
  const chatName = (id) =>
    state.chats.find((c) => c.id === Number(id))?.title || "Чат";
  async function messages(params, seq) {
    state = await api("/api/state");
    if (!W.isCurrent(seq)) return;
    const project = params.get("project"),
      chat = Number(params.get("chat_id")) || null,
      topic = Number(params.get("topic_id")) || 1;
    current = { chat, topic };
    selected.clear();
    $("#page-actions").innerHTML =
      `<button class="button" id="sync-now">${icon("refresh")}Синхронизировать</button><button class="button primary" id="add-chat">${icon("plus")}Добавить из Telegram</button>`;
    $("#view").innerHTML =
      `<div class="filters"><div class="field flex"><label for="chat-filter">Найти чат или тему</label><input id="chat-filter" type="search" placeholder="Название чата или темы"></div><div class="field"><label for="source-filter">Источники</label><select id="source-filter"><option value="all">Все чаты</option><option value="watch">Отмеченные</option><option value="forum">Форумы</option><option value="channel">Каналы</option>${(state.folders || []).map((f) => `<option value="folder:${f.id}">${esc(f.title)}</option>`).join("")}</select></div></div><div class="correspondence"><aside class="source-tree" id="source-tree" aria-label="Чаты и темы"></aside><section class="message-pane" aria-label="Сообщения"><div id="message-toolbar" class="message-toolbar"></div><div class="message-list" id="message-list">${W.empty("Выберите тему", "Слева — синхронизированные чаты. Кнопка-звезда добавляет тему в watch-лист.")}</div><div class="copybar cluster" id="copybar" hidden><span id="copy-count"></span><button class="button" id="copy-messages">${icon("copy")}Копировать для агента</button><button class="text-button" id="clear-messages">Снять выделение</button></div></section></div><div id="message-status" role="status"></div>`;
    function tree() {
      const text = $("#chat-filter").value.toLocaleLowerCase(),
        filter = $("#source-filter").value;
      const chats = state.chats
        .filter(
          (c) =>
            !project || (c.watched || []).some((w) => w.project === project),
        )
        .filter(
          (c) =>
            filter === "all" ||
            (filter === "watch" && c.watched.length) ||
            (filter === "forum" && c.is_forum) ||
            (filter === "channel" && c.kind === "channel") ||
            (filter.startsWith("folder:") &&
              (
                state.folders.find((f) => f.id === Number(filter.split(":")[1]))
                  ?.chat_ids || []
              ).includes(c.id)),
        );
      $("#source-tree").innerHTML =
        chats
          .map((c) => {
            const topics = c.topics.filter(
              (t) =>
                !text ||
                (
                  c.title +
                  " " +
                  t.title +
                  " " +
                  ((c.watched || []).find((w) => w.topic_id === t.topic_id)
                    ?.alias || "")
                )
                  .toLocaleLowerCase()
                  .includes(text),
            );
            if (!topics.length && text) return "";
            return `<details ${chat === c.id || text ? "open" : ""}><summary>${esc(c.title)} <small>${c.new_total || 0} новых</small></summary><button class="text-button" data-watch-all="${c.id}">Отметить все темы</button>${topics
              .map((t) => {
                const w = c.watched.find((w) => w.topic_id === t.topic_id);
                return `<div class="topic-entry"><a class="${chat === c.id && topic === t.topic_id ? "selected" : ""}" href="#messages?${W.query({ chat_id: c.id, topic_id: t.topic_id, project })}">${esc(w?.alias || t.title)}${w?.new_since_read ? ` <small>+${w.new_since_read}</small>` : ""}</a><button class="icon-button" data-watch="${c.id}:${t.topic_id}" aria-label="${w ? "Снять отметку" : "Отметить тему"}" title="${w ? "Снять отметку" : "Отметить тему"}">${icon(w ? "check" : "plus")}</button></div>`;
              })
              .join("")}</details>`;
          })
          .join("") ||
        W.empty("Нет чатов", "Синхронизируйте нужный чат или измените фильтр.");
      document.querySelectorAll("[data-watch]").forEach(
        (b) =>
          (b.onclick = async () => {
            const [cid, tid] = b.dataset.watch.split(":").map(Number),
              c = state.chats.find((c) => c.id === cid),
              w = c.watched.find((w) => w.topic_id === tid);
            try {
              await api(w ? "/api/unwatch" : "/api/watch", {
                chat_id: cid,
                topic_id: tid,
              });
              W.render();
            } catch (e) {
              W.toast(e.message);
            }
          }),
      );
      document.querySelectorAll("[data-watch-all]").forEach(
        (b) =>
          (b.onclick = async () => {
            b.disabled = true;
            try {
              const c = state.chats.find(
                (c) => c.id === Number(b.dataset.watchAll),
              );
              for (const t of c.topics)
                await api("/api/watch", {
                  chat_id: c.id,
                  topic_id: t.topic_id,
                });
              W.render();
            } catch (e) {
              W.toast(e.message);
              b.disabled = false;
            }
          }),
      );
    }
    tree();
    $("#chat-filter").oninput = tree;
    $("#source-filter").onchange = tree;
    $("#sync-now").onclick = async () => {
      try {
        await api("/api/sync", {});
        pollSync();
      } catch (e) {
        W.toast(e.message);
      }
    };
    $("#add-chat").onclick = addChat;
    $("#clear-messages").onclick = () => {
      selected.clear();
      document
        .querySelectorAll(".message.selected")
        .forEach((n) => n.classList.remove("selected"));
      updateSelection();
    };
    $("#copy-messages").onclick = async () => {
      const text =
        `Переписка: ${chatName(chat)}\n` +
        [...selected.values()]
          .sort((a, b) => a.msg_id - b.msg_id)
          .map(
            (m) =>
              `- ${m.sender_name || "Неизвестно"} (${time(m.ts)}): ${m.text || ""}`,
          )
          .join("\n");
      try {
        await navigator.clipboard.writeText(text);
        W.toast("Выдержка скопирована");
      } catch {
        const box = document.createElement("textarea");
        box.value = text;
        $("#copybar").append(box);
        box.select();
        if (document.execCommand("copy")) {
          box.remove();
          W.toast("Выдержка скопирована");
        } else W.toast("Скопируйте текст из поля вручную");
      }
    };
    if (chat) {
      const c = state.chats.find((c) => c.id === chat),
        t = c?.topics.find((t) => t.topic_id === topic),
        w = c?.watched.find((w) => w.topic_id === topic);
      $("#message-toolbar").innerHTML =
        `<div class="section-heading"><h2>${esc(w?.alias || t?.title || "Тема")}</h2>${w ? '<button class="button" id="read-topic">Отметить прочитанным</button>' : ""}<button class="button" id="analyze-topic">${icon("arrow")}Разобрать</button></div><div class="row-meta">${esc(c?.title || "Чат")} · ${esc(w?.project || "Проект не назначен")}</div><div class="filters section-gap"><div class="field flex"><label for="message-query">Поиск в этой теме</label><input id="message-query" type="search" placeholder="Слова из сообщения"></div><div class="field"><label for="message-date">К дате</label><input id="message-date" type="date"></div></div>${w ? `<div class="filters"><div class="field flex"><label for="topic-alias">Название для агента</label><input id="topic-alias" value="${esc(w.alias || "")}" placeholder="Короткое название темы"></div><button class="button" id="save-alias">Сохранить</button><button class="button" id="pin-topic">${w.pinned ? "Открепить тему" : "Закрепить тему"}</button></div>` : ""}<div id="message-pins"></div>`;
      $("#analyze-topic")?.addEventListener("click", () =>
        W.go("analysis", { chat_id: chat, topic_id: topic }),
      );
      $("#read-topic")?.addEventListener("click", async () => {
        try {
          await api("/api/mark_read", { chat_id: chat, topic_id: topic });
          W.toast("Тема отмечена прочитанной");
          W.render();
        } catch (e) {
          W.toast(e.message);
        }
      });
      $("#save-alias")?.addEventListener("click", async () => {
        try {
          await api("/api/alias", {
            chat_id: chat,
            topic_id: topic,
            alias: $("#topic-alias").value.trim(),
          });
          W.toast("Название сохранено");
          W.render();
        } catch (e) {
          W.toast(e.message);
        }
      });
      $("#pin-topic")?.addEventListener("click", async () => {
        try {
          await api("/api/pin", {
            chat_id: chat,
            topic_id: topic,
            pinned: !w.pinned,
          });
          W.render();
        } catch (e) {
          W.toast(e.message);
        }
      });
      const pins = (c?.pins || []).filter((p) => (p.topic_id || 1) === topic);
      if (pins.length)
        $("#message-pins").innerHTML =
          `<details class="notice"><summary>Закреплённые сообщения (${pins.length})</summary>${pins.map((p) => `<p>${esc(p.text)}</p>`).join("")}</details>`;
      let timer;
      $("#message-query").oninput = () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          const q = $("#message-query").value.trim();
          try {
            if (!q) {
              await load(chat, topic);
              return;
            }
            const d = await api(
              "/api/search?" + W.query({ q, chat_id: chat, topic_id: topic }),
            );
            $("#message-list").innerHTML = d.hits.length
              ? d.hits.map(messageHtml).join("")
              : W.empty("Ничего не найдено", "Уточните запрос.");
            bindSelection(d.hits);
            regroup();
          } catch (e) {
            $("#message-list").innerHTML = W.error(e);
          }
        }, 300);
      };
      $("#message-date").onchange = () =>
        load(chat, topic, {
          around: Math.floor(
            new Date($("#message-date").value + "T23:59:59").getTime() / 1000,
          ),
        });
      const target = Number(params.get("msg_id"));
      await load(chat, topic, target ? { before: target + 31 } : {});
      if (target) {
        const el = document.querySelector(`[data-message="${target}"]`);
        if (el) {
          el.classList.add("highlight");
          el.scrollIntoView({ block: "center" });
        } else
          $("#message-status").innerHTML =
            '<div class="notice warning">Сообщение отсутствует в этой теме или удалено. Сохранённая цитата доступна в карточке.</div>';
      }
    }
  }
  const badgeMedia = (m) => `<span class="badge">${esc(m)}</span>`;
  function senderTint(name) {
    const s = String(name || "");
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h % 6;
  }
  function initials(name) {
    const parts = String(name || "?").trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return "?";
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }
  function bubble(m, withActions) {
    const name = m.sender_name || "Автор неизвестен";
    const sel = withActions && selected.has(m.msg_id);
    return `<article class="message ${sel ? "selected" : ""}" data-message="${m.msg_id}" data-sender="${esc(name)}" data-tint="${senderTint(name)}"><span class="msg-avatar" aria-hidden="true">${esc(initials(name))}</span><div class="msg-body"><div class="msg-head"><strong>${esc(name)}</strong></div>${m.reply_to ? `<small class="msg-reply">Ответ на сообщение ${m.reply_to}</small>` : ""}<p>${esc(m.text || (m.media ? "Вложение без текста" : "Сервисное сообщение"))}</p><div class="msg-foot"><time>${time(m.ts)}</time>${m.media ? badgeMedia(m.media) : ""}${withActions ? `<button class="text-button" data-select-msg="${m.msg_id}">${icon("copy")}${sel ? "Снять выделение" : "В выдержку"}</button>` : ""}</div></div></article>`;
  }
  const messageHtml = (m) => bubble(m, true);
  function regroup(root) {
    const node = root || $("#message-list");
    if (!node) return;
    let prev = null;
    node.querySelectorAll(".message").forEach((el) => {
      const same = prev !== null && el.dataset.sender === prev;
      el.classList.toggle("same-as-prev", same);
      prev = el.dataset.sender;
    });
  }
  function updateSelection() {
    $("#copybar").hidden = selected.size === 0;
    $("#copy-count").textContent = `Выбрано сообщений: ${selected.size}`;
  }
  function bindSelection(rows) {
    document.querySelectorAll("[data-select-msg]").forEach(
      (b) =>
        (b.onclick = () => {
          const id = Number(b.dataset.selectMsg),
            m = rows.find((m) => m.msg_id === id);
          selected.has(id) ? selected.delete(id) : selected.set(id, m);
          b.closest(".message").classList.toggle("selected", selected.has(id));
          b.innerHTML =
            icon("copy") +
            (selected.has(id) ? "Снять выделение" : "В выдержку");
          updateSelection();
        }),
    );
  }
  let cache = [];
  async function load(chat, topic, extra = {}) {
    try {
      const d = await api(
        "/api/messages?" +
          W.query({ chat_id: chat, topic_id: topic, limit: 60, ...extra }),
      );
      if (
        W.route.name !== "messages" ||
        current.chat !== chat ||
        current.topic !== topic
      )
        return;
      cache = d.messages;
      oldest = d.oldest;
      $("#message-list").innerHTML =
        (d.messages.length
          ? '<button class="button" id="older-messages">Показать старее</button>'
          : "") + d.messages.map(messageHtml).join("") ||
        W.empty(
          "Сообщений пока нет",
          "Синхронизируйте тему или выберите другой источник.",
        );
      bindSelection(cache);
      regroup();
      $("#older-messages")?.addEventListener("click", async function older() {
        try {
          const prev = await api(
            "/api/messages?" +
              W.query({
                chat_id: chat,
                topic_id: topic,
                before: oldest,
                limit: 60,
              }),
          );
          if (!prev.messages.length) {
            this.remove();
            return;
          }
          oldest = prev.oldest;
          cache = [...prev.messages, ...cache];
          this.insertAdjacentHTML(
            "afterend",
            prev.messages.map(messageHtml).join(""),
          );
          bindSelection(cache);
          regroup();
        } catch (e) {
          W.toast(e.message);
        }
      });
    } catch (e) {
      $("#message-list").innerHTML = W.error(e);
    }
  }
  async function pollSync() {
    const node = $("#message-status");
    if (!node) return;
    try {
      const s = await api("/api/sync_status");
      node.innerHTML = `<div class="notice ${s.code && !s.running ? "danger" : ""}">${esc(s.running ? "Синхронизация: " + (s.lines.at(-1) || "идёт загрузка") : s.code === 0 ? "Синхронизация завершена" : "Синхронизация не завершена. " + (s.lines.at(-1) || "Проверьте вход в Telegram через CLI."))}${s.running ? ' <button class="button" id="sync-cancel">Отменить</button>' : ""}</div>`;
      if (s.running) {
        $("#sync-cancel").onclick = async () => {
          $("#sync-cancel").disabled = true;
          try {
            await api("/api/sync/cancel", {});
            W.toast("Синхронизация отменена");
            pollSync();
          } catch (e) {
            node.innerHTML = W.error(e);
            setTimeout(pollSync, 1500);
          }
        };
        setTimeout(pollSync, 1500);
      } else W.refreshHealth();
    } catch (e) {
      node.innerHTML = W.error(e);
    }
  }
  async function addChat() {
    const dialog = $("#setup-dialog");
    $("#setup-content").innerHTML =
      `<h2 id="setup-title">Добавить чат из Telegram</h2><div id="available-chats" class="stack">Загружаем список…</div><div class="modal-footer"><button class="button" id="close-add-chat">Закрыть</button></div>`;
    dialog.showModal();
    $("#close-add-chat").onclick = () => dialog.close();
    try {
      const d = await api("/api/available");
      if (!d.ok) throw new Error(d.error);
      $("#available-chats").innerHTML =
        `<div class="field"><label for="add-filter">Фильтр чатов</label><input id="add-filter" type="search" placeholder="Название чата"></div><div class="field"><label for="add-days">Скачать историю за дней</label><input id="add-days" type="number" value="30" min="0" max="3650"></div><div id="available-rows"></div>`;
      function rows() {
        const q = $("#add-filter").value.toLocaleLowerCase();
        $("#available-rows").innerHTML = d.chats
          .filter((c) => c.title.toLocaleLowerCase().includes(q))
          .slice(0, 50)
          .map(
            (c) =>
              `<div class="attention-row"><div class="row-body">${esc(c.title)}${c.in_index ? '<small class="muted"> · в индексе</small>' : ""}</div><button class="button" data-add-source="${esc(c.username ? "@" + c.username : c.id)}">Синхронизировать</button></div>`,
          )
          .join("");
        document.querySelectorAll("[data-add-source]").forEach(
          (b) =>
            (b.onclick = async () => {
              try {
                await api("/api/sync_chat", {
                  chat: b.dataset.addSource,
                  days: Number($("#add-days").value),
                });
                dialog.close();
                pollSync();
              } catch (e) {
                W.toast(e.message);
              }
            }),
        );
      }
      rows();
      $("#add-filter").oninput = rows;
    } catch (e) {
      $("#available-chats").innerHTML = W.error(e);
    }
  }
  let since = Math.floor(Date.now() / 1000);
  setInterval(async () => {
    if (W.route.name !== "messages" || !current?.chat) return;
    try {
      const d = await api("/api/poll?since=" + since);
      if (d.messages.length)
        since = Math.max(since, ...d.messages.map((m) => m.ts));
      const rows = d.messages.filter(
        (m) =>
          m.chat_id === current.chat &&
          (m.topic_id || 1) === current.topic &&
          !cache.some((c) => c.msg_id === m.msg_id),
      );
      if (rows.length) {
        cache.push(...rows);
        const list = $("#message-list"),
          bottom =
            list.scrollTop + list.clientHeight >= list.scrollHeight - 100;
        list.insertAdjacentHTML("beforeend", rows.map(messageHtml).join(""));
        bindSelection(cache);
        regroup();
        if (bottom) list.scrollTop = list.scrollHeight;
      }
    } catch (e) {
      if ($("#message-status")) $("#message-status").innerHTML = W.error(e);
    }
  }, 5000);
  W.screens.messages = messages;
  W.bubble = bubble;
  W.regroup = regroup;
})();
