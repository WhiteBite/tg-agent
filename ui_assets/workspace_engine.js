"use strict";
(() => {
  const W = window.WS,
    { $, get, post, badge } = W;
  const LABELS = { pipeline: "Цикл конвейера", live: "Приём сообщений (live)" };
  async function engine(params, seq) {
    const d = await get("engine");
    if (!W.isCurrent(seq)) return;
    const card = (name) => {
      const e = d.engines[name] || {};
      const state = e.running ? badge("open", "работает") : badge("done", "остановлен");
      const pid = e.pid ? ` · pid ${e.pid}` : "";
      const heartbeat =
        name === "pipeline" && e.age_min != null
          ? `<div class="row-meta">последний успешный цикл ${e.age_min} мин назад${
              e.stale ? " · метка устарела" : ""
            }</div>`
          : name === "pipeline"
            ? '<div class="row-meta">успешных циклов ещё не было</div>'
            : "";
      const disabled = d.enabled ? "" : "disabled";
      return `<div class="attention-row"><div class="row-body"><strong>${
        LABELS[name]
      }</strong><div class="row-meta">${state}${pid}</div>${heartbeat}</div><div class="cluster"><button class="button" data-engine-start="${name}" ${disabled}>Запустить</button><button class="button" data-engine-stop="${name}" ${disabled}>Остановить</button></div></div>`;
    };
    $("#view").innerHTML =
      `<section class="panel panel-pad stack"><div class="section-heading"><h2>Фоновые демоны</h2>${
        d.enabled
          ? badge("open", "управление включено")
          : badge("warning", "только статус")
      }</div><div class="notice">${
        d.enabled
          ? "Пуск и остановка выполняются локально на этой машине. Стоп завершает процесс по записанному pid."
          : "Панель показывает состояние. Запуск и остановка требуют TG_ALLOW_ENGINE=1 на сервере."
      }</div>${card("pipeline")}${card("live")}</section>`;
    document
      .querySelectorAll("[data-engine-start]")
      .forEach(
        (b) =>
          (b.onclick = () => control("engine/start", b.dataset.engineStart)),
      );
    document
      .querySelectorAll("[data-engine-stop]")
      .forEach(
        (b) => (b.onclick = () => control("engine/stop", b.dataset.engineStop)),
      );
  }
  async function control(path, name) {
    try {
      await post(path, { engine: name });
      W.toast("Готово");
      W.render();
    } catch (err) {
      W.toast(err.message || "Не удалось выполнить действие");
    }
  }
  W.screens.engine = engine;
})();
