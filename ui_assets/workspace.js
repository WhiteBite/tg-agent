"use strict";
window.WS = (() => {
  const token = new URLSearchParams(location.search).get("token") || "";
  const $ = (s) => document.querySelector(s);
  const esc = (s) =>
    String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const paths = {
    attention:
      '<circle cx="10" cy="10" r="7"/><path d="M10 6v5m0 3h.01M16 16l4 4"/>',
    projects: '<path d="M3 7h7l2 2h9v11H3zM3 7V4h7l2 3"/>',
    tasks:
      '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="m8 9 1.5 1.5L12 8m2 2h3M8 16h9"/>',
    decisions: '<path d="m5 12 4 4L19 6M5 5h6m-6 15h14"/>',
    questions:
      '<circle cx="12" cy="12" r="9"/><path d="M9 8a3 3 0 0 1 6 0c0 2-3 2-3 5m0 3h.01"/>',
    messages:
      '<path d="M21 11a8 8 0 0 1-8 8H6l-4 3V11a8 8 0 0 1 8-8h3a8 8 0 0 1 8 8Z"/>',
    catalog: '<path d="M4 6h16M4 12h16M4 18h10"/>',
    downloads: '<path d="M12 3v11m-4-4 4 4 4-4M4 17v4h16v-4"/>',
    integrations: '<path d="M8 3v5m8-5v5M6 8h12v4a6 6 0 0 1-12 0zm6 10v4"/>',
    settings:
      '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3" fill="var(--surface)"/><circle cx="15" cy="17" r="3" fill="var(--surface)"/>',
    engine:
      '<circle cx="12" cy="12" r="3"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M19 5l-2 2M7 17l-2 2"/>',
    links:
      '<path d="M9 15l6-6M8 7l1-1a3 3 0 0 1 4 0m-1 11 1 1a3 3 0 0 0 4 0l1-1a3 3 0 0 0 0-4l-1-1M7 12l-1-1a3 3 0 0 1 0-4l1-1a3 3 0 0 1 4 0l1 1"/>',
    global:
      '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/>',
    search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
    back: '<path d="M19 12H5m5-5-5 5 5 5"/>',
    plus: '<path d="M12 4v16M4 12h16"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    source: '<path d="M14 3h7v7m-1-6L10 14M11 3H3v18h18v-8"/>',
    refresh: '<path d="M20 7a8 8 0 1 0 0 10M20 3v5h-5"/>',
    copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  };
  const icon = (name) =>
    `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.tasks}</svg>`;
  async function api(path, body) {
    const response = await fetch(
      path +
        (path.includes("?") ? "&" : "?") +
        "token=" +
        encodeURIComponent(token),
      body === undefined
        ? {}
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
    );
    const data = await response.json();
    if (!response.ok || data.ok === false)
      throw new Error(data.error || `Ошибка запроса (${response.status})`);
    return data;
  }
  const query = (params) =>
    new URLSearchParams(
      Object.entries(params).filter(
        ([, v]) => v !== undefined && v !== null && v !== "",
      ),
    ).toString();
  const get = (path, params = {}) =>
    api(
      `/api/workspace/${path}${Object.keys(params).length ? "?" + query(params) : ""}`,
    );
  const post = (path, body) => api(`/api/workspace/${path}`, body);
  const date = (ts) =>
    ts
      ? new Date(Number(ts) * 1000).toLocaleDateString("ru-RU", {
          day: "numeric",
          month: "short",
          year: "numeric",
        })
      : "Не указан";
  const time = (ts) =>
    ts
      ? new Date(Number(ts) * 1000).toLocaleString("ru-RU", {
          day: "numeric",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
        })
      : "Ещё не было";
  function relative(ts) {
    if (!ts) return "нет данных";
    const minutes = Math.max(
      0,
      Math.floor((Date.now() / 1000 - Number(ts)) / 60),
    );
    return minutes < 1
      ? "только что"
      : minutes < 60
        ? `${minutes} мин назад`
        : minutes < 1440
          ? `${Math.floor(minutes / 60)} ч назад`
          : `${Math.floor(minutes / 1440)} дн назад`;
  }
  const statuses = {
    open: "Открыто",
    in_progress: "В работе",
    blocked: "Заблокировано",
    done: "Завершено",
    closed: "Завершено",
  };
  const kinds = {
    task: "Задача",
    promise: "Обещание",
    question: "Вопрос",
    decision: "Решение",
    service: "Сервис",
    trial: "Пробный период",
    promo: "Предложение",
  };
  const badge = (value, text) =>
    `<span class="badge ${esc(value)}">${esc(text || statuses[value] || value)}</span>`;
  const empty = (title, text, action = "", iconName = "tasks") =>
    `<div class="empty">${icon(iconName)}<h2>${esc(title)}</h2><p>${esc(text)}</p>${action}</div>`;
  const error = (e, retry = "") =>
    `<div class="notice danger error-box"><strong>Не удалось загрузить данные</strong><span>${esc(e.message || e)}</span>${retry ? `<button class="button" data-action="${esc(retry)}">Повторить</button>` : ""}</div>`;
  function toast(text) {
    $("#toast").textContent = text;
    $("#toast").hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => ($("#toast").hidden = true), 4000);
  }
  const headings = {
    attention: [
      "Требует внимания",
      "Сначала важное. Рядом с каждым выводом — его источник.",
    ],
    projects: ["Проекты", "Состояние объектов, изменения и открытые вопросы."],
    tasks: [
      "Задачи и обещания",
      "Ответственные, сроки и статус — без поиска по чатам.",
    ],
    decisions: ["Решения", "Что согласовали и как менялись договорённости."],
    questions: ["Вопросы", "Что ещё требует ответа или подтверждения."],
    messages: [
      "Переписка",
      "Источники и контекст. Отмечайте только нужные темы.",
    ],
    analysis: [
      "Разбор чата",
      "Сводка по теме: записи, пробелы и свежие сообщения.",
    ],
    catalog: [
      "Каталог",
      "Сервисы, триалы и промо — по одной строке на сервис.",
    ],
    downloads: [
      "Загрузки",
      "Скачивание и пересылка медиа из отмеченных источников.",
    ],
    integrations: [
      "Интеграции",
      "Локальное состояние выгрузки и запросы-кандидаты.",
    ],
    engine: ["Движок", "Фоновые демоны: статус, запуск и остановка."],
    links: ["Ссылки", "Что шерили: упоминания, публичные ссылки, инвайты и их статус."],
    global: ["Поиск в Telegram", "Глобальный поиск каналов и групп — не добавляет их в индекс."],
    settings: ["Настройки", "Проекты, люди, уведомления и внешний вид."],
    project: ["Проект", "Контроль проекта по переписке."],
    search: ["Поиск", "Задачи, решения и сообщения с контекстом."],
  };
  const nav = [
    ["attention", "Требует внимания"],
    ["projects", "Проекты"],
    ["catalog", "Каталог"],
    ["tasks", "Задачи"],
    ["decisions", "Решения"],
    ["questions", "Вопросы"],
    ["messages", "Переписка"],
    ["links", "Ссылки"],
    ["global", "Поиск в Telegram"],
    ["downloads", "Загрузки"],
    ["integrations", "Интеграции"],
    ["engine", "Движок"],
    ["settings", "Настройки"],
  ];
  let route = { name: "attention", params: new URLSearchParams() };
  const screens = {};
  let serial = 0;
  const scrollMemory = new Map();
  let currentHash = location.hash;
  function saveScroll() {
    const main = $("#main");
    if (!main) return;
    scrollMemory.set(currentHash, main.scrollTop);
    while (scrollMemory.size > 50)
      scrollMemory.delete(scrollMemory.keys().next().value);
  }
  function go(name, params = {}) {
    saveScroll();
    location.hash = name + (query(params) ? "?" + query(params) : "");
  }
  function saved(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem(key)) ?? fallback;
    } catch {
      return fallback;
    }
  }
  function store(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      toast("Браузер запретил сохранять настройки");
    }
  }
  function applyTheme(value) {
    document.documentElement.dataset.theme =
      value === "system"
        ? matchMedia("(prefers-color-scheme:dark)").matches
          ? "dark"
          : "light"
        : value;
  }
  function setMenu(on) {
    $("#navigation").classList.toggle("visible", on);
    $("#menu-toggle").setAttribute("aria-expanded", String(on));
    $("#nav-scrim").hidden = !on;
  }
  async function render() {
    const seq = ++serial;
    saveScroll();
    currentHash = location.hash;
    const [name, params = ""] = location.hash.slice(1).split("?");
    route = {
      name: headings[name] ? name : "attention",
      params: new URLSearchParams(params),
    };
    const heading = headings[route.name];
    document.title = `TG Agent · ${heading[0]}`;
    $("#page-eyebrow").textContent = "Контроль по переписке";
    $("#page-title").textContent = heading[0];
    $("#page-subtitle").textContent = heading[1];
    $("#page-actions").innerHTML = "";
    $("#breadcrumb").textContent =
      route.name === "project"
        ? "Проекты / " + (route.params.get("project") || "")
        : route.name === "analysis"
          ? "Переписка / Разбор темы"
          : "Рабочее пространство / " + heading[0];
    $("#main-nav").innerHTML = nav
      .map(
        ([key, label]) =>
          `<a class="nav-link" href="#${key}" ${key === route.name || (key === "projects" && route.name === "project") ? 'aria-current="page"' : ""}>${icon(key)}<span>${label}</span></a>`,
      )
      .join("");
    setMenu(false);
    $("#view").innerHTML =
      '<div class="loading" role="status">Загружаем данные…</div>';
    const drawer = $("#entity-drawer");
    if (drawer.open) drawer.close();
    try {
      if (screens[route.name]) await screens[route.name](route.params, seq);
      else
        $("#view").innerHTML = empty("Раздел загружается", "Обновите страницу");
      if (seq === serial) {
        const y = scrollMemory.get(currentHash);
        if (y) $("#main").scrollTop = y;
      }
    } catch (e) {
      if (seq === serial) $("#view").innerHTML = error(e, "reload");
    }
  }
  async function refreshHealth() {
    try {
      const h = await get("home");
      const f = h.freshness || {};
      const stale = (ts) => !ts || Date.now() / 1000 - ts > 7200;
      const rows = [
        ["Сообщения", f.messages_ts],
        ["Извлечение", f.extraction_ts],
        ["Конвейер", f.pipeline_ok_ts],
      ];
      $("#freshness").innerHTML =
        rows
          .map(
            ([label, ts]) =>
              `<span class="${stale(ts) ? "stale" : ""}">${label}: ${relative(ts)}</span>`,
          )
          .join("") +
        (rows.some(([, ts]) => stale(ts))
          ? '<button class="text-button" data-action="sync">Синхронизировать</button>'
          : "");
      $("#footer-state").textContent = "Индекс подключён";
    } catch {
      $("#freshness").innerHTML =
        '<span class="stale">Свежесть данных неизвестна. Проверьте подключение к индексу.</span>';
      $("#footer-state").textContent = "Нет связи с индексом";
    }
  }
  async function syncStale(button) {
    button.disabled = true;
    try {
      await api("/api/sync", {});
      toast("Синхронизация запущена");
      go("messages");
      pollSyncStatus();
    } catch (e) {
      toast(e.message);
      button.disabled = false;
    }
  }
  function pollSyncStatus() {
    api("/api/sync_status")
      .then((s) => {
        if (s.running) setTimeout(pollSyncStatus, 1500);
        else {
          if (s.code)
            toast(
              "Синхронизация не завершена. Проверьте вход в Telegram через CLI.",
            );
          refreshHealth();
        }
      })
      .catch(() => refreshHealth());
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-action]");
    if (b?.dataset.action === "reload") render();
    if (b?.dataset.action === "sync") syncStale(b);
  });
  window.addEventListener("hashchange", render);
  document.addEventListener("DOMContentLoaded", () => {
    applyTheme(saved("ws:theme", "dark"));
    matchMedia("(prefers-color-scheme: dark)").addEventListener(
      "change",
      () => {
        if (saved("ws:theme", "dark") === "system") applyTheme("system");
      },
    );
    $("#menu-toggle").innerHTML = icon("menu");
    $("#nav-close").innerHTML = icon("close");
    $("#search-icon").innerHTML = icon("search");
    $("#menu-toggle").onclick = () =>
      setMenu(!$("#navigation").classList.contains("visible"));
    $("#nav-scrim").onclick = () => setMenu(false);
    $("#nav-close").onclick = () => setMenu(false);
    $("#global-search").onsubmit = (e) => {
      e.preventDefault();
      go("search", { q: $("#global-query").value.trim() });
    };
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") setMenu(false);
    });
    render();
    refreshHealth();
  });
  return {
    $,
    esc,
    icon,
    api,
    get,
    post,
    query,
    date,
    time,
    relative,
    statuses,
    kinds,
    badge,
    empty,
    error,
    toast,
    go,
    saved,
    store,
    applyTheme,
    render,
    refreshHealth,
    screens,
    get route() {
      return route;
    },
    isCurrent: (n) => n === serial,
  };
})();
