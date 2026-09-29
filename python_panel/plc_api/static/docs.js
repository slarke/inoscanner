// Страница /docs: вкладки, общий токен, Swagger UI и монитор WebSocket.
// Пульт управления осями — в control.js (использует window.plcApi).
(function () {
  "use strict";

  const API = "/api/v1";
  const AXES = ["X", "Y", "Z"];
  const LOG_LIMIT = 500;
  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------------ //
  // Хранилище браузера — только удобство, может быть недоступно
  // ------------------------------------------------------------------ //
  function storageGet(store, key) {
    try { return window[store].getItem(key); } catch (e) { return null; }
  }
  function storageSet(store, key, value) {
    try { window[store].setItem(key, value); } catch (e) { /* приватный режим */ }
  }

  // Токен: поле в шапке, иначе — введённый в Swagger «Authorize».
  function currentToken() {
    const own = $("api-token").value.trim();
    if (own) return own;
    try {
      const auth = JSON.parse(storageGet("localStorage", "authorized") || "{}");
      return (auth.bearerAuth && auth.bearerAuth.value) || "";
    } catch (e) {
      return "";
    }
  }

  // ------------------------------------------------------------------ //
  // Вкладки
  // ------------------------------------------------------------------ //
  const TABS = ["control", "rest", "ws"];
  const tabListeners = [];

  function showTab(name) {
    document.querySelectorAll(".tabs button").forEach((b) => {
      b.setAttribute("aria-selected", String(b.dataset.tab === name));
    });
    TABS.forEach((t) => { $("tab-" + t).hidden = t !== name; });
    // Swagger сам пишет в hash (deepLinking) — трогаем hash только для ws/пульта.
    if (name === "ws") history.replaceState(null, "", "#ws");
    else if (name === "control" && location.hash) {
      history.replaceState(null, "", location.pathname);
    }
    tabListeners.forEach((fn) => fn(name));
  }
  document.querySelectorAll(".tabs button").forEach((b) => {
    b.addEventListener("click", () => showTab(b.dataset.tab));
  });
  // #ws — монитор; ссылки Swagger (#/Оси/...) — REST; иначе пульт.
  const initialTab = location.hash === "#ws" ? "ws"
    : location.hash.startsWith("#/") ? "rest" : "control";

  // ------------------------------------------------------------------ //
  // Swagger UI (статика встроена, без внешних запросов)
  // ------------------------------------------------------------------ //
  window.ui = SwaggerUIBundle({
    url: API + "/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    persistAuthorization: true,
    displayRequestDuration: true,
    docExpansion: "list",
    defaultModelsExpandDepth: 0,
    presets: [SwaggerUIBundle.presets.apis],
    layout: "BaseLayout",
  });

  // ------------------------------------------------------------------ //
  // Индикатор связи сервера с ПЛК
  // ------------------------------------------------------------------ //
  function setPill(el, text, cls) {
    el.textContent = text;
    el.className = "pill" + (cls ? " " + cls : "");
  }

  async function refreshHealth() {
    const pill = $("link-state");
    const headers = {};
    const token = currentToken();
    if (token) headers.Authorization = "Bearer " + token;
    try {
      const r = await fetch(API + "/health", { headers });
      if (r.status === 401) return setPill(pill, "нужен токен", "off");
      const h = await r.json();
      setPill(pill, h.connected ? "ПЛК: связь есть" : "ПЛК: нет связи",
              h.connected ? "on" : "off");
      pill.title = h.detail || "";
    } catch (e) {
      setPill(pill, "сервер недоступен", "off");
    }
  }
  refreshHealth();
  setInterval(refreshHealth, 3000);

  // ------------------------------------------------------------------ //
  // WebSocket-монитор
  // ------------------------------------------------------------------ //
  let ws = null;
  let frames = 0;
  let firstFrameAt = 0;
  let pendingTelemetry = null;

  const tokenListeners = [];
  $("api-token").value = storageGet("sessionStorage", "plc-api-token") || "";
  $("api-token").addEventListener("change", () => {
    storageSet("sessionStorage", "plc-api-token", $("api-token").value.trim());
    refreshHealth();
    tokenListeners.forEach((fn) => fn());
  });

  function selectedEvents() {
    return Array.from(document.querySelectorAll("#ws-events input:checked"))
      .map((i) => i.value);
  }

  function intervalMs() {
    const v = Number($("ws-interval").value);
    return Number.isFinite(v) && v >= 0 ? Math.round(v) : 0;
  }

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const q = new URLSearchParams();
    const events = selectedEvents();
    if (events.length) q.set("events", events.join(","));
    q.set("telemetry_interval_ms", String(intervalMs()));
    const token = currentToken();
    if (token) q.set("token", token);
    return proto + "//" + location.host + API + "/ws?" + q.toString();
  }

  function setWsState(text, cls) { setPill($("ws-state"), text, cls); }

  function connect() {
    if (!selectedEvents().length) {
      logLine("error", "выберите хотя бы одно событие");
      return;
    }
    frames = 0;
    firstFrameAt = 0;
    ws = new WebSocket(wsUrl());
    setWsState("подключение…", "");
    $("ws-connect").textContent = "Отключить";
    ws.onopen = () => {
      setWsState("подключено", "on");
      $("ws-ping").disabled = false;
    };
    ws.onmessage = (m) => {
      let msg;
      try { msg = JSON.parse(m.data); } catch (e) { return; }
      handle(msg);
    };
    ws.onclose = (e) => {
      setWsState(e.code === 1000 || e.code === 1005 ? "отключено"
                 : "закрыто (" + e.code + ")", "off");
      if (e.code === 1006 && !e.wasClean) {
        logLine("error", "соединение не установлено или оборвано — проверьте токен и сервер");
      }
      $("ws-connect").textContent = "Подключить";
      $("ws-ping").disabled = true;
      ws = null;
    };
  }

  function disconnect() { if (ws) ws.close(1000); }

  $("ws-connect").addEventListener("click", () => (ws ? disconnect() : connect()));
  $("ws-ping").addEventListener("click", () => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
  });

  // Смена подписки на лету, без переподключения.
  function resubscribe() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({
      type: "subscribe", events: selectedEvents(), telemetry_interval_ms: intervalMs(),
    }));
  }
  document.querySelectorAll("#ws-events input").forEach((i) => {
    i.addEventListener("change", resubscribe);
  });
  $("ws-interval").addEventListener("change", resubscribe);

  function handle(msg) {
    switch (msg.type) {
      case "hello":
        logLine("hello", "версия " + msg.version + ", ПЛК: "
                + (msg.status.connected ? "связь есть" : "нет связи")
                + ", события: " + msg.events.join(", "));
        if (msg.telemetry) queueTelemetry(msg.telemetry);
        break;
      case "telemetry":
        frames += 1;
        if (!firstFrameAt) firstFrameAt = performance.now();
        queueTelemetry(msg.data);
        if ($("log-telemetry").checked) logLine("telemetry", summarize(msg.data));
        break;
      case "connection":
        logLine("connection", (msg.connected ? "связь с ПЛК есть" : "нет связи")
                + (msg.detail ? " — " + msg.detail : ""));
        refreshHealth();
        break;
      case "command":
        logLine("command", msg.command + (msg.axis ? " " + msg.axis : "")
                + " " + JSON.stringify(msg.params));
        break;
      case "arrived":
        logLine("arrived", msg.axis + " в точке " + msg.target
                + " (позиция " + fmt(msg.position) + ")");
        break;
      case "blocked":
        logLine("blocked", msg.axis + (msg.blocked ? " ЗАБЛОКИРОВАНА концевиком"
                                                    : " блокировка снята"));
        break;
      case "subscribed":
        logLine("subscribed", "события: " + msg.events.join(", ")
                + ", телеметрия не чаще " + msg.telemetry_interval_ms + " мс");
        break;
      case "pong":
        logLine("pong", "");
        break;
      case "error":
        logLine("error", msg.message);
        break;
      default:
        logLine(msg.type, JSON.stringify(msg));
    }
  }

  // ------------------------------------------------------------------ //
  // Таблица осей (перерисовка не чаще кадра браузера)
  // ------------------------------------------------------------------ //
  function queueTelemetry(data) {
    const first = pendingTelemetry === null;
    pendingTelemetry = data;
    if (first) requestAnimationFrame(renderTelemetry);
  }

  function fmt(v, digits) {
    return v === null || v === undefined ? "—" : Number(v).toFixed(digits === undefined ? 3 : digits);
  }

  function flag(v, yesText, noText, alarm) {
    const span = document.createElement("span");
    if (v === null || v === undefined) {
      span.textContent = "—";
      span.className = "muted";
    } else {
      span.textContent = v ? yesText : noText;
      if (v) span.className = alarm ? "alarm" : "yes";
    }
    return span;
  }

  function renderTelemetry() {
    const data = pendingTelemetry;
    pendingTelemetry = null;
    if (!data) return;
    const body = $("axes-body");
    body.replaceChildren();
    AXES.forEach((name) => {
      const a = data.axes[name];
      if (!a) return;
      const tr = document.createElement("tr");
      const cells = [
        name + (a.active ? "" : " (не подкл.)"),
        fmt(a.position),
        fmt(a.velocity),
        flag(a.power_command, "ВКЛ", "выкл", false),
        flag(a.blocked, "ДА", "нет", true),
      ];
      cells.forEach((c, i) => {
        const td = document.createElement("td");
        if (i === 1 || i === 2) td.className = "num";
        if (c instanceof Node) td.appendChild(c); else td.textContent = c;
        tr.appendChild(td);
      });
      const limits = document.createElement("td");
      limits.append(flag(a.limit_left, "Л", "л", true), " / ",
                    flag(a.limit_right, "П", "п", true));
      tr.appendChild(limits);
      body.appendChild(tr);
    });

    const e = data.errors || {};
    const hasError = e.block_error_id || e.servo_error_id || e.axis_error_id;
    $("tel-errors").textContent = "Ошибки: D200=" + e.block_error_id
      + ", servo=" + e.servo_error_id + ", axis=" + e.axis_error_id;
    $("tel-errors").className = "small" + (hasError ? " alarm" : " muted");

    const t = new Date(data.timestamp * 1000).toLocaleTimeString();
    const secs = firstFrameAt ? (performance.now() - firstFrameAt) / 1000 : 0;
    const rate = secs > 1 ? " · " + (frames / secs).toFixed(1) + " кадр/с" : "";
    $("tel-meta").textContent = "кадр " + t + rate;
  }

  function summarize(d) {
    return AXES.map((n) => d.axes[n] ? n + "=" + fmt(d.axes[n].position, 2) : "")
      .filter(Boolean).join(" ");
  }

  // ------------------------------------------------------------------ //
  // Журнал событий
  // ------------------------------------------------------------------ //
  function logLine(kind, text) {
    if ($("log-pause").checked) return;
    const li = document.createElement("li");
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = new Date().toLocaleTimeString();
    const k = document.createElement("span");
    k.className = "k k-" + kind;
    k.textContent = kind;
    li.append(t, k, document.createTextNode(text));
    const log = $("log");
    log.prepend(li);
    while (log.childElementCount > LOG_LIMIT) log.lastElementChild.remove();
  }
  $("log-clear").addEventListener("click", () => $("log").replaceChildren());

  showTab(initialTab);

  // Общие функции для control.js.
  window.plcApi = {
    API,
    token: currentToken,
    storageGet,
    storageSet,
    refreshHealth,
    onTab: (fn) => tabListeners.push(fn),
    onTokenChange: (fn) => tokenListeners.push(fn),
    showTab,
    initialTab,
  };
})();
