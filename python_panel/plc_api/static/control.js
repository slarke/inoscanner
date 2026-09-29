// Вкладка «Пульт»: карточки осей с кнопками команд и живой телеметрией.
// Команды — REST, состояние — собственное WebSocket-соединение вкладки.
(function () {
  "use strict";

  const P = window.plcApi;
  const $ = (id) => document.getElementById(id);
  const LOG_LIMIT = 100;
  const JOG_STEPS = [0.1, 1, 10, 100];

  let caps = null;              // /capabilities
  let ws = null;
  let reconnectTimer = null;
  let linkUp = false;           // связь сервера с ПЛК
  const cards = {};             // имя оси -> {el, state, busy}

  // ------------------------------------------------------------------ //
  // REST
  // ------------------------------------------------------------------ //
  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  async function api(method, path, body) {
    const headers = { "Content-Type": "application/json" };
    const token = P.token();
    if (token) headers.Authorization = "Bearer " + token;
    let r;
    try {
      r = await fetch(P.API + path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      throw new ApiError(0, "network", "сервер недоступен");
    }
    let data = null;
    try { data = await r.json(); } catch (e) { /* пустое тело */ }
    if (!r.ok) {
      const err = (data && data.error) || {};
      throw new ApiError(r.status, err.code || "http_" + r.status,
                         err.message || r.statusText);
    }
    return data;
  }

  // ------------------------------------------------------------------ //
  // Журнал и статусы
  // ------------------------------------------------------------------ //
  function log(kind, text) {
    const li = document.createElement("li");
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = new Date().toLocaleTimeString();
    const k = document.createElement("span");
    k.className = "k k-" + kind;
    k.textContent = kind;
    li.append(t, k, document.createTextNode(text));
    const list = $("ctl-log");
    list.prepend(li);
    while (list.childElementCount > LOG_LIMIT) list.lastElementChild.remove();
  }
  $("ctl-log-clear").addEventListener("click", () => $("ctl-log").replaceChildren());

  function setStatus(axis, text, cls) {
    const el = cards[axis] && cards[axis].el.querySelector(".status");
    if (!el) return;
    el.textContent = text;
    el.className = "status" + (cls ? " " + cls : "");
  }

  function message(text) {
    const m = $("ctl-message");
    m.hidden = !text;
    m.textContent = text || "";
  }

  // Выполнить команду: блокировка кнопки, статус, журнал.
  async function run(label, axis, button, method, path, body) {
    if (button) {
      button.disabled = true;
      button.dataset.busy = "1";     // refreshCard не должен включить её раньше ответа
    }
    if (axis) setStatus(axis, label + "…", "busy");
    try {
      const res = await api(method, path, body);
      const text = res && res.status === "arrived"
        ? "в точке: " + fmt(res.state.position)
        : "отправлено";
      if (axis) setStatus(axis, label + ": " + text, "ok");
      log(res && res.status === "arrived" ? "arrived" : "command",
          (axis ? axis + " " : "") + label + (body ? " " + JSON.stringify(body) : "")
          + " — " + text);
      return res;
    } catch (e) {
      const text = e.message + (e.code ? " [" + e.code + "]" : "");
      if (axis) setStatus(axis, label + ": " + text, "err");
      log("error", (axis ? axis + " " : "") + label + " — " + text);
      if (e.status === 401) message("Сервер требует токен — введите его в поле «Токен» вверху.");
      return null;
    } finally {
      if (button) {
        delete button.dataset.busy;
        button.disabled = false;
        refreshCard(axis);
      }
    }
  }

  // ------------------------------------------------------------------ //
  // Поля ввода: число из поля, запоминание между сессиями
  // ------------------------------------------------------------------ //
  function num(card, field, label) {
    const input = card.el.querySelector('[data-field="' + field + '"]');
    const v = input.value.trim() === "" ? NaN : Number(input.value);
    if (!Number.isFinite(v)) {
      input.focus();
      throw new ApiError(0, "input", "укажите " + label);
    }
    return v;
  }

  function remember(axis, input) {
    const key = "plc-ctl-" + axis + "-" + input.dataset.field;
    const saved = P.storageGet("localStorage", key);
    if (saved !== null) {
      if (input.type === "checkbox") input.checked = saved === "1";
      else input.value = saved;
    }
    input.addEventListener("change", () => {
      P.storageSet("localStorage", key,
                   input.type === "checkbox" ? (input.checked ? "1" : "0") : input.value);
    });
  }

  // ------------------------------------------------------------------ //
  // Карточки осей
  // ------------------------------------------------------------------ //
  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function fmt(v, digits) {
    return v === null || v === undefined ? "—"
      : Number(v).toFixed(digits === undefined ? 3 : digits);
  }

  function cardHtml(name, info, active, limits) {
    const lim = limits ? "пределы " + limits[0] + " … " + limits[1] + " мм" : "пределы не проверяются";
    const pulse = info.pulse_on_arrival
      ? '<label class="inline"><input type="checkbox" data-field="pulse"> импульс Y0</label>'
      : '<span class="muted small" title="В текущем MAIN.LD импульс по приезду есть только у оси X">без импульса</span>';
    const jogOptions = JOG_STEPS.map((s) => '<option value="' + s + '">' + s + " мм</option>").join("");
    return (
      '<div class="card-head">' +
        "<h2>" + esc(name) + "</h2>" +
        '<div class="readout"><span class="pos">—</span><span class="unit">мм</span>' +
        '<div class="vel muted small">v —</div></div>' +
      "</div>" +
      (active ? "" : '<p class="inactive-note">Ось не подключена (active_axes) — движение запрещено</p>') +
      '<div class="badges">' +
        '<span class="badge" data-badge="power">питание —</span>' +
        '<span class="badge" data-badge="limits">концевики —</span>' +
      "</div>" +
      '<div class="blocked-bar" hidden>Ось заблокирована концевиком ' +
        '<button data-cmd="unblock">Снять блокировку</button></div>' +
      '<div class="row">' +
        '<button data-cmd="power-on">Вкл</button>' +
        '<button data-cmd="power-off">Выкл</button>' +
        '<button data-cmd="reset" title="MC_Reset">Сброс ошибок</button>' +
        '<button data-cmd="stop" class="danger">СТОП</button>' +
      "</div>" +
      '<fieldset><legend>В точку <span class="muted small">' + esc(lim) + "</span></legend>" +
        '<label>позиция, мм<input type="number" step="any" data-field="position"></label>' +
        '<label>скорость<input type="number" step="any" min="0" value="10" data-field="abs_speed"></label>' +
        pulse +
        '<label class="inline"><input type="checkbox" data-field="wait" checked> ждать приезда</label>' +
        '<button data-cmd="move-abs" data-motion class="primary">Ехать</button>' +
      "</fieldset>" +
      "<fieldset><legend>Шаг (относительно)</legend>" +
        '<label>шаг<select data-field="jog_step">' + jogOptions + "</select></label>" +
        '<label>скорость<input type="number" step="any" min="0" value="10" data-field="jog_speed"></label>' +
        '<button data-cmd="jog-" data-motion class="jog">−</button>' +
        '<button data-cmd="jog+" data-motion class="jog">+</button>' +
      "</fieldset>" +
      "<fieldset><legend>Со скоростью (до СТОП или концевика)</legend>" +
        '<label>скорость<input type="number" step="any" min="0" value="5" data-field="vel_speed"></label>' +
        '<label>разгон<input type="number" step="any" min="0" value="20" data-field="vel_acc"></label>' +
        '<label>торможение<input type="number" step="any" min="0" value="20" data-field="vel_dec"></label>' +
        '<button data-cmd="vel-" data-motion>◀ Пуск</button>' +
        '<button data-cmd="vel+" data-motion>Пуск ▶</button>' +
      "</fieldset>" +
      "<fieldset><legend>Координата (MC_SetPosition)</legend>" +
        '<label>значение, мм<input type="number" step="any" value="0" data-field="set_value"></label>' +
        '<button data-cmd="set-pos">Установить</button>' +
        '<button data-cmd="zero">Обнулить</button>' +
      "</fieldset>" +
      '<div class="status" aria-live="polite"></div>'
    );
  }

  function buildCards() {
    const host = $("axis-cards");
    host.replaceChildren();
    const active = new Set(caps.active_axes);
    Object.entries(caps.axes).forEach(([name, info]) => {
      const el = document.createElement("div");
      el.className = "panel axis-card" + (active.has(name) ? "" : " inactive");
      el.dataset.axis = name;
      el.innerHTML = cardHtml(name, info, active.has(name),
                              caps.soft_limits ? caps.soft_limits[name] : null);
      host.appendChild(el);
      cards[name] = { el, active: active.has(name), state: null };
      el.querySelectorAll("[data-field]").forEach((i) => remember(name, i));
      el.addEventListener("click", (e) => {
        const b = e.target.closest("button[data-cmd]");
        if (b) command(name, b.dataset.cmd, b);
      });
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && e.target.dataset.field === "position") {
          command(name, "move-abs", el.querySelector('[data-cmd="move-abs"]'));
        }
      });
      refreshCard(name);
    });
  }

  function command(axis, cmd, button) {
    const card = cards[axis];
    const base = "/axes/" + axis.toLowerCase();
    try {
      switch (cmd) {
        case "power-on": return run("питание вкл", axis, button, "POST", base + "/power", { on: true });
        case "power-off": return run("питание выкл", axis, button, "POST", base + "/power", { on: false });
        case "reset": return run("сброс", axis, button, "POST", base + "/reset");
        case "stop": return run("СТОП", axis, null, "POST", base + "/stop");
        case "unblock": return run("снятие блокировки", axis, button, "POST", base + "/unblock");
        case "move-abs": {
          const body = {
            position: num(card, "position", "позицию"),
            speed: num(card, "abs_speed", "скорость"),
          };
          const pulse = card.el.querySelector('[data-field="pulse"]');
          if (pulse && pulse.checked) body.pulse = true;
          if (card.el.querySelector('[data-field="wait"]').checked) {
            body.wait = true;
            body.timeout_s = 600;
          }
          return run("в точку", axis, button, "POST", base + "/move-absolute", body);
        }
        case "jog-":
        case "jog+": {
          const step = num(card, "jog_step", "шаг");
          return run("шаг " + (cmd === "jog+" ? "+" : "−") + step, axis, button, "POST",
                     base + "/move-relative",
                     { distance: cmd === "jog+" ? step : -step,
                       speed: num(card, "jog_speed", "скорость шага") });
        }
        case "vel-":
        case "vel+": {
          const speed = Math.abs(num(card, "vel_speed", "скорость"));
          return run("пуск " + (cmd === "vel+" ? "▶" : "◀"), axis, button, "POST",
                     base + "/move-velocity",
                     { speed: cmd === "vel+" ? speed : -speed,
                       acceleration: num(card, "vel_acc", "разгон"),
                       deceleration: num(card, "vel_dec", "торможение") });
        }
        case "set-pos":
          return run("координата", axis, button, "POST", base + "/set-position",
                     { position: num(card, "set_value", "значение") });
        case "zero":
          return run("обнуление", axis, button, "POST", base + "/set-position", { position: 0 });
      }
    } catch (e) {
      setStatus(axis, e.message, "err");   // ошибка ввода — запрос не отправлен
    }
    return null;
  }

  // Кнопки движения: запрещены без связи, для неподключённой и заблокированной оси.
  function refreshCard(axis) {
    const card = cards[axis];
    if (!card) return;
    const s = card.state;
    const blocked = !!(s && s.blocked);
    card.el.classList.toggle("blocked", blocked);
    card.el.querySelector(".blocked-bar").hidden = !blocked;
    card.el.querySelectorAll("[data-motion]").forEach((b) => {
      b.disabled = b.dataset.busy === "1" || !linkUp || !card.active || blocked;
    });
  }

  function badge(card, name, text, cls) {
    const el = card.el.querySelector('[data-badge="' + name + '"]');
    el.textContent = text;
    el.className = "badge" + (cls ? " " + cls : "");
  }

  function applyTelemetry(data) {
    Object.entries(data.axes).forEach(([name, a]) => {
      const card = cards[name];
      if (!card) return;
      card.state = a;
      card.el.querySelector(".pos").textContent = fmt(a.position);
      card.el.querySelector(".vel").textContent = "v " + fmt(a.velocity, 2);
      badge(card, "power",
            a.power_command === null ? "питание —" : a.power_command ? "питание ВКЛ" : "питание выкл",
            a.power_command ? "on" : "");
      const l = a.limit_left, r = a.limit_right;
      badge(card, "limits",
            l === null && r === null ? "концевики —"
              : "концевики " + (l ? "Л" : "л") + " / " + (r ? "П" : "п"),
            l || r ? "alarm" : "");
      refreshCard(name);
    });
  }

  // ------------------------------------------------------------------ //
  // Общие кнопки
  // ------------------------------------------------------------------ //
  function stopAll() {
    const powerOff = $("stop-all-power").checked;
    return run(powerOff ? "СТОП ВСЕ + снять питание" : "СТОП ВСЕ", null, null,
               "POST", "/stop-all", { power_off: powerOff });
  }
  $("stop-all").addEventListener("click", stopAll);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("tab-control").hidden) {
      e.preventDefault();
      stopAll();
    }
  });

  document.querySelectorAll("[data-all]").forEach((b) => {
    b.addEventListener("click", async () => {
      const on = b.dataset.all === "power-on";
      b.disabled = true;
      for (const name of Object.keys(cards)) {
        if (on && !cards[name].active) continue;   // не включаем неподключённые
        await run(on ? "питание вкл" : "питание выкл", name, null, "POST",
                  "/axes/" + name.toLowerCase() + "/power", { on });
      }
      b.disabled = false;
    });
  });

  document.querySelectorAll("[data-y0]").forEach((b) => {
    b.addEventListener("click", () => {
      const kind = b.dataset.y0;
      if (kind === "pulse") return run("Y0 импульс", null, b, "POST", "/io/y0/pulse");
      return run("Y0 " + kind.toUpperCase(), null, b, "PUT", "/io/y0", { high: kind === "high" });
    });
  });

  // ------------------------------------------------------------------ //
  // Живое состояние по WebSocket (только пока вкладка открыта)
  // ------------------------------------------------------------------ //
  function setLive(ok, text) {
    const el = $("ctl-live");
    el.textContent = text;
    el.className = "pill " + (ok ? "on" : "off");
  }

  function setLink(up) {
    linkUp = up;
    Object.keys(cards).forEach(refreshCard);
  }

  function openWs() {
    if (ws || $("tab-control").hidden || !caps) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const q = new URLSearchParams({
      events: "telemetry,connection,arrived,blocked",
      telemetry_interval_ms: "100",
    });
    const token = P.token();
    if (token) q.set("token", token);
    ws = new WebSocket(proto + "//" + location.host + P.API + "/ws?" + q);
    ws.onmessage = (m) => {
      let msg;
      try { msg = JSON.parse(m.data); } catch (e) { return; }
      if (msg.type === "hello") {
        setLive(true, "телеметрия: есть");
        setLink(msg.status.connected);
        if (msg.telemetry) applyTelemetry(msg.telemetry);
      } else if (msg.type === "telemetry") {
        applyTelemetry(msg.data);
      } else if (msg.type === "connection") {
        setLink(msg.connected);
        log("connection", msg.connected ? "связь с ПЛК есть" : "нет связи: " + msg.detail);
        P.refreshHealth();
      } else if (msg.type === "arrived") {
        setStatus(msg.axis, "в точке " + msg.target + " (" + fmt(msg.position) + ")", "ok");
        log("arrived", msg.axis + " в точке " + msg.target);
      } else if (msg.type === "blocked") {
        log(msg.blocked ? "blocked" : "command",
            msg.axis + (msg.blocked ? " ЗАБЛОКИРОВАНА концевиком" : " блокировка снята"));
      }
    };
    ws.onclose = () => {
      ws = null;
      setLive(false, "телеметрия: нет");
      setLink(false);
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    if ($("tab-control").hidden) return;
    reconnectTimer = setTimeout(openWs, 2000);
  }

  function closeWs() {
    clearTimeout(reconnectTimer);
    if (ws) {
      ws.onclose = null;
      ws.close(1000);
      ws = null;
      setLive(false, "телеметрия: нет");
    }
  }

  // ------------------------------------------------------------------ //
  // Старт: возможности ладдера -> карточки -> WebSocket
  // ------------------------------------------------------------------ //
  async function init() {
    try {
      caps = await api("GET", "/capabilities");
    } catch (e) {
      message(e.status === 401
        ? "Сервер требует токен — введите его в поле «Токен» вверху."
        : "Не удалось получить /capabilities: " + e.message);
      return;
    }
    message("");
    buildCards();
    openWs();
  }

  P.onTab((name) => (name === "control" ? openWs() : closeWs()));
  P.onTokenChange(() => { closeWs(); if (caps) openWs(); else init(); });
  init();
})();
