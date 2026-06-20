// src/js/app.js
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

import { log } from './logger.js';
import { drawXYPlot, drawLinearAxis } from './charts.js';
import { pointsQueue, queueActive, currentStepIndex, handleStepArrival, restoreSession } from './trajectory.js';

import './plc.js';

const gid = (id) => document.getElementById(id);

export let isConnected = false;
export let lastX = 0; 
export let lastY = 0; 
export let lastZ = 0;
export let isMonitorExpanded = false; 
// МОДИФИЦИРОВАНО: Предустановленные границы отображения шкал, включая ось Z
export let LIMITS = { xMin: -50.0, xMax: 500.0, yMin: -50.0, yMax: 500.0, zMin: -10.0, zMax: 200.0 };

// МОДИФИЦИРОВАНО: Математический хелпер перевода больших уставки/координат в научную нотацию
export function formatValue(val) {
  if (Math.abs(val) >= 10000 || (Math.abs(val) < 0.01 && val !== 0)) {
    return val.toExponential(2); // Вывод в формате степеней: e.g. 1.25e+4
  }
  return val.toFixed(2);
}

export function switchTab(num) {
  try {
    log(`Переключение на Вкладку №${num}`);
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    
    document.getElementById('tab' + num).classList.add('active');
    document.getElementById('content' + num).classList.add('active');
    refreshCanvases(lastX, lastY, lastZ);
  } catch (err) {
    log(`Сбой интерфейса вкладок: ${err}`);
  }
}

// МОДИФИЦИРОВАНО: Сборка и применение конфигурации XYZ границ сетки отображения из HMI
export function applyGridSettings() {
  log("Нажата кнопка: Применить настройки сетки. Обновление лимитов...");
  LIMITS.xMin = parseFloat(document.getElementById('cfg_x_min').value);
  LIMITS.xMax = parseFloat(document.getElementById('cfg_x_max').value);
  LIMITS.yMin = parseFloat(document.getElementById('cfg_y_min').value);
  LIMITS.yMax = parseFloat(document.getElementById('cfg_y_max').value);
  LIMITS.zMin = parseFloat(document.getElementById('cfg_z_min').value);
  LIMITS.zMax = parseFloat(document.getElementById('cfg_z_max').value);
  refreshCanvases(lastX, lastY, lastZ);
  log(`Границы сетки шкал обновлены: X[${LIMITS.xMin}..${LIMITS.xMax}], Y[${LIMITS.yMin}..${LIMITS.yMax}], Z[${LIMITS.zMin}..${LIMITS.zMax}]`);
}

export function refreshCanvases(x, y, z) {
  try {
    const isTab1Active = document.getElementById('content1').classList.contains('active');
    const isTab2Active = document.getElementById('content2').classList.contains('active');

    if (isTab1Active) drawXYPlot(x, y, LIMITS);
    if (isTab2Active) {
      drawLinearAxis('xLinearCanvas', x, "#0078d4", LIMITS);
      drawLinearAxis('yLinearCanvas', y, "#107c41", LIMITS);
      drawLinearAxis('zLinearCanvas', z, "#7a24db", LIMITS);
    }
  } catch (err) {}
}

export function toggleMonitorExpansion() {
  const trajCard = document.querySelector('.card-trajectory');
  const canvas = document.getElementById('xyPlotCanvas');
  const btn = document.getElementById('btnExpandMonitor');
  isMonitorExpanded = !isMonitorExpanded;
  if (isMonitorExpanded) {
    trajCard.classList.add('compact'); canvas.height = 430; btn.innerText = "Свернуть";
  } else {
    trajCard.classList.remove('compact'); canvas.height = 280; btn.innerText = "Развернуть";
  }
  refreshCanvases(lastX, lastY, lastZ);
}

export async function toggleConnect() {
  // Состояние кнопки/флага ведётся событием 'connection-status' из бэкенда —
  // здесь только выдаём команду подключения/отключения.
  if (!isConnected) {
    let ip = gid('cfg_plc_ip').value;
    let port = parseInt(gid('cfg_plc_port').value);
    let intervalMs = parseInt(gid('cfg_poll_ms').value);
    log(`Старт Modbus TCP сессии на ${ip}:${port}...`);
    try {
      let res = await invoke('connect_plc', { ip, port, intervalMs });
      log(res);
    } catch (err) { log("Ошибка соединения: " + err); }
  } else {
    try { await invoke('disconnect_plc'); } catch (err) { log("Ошибка отключения: " + err); }
    log("Запрошено закрытие сессии.");
  }
}

// Единый приёмник статуса связи (включая авто-переподключение после обрыва/VPN).
listen('connection-status', (event) => {
  const { status, message } = event.payload;
  const btn = gid('btnConnect');
  if (status === 'connected') {
    isConnected = true;
    if (btn) { btn.innerText = "Связь: ОК"; btn.className = "btn-success"; }
  } else if (status === 'reconnecting') {
    isConnected = false;
    if (btn) { btn.innerText = "Переподключение…"; btn.className = "btn-warn"; }
  } else { // disconnected
    isConnected = false;
    if (btn) { btn.innerText = "Связь: Отсутствует"; btn.className = "btn-danger"; }
  }
  if (message) log(`[Связь] ${message}`);

  // После восстановления связи — продолжаем прерванный сценарий, если он был.
  if (status === 'connected' && window.__pendingResume && typeof window.__tryResumeSession === 'function') {
    window.__tryResumeSession();
  }
});

// --- Активные оси (подключённые сервоприводы) ---
function setActiveAxesUI(axes) {
  const list = Array.isArray(axes) && axes.length ? axes.map(Number) : [1, 2, 3];
  window.activeAxes = list;
  if (gid('cfg_axis_x')) gid('cfg_axis_x').checked = list.includes(1);
  if (gid('cfg_axis_y')) gid('cfg_axis_y').checked = list.includes(2);
  if (gid('cfg_axis_z')) gid('cfg_axis_z').checked = list.includes(3);
}

function readActiveAxesUI() {
  const axes = [];
  if (gid('cfg_axis_x') && gid('cfg_axis_x').checked) axes.push(1);
  if (gid('cfg_axis_y') && gid('cfg_axis_y').checked) axes.push(2);
  if (gid('cfg_axis_z') && gid('cfg_axis_z').checked) axes.push(3);
  return axes.length ? axes : [1, 2, 3];
}

// --- Персистентность настроек (app_config.json рядом с приложением) ---
async function loadAppConfig() {
  try {
    const str = await invoke('load_app_config');
    const cfg = JSON.parse(str || "{}");

    if (cfg.plc_ip !== undefined) gid('cfg_plc_ip').value = cfg.plc_ip;
    if (cfg.plc_port !== undefined) gid('cfg_plc_port').value = cfg.plc_port;
    if (cfg.poll_interval_ms !== undefined) gid('cfg_poll_ms').value = cfg.poll_interval_ms;
    if (cfg.scpi_ip !== undefined) gid('cfg_scpi_ip').value = cfg.scpi_ip;
    if (cfg.scpi_port !== undefined) gid('cfg_scpi_port').value = cfg.scpi_port;

    if (cfg.limits) {
      const L = cfg.limits;
      if (L.x_min !== undefined) gid('cfg_x_min').value = L.x_min;
      if (L.x_max !== undefined) gid('cfg_x_max').value = L.x_max;
      if (L.y_min !== undefined) gid('cfg_y_min').value = L.y_min;
      if (L.y_max !== undefined) gid('cfg_y_max').value = L.y_max;
      if (L.z_min !== undefined) gid('cfg_z_min').value = L.z_min;
      if (L.z_max !== undefined) gid('cfg_z_max').value = L.z_max;
      applyGridSettings();
    }

    setActiveAxesUI(cfg.active_axes);

    const autoConnect = !!cfg.auto_connect;
    if (gid('cfg_auto_connect')) gid('cfg_auto_connect').checked = autoConnect;

    log("Настройки загружены из app_config.json.");

    if (autoConnect) {
      log("Автоподключение при запуске активно — устанавливаю связь...");
      setTimeout(() => { if (!isConnected) toggleConnect(); }, 300);
    }
  } catch (e) {
    log("Не удалось загрузить настройки: " + e);
    window.activeAxes = [1, 2, 3];
  }
}

export async function saveAppConfig() {
  const cfg = {
    plc_ip: gid('cfg_plc_ip').value,
    plc_port: parseInt(gid('cfg_plc_port').value),
    poll_interval_ms: parseInt(gid('cfg_poll_ms').value),
    scpi_ip: gid('cfg_scpi_ip').value,
    scpi_port: parseInt(gid('cfg_scpi_port').value),
    auto_connect: gid('cfg_auto_connect') ? gid('cfg_auto_connect').checked : false,
    active_axes: readActiveAxesUI(),
    limits: {
      x_min: parseFloat(gid('cfg_x_min').value), x_max: parseFloat(gid('cfg_x_max').value),
      y_min: parseFloat(gid('cfg_y_min').value), y_max: parseFloat(gid('cfg_y_max').value),
      z_min: parseFloat(gid('cfg_z_min').value), z_max: parseFloat(gid('cfg_z_max').value),
    },
  };
  try {
    await invoke('save_app_config', { content: JSON.stringify(cfg, null, 2) });
    log("Настройки сохранены в app_config.json.");
  } catch (e) { log("Ошибка сохранения настроек: " + e); }
  window.activeAxes = cfg.active_axes;
  applyGridSettings();
}
window.saveAppConfig = saveAppConfig;

// Прием высокоскоростной телеметрии из ПЛК
listen('telemetry-update', (event) => {
  try {
    const t = event.payload; 
    lastX = t.x_pos; lastY = t.y_pos; lastZ = t.z_pos;
    
    // МОДИФИЦИРОВАНО: Все выводы телеметрии пропущены через маску форматирования formatValue
    document.getElementById('mainRealStats').innerText = `X: ${formatValue(t.x_pos)} | Y: ${formatValue(t.y_pos)} | Z: ${formatValue(t.z_pos)}`;
    
    let xStats = document.getElementById('xRealStats');
    if (xStats) {
      document.getElementById('xRealStats').innerText = `Реальные: ${formatValue(t.x_pos)} мм`;
      document.getElementById('xRawPos').innerText = `D210: [${t.x_pos_raw.join(', ')}]`;
      document.getElementById('yRealStats').innerText = `Реальные: ${formatValue(t.y_pos)} мм`;
      document.getElementById('yRawPos').innerText = `D220: [${t.y_pos_raw.join(', ')}]`;
      document.getElementById('zRealStats').innerText = `Реальные: ${formatValue(t.z_pos)} мм`;
      document.getElementById('zRawPos').innerText = `D240: [${t.z_pos_raw.join(', ')}]`;
    }
    
    refreshCanvases(t.x_pos, t.y_pos, t.z_pos);

    if (t.inputs && t.inputs.length >= 4) {
      const inputColors = ["#107c41", "#107c41", "#0078d4", "#d83b01"]; 
      for (let i = 0; i < 4; i++) {
        let led = document.getElementById(`led_x${i}`);
        if (led) {
          led.style.backgroundColor = t.inputs[i] ? inputColors[i] : "#ccc";
          led.style.boxShadow = t.inputs[i] ? `0 0 8px ${inputColors[i]}` : "none";
        }
      }
    }

    if (t.outputs && t.outputs.length >= 3) {
      const outputColors = ["#d83b01", "#107c41", "#7a24db"]; 
      for (let i = 0; i < 3; i++) {
        let led = document.getElementById(`led_y${i}`);
        if (led) {
          led.style.backgroundColor = t.outputs[i] ? outputColors[i] : "#ccc";
          led.style.boxShadow = t.outputs[i] ? `0 0 8px ${outputColors[i]}` : "none";
        }
      }
    }

    // Поосный прецизионный контроль доезда до целевой точки
    if (queueActive && currentStepIndex >= 0 && currentStepIndex < pointsQueue.length) {
      const step = pointsQueue[currentStepIndex];
      if (step.type === 'move' || step.type === 'move_pulse') {
        let tolerance = 0.4; 
        let arrived = false;

        if (step.axis === 1) arrived = Math.abs(t.x_pos - step.pos) < tolerance;
        else if (step.axis === 2) arrived = Math.abs(t.y_pos - step.pos) < tolerance;
        else if (step.axis === 3) arrived = Math.abs(t.z_pos - step.pos) < tolerance;
        
        if (arrived) {
          handleStepArrival(); 
        }
      }
    }
  } catch (err) { console.error(err); }
});

window.addEventListener("resize", () => { refreshCanvases(lastX, lastY, lastZ); });
window.addEventListener("DOMContentLoaded", async () => {
  refreshCanvases(0, 0, 0);
  await loadAppConfig();      // настройки + авто-подключение
  await restoreSession();     // докат прерванного сценария после краша
});
window.switchTab = switchTab; window.toggleMonitorExpansion = toggleMonitorExpansion; 
window.toggleConnect = toggleConnect; window.applyGridSettings = applyGridSettings; // Регистрация метода