// src/js/trajectory.js
const { invoke } = window.__TAURI__.core;
import { log } from './logger.js';

// Массив полиморфных шагов скрипта: может содержать move, delay, scpi, pulse
export let pointsQueue = [];
export let queueActive = false;
export let queuePaused = false;
export let currentStepIndex = -1;

// Пауза между авто-Servo ON и первым шагом, чтобы приводы успели запитаться
// (как в Python-панели). Без неё первая команда MOVE уходит раньше готовности
// серво и ПЛК её игнорирует — приходилось жать «Запуск» дважды.
const SERVO_ENABLE_DELAY_MS = 500;

// --- Активные оси (какие сервоприводы физически подключены) ---
// Источник истины — window.activeAxes (задаётся из настроек в app.js).
function getActiveAxes() {
  return Array.isArray(window.activeAxes) && window.activeAxes.length ? window.activeAxes : [1, 2, 3];
}

// --- Сохранение прогресса сценария на диск (crash-resume) ---
// Снимок пишется при старте и на каждой границе шага; чистится при штатном
// завершении/останове. Так файл переживает только реальное внезапное падение.
async function persistSession() {
  if (!queueActive) return;
  try {
    await invoke('save_scan_session', {
      content: JSON.stringify({ running: true, index: currentStepIndex, steps: pointsQueue })
    });
  } catch (e) { /* запись прогресса — удобство, не критично */ }
}

async function clearSession() {
  try { await invoke('clear_scan_session'); } catch (e) {}
}

// Восстановление прерванного сценария при запуске программы. Загружает шаги в
// очередь и помечает к возобновлению — фактический запуск произойдёт после
// установки связи (см. обработчик connection-status в app.js).
export async function restoreSession() {
  try {
    const str = await invoke('load_scan_session');
    if (!str) return;
    const data = JSON.parse(str);
    if (!data || !data.running || !Array.isArray(data.steps) || data.steps.length === 0) return;

    pointsQueue = data.steps.map(p => ({ ...p, checked: false }));
    window.pointsQueue = pointsQueue;
    const idx = Math.max(0, Math.min(parseInt(data.index) || 0, pointsQueue.length - 1));
    updateQueueUi();

    window.__resumeIndex = idx;
    window.__pendingResume = true;
    log(`⚠ Обнаружен прерванный сценарий (${pointsQueue.length} шагов). Продолжение с шага №${idx + 1} после установки связи.`);
  } catch (e) { /* нет валидной сессии — нечего восстанавливать */ }
}

// Возобновление прерванного сканирования (вызывается из app.js по событию
// connection-status='connected', если есть отложенное восстановление).
window.__tryResumeSession = function () {
  if (!window.__pendingResume) return;
  window.__pendingResume = false;
  const idx = window.__resumeIndex || 0;
  log(`▶ Возобновление прерванного сканирования с шага №${idx + 1}`);
  const axes = getActiveAxes();
  currentStepIndex = idx;
  queueActive = true;
  queuePaused = false;
  updatePauseBtn();
  (async () => {
    try { await invoke('enable_all_motors', { axes }); } catch (e) {}
    // Та же задержка готовности приводов, что и при обычном старте.
    setTimeout(() => { if (queueActive) executeCurrentStep(); }, SERVO_ENABLE_DELAY_MS);
  })();
};

// Внутренний изолированный буфер для отслеживания виртуального индекса перетаскивания
let draggedIdx = null;

export function toggleStepInputs() {
  const type = document.getElementById('stepTypeSelect').value;
  document.getElementById('input_group_move').style.display = (type === 'move' || type === 'move_pulse') ? 'inline' : 'none';
  document.getElementById('input_group_delay').style.display = type === 'delay' ? 'inline' : 'none';
  document.getElementById('input_group_scpi').style.display = type === 'scpi' ? 'inline' : 'none';
  document.getElementById('input_group_pulse').style.display = type === 'pulse' ? 'inline' : 'none';
}

export function addPolymorphicStep() {
  const type = document.getElementById('stepTypeSelect').value;
  let step = { type: type, checked: false };

  if (type === 'move' || type === 'move_pulse') {
    step.axis = parseInt(document.getElementById('q_axis_select').value);
    step.pos = parseFloat(document.getElementById('q_coord').value);
    step.spd = parseFloat(document.getElementById('q_speed').value);
    
    let aName = step.axis === 1 ? 'X' : (step.axis === 2 ? 'Y' : 'Z');
    let pInfo = type === 'move_pulse' ? "с авто-импульсом замера" : "БЕЗ импульса";
    log(`Добавлен шаг сценария: Ось ${aName} -> координата ${step.pos} мм [${pInfo}]`);
  } else if (type === 'delay') {
    step.ms = parseInt(document.getElementById('q_delay').value);
    log(`Добавлен шаг задержки: ${step.ms} мс`);
  } else if (type === 'scpi') {
    step.cmd = document.getElementById('q_scpi').value;
    log(`Добавлен шаг SCPI: "${step.cmd}"`);
  } else if (type === 'pulse') {
    log(`Добавлен шаг триггерного импульса Y0`);
  }

  pointsQueue.push(step);
  updateQueueUi();
}

// Полевое обновление данных без полной перерисовки (курсор не слетает во время ввода текста/цифр)
window.updateStepField = (idx, field, value) => {
  if (!pointsQueue[idx]) return;
  if (field === 'axis' || field === 'ms') {
    pointsQueue[idx][field] = parseInt(value);
  } else if (field === 'pos' || field === 'spd') {
    pointsQueue[idx][field] = parseFloat(value);
  } else {
    pointsQueue[idx][field] = value;
  }
};

// Эксклюзивный выбор чекбокса (как Радио-кнопка) для указания конкретной точки старта
window.handleStepCheck = (idx, checked) => {
  pointsQueue.forEach((p, i) => {
    p.checked = (i === idx) ? checked : false;
  });
  updateQueueUi();
};

// Вспомогательный генератор HTML-кода внутренних полей управления шага
function getStepControlsHtml(p, idx) {
  if (p.type === 'move' || p.type === 'move_pulse') {
    let modeBadge = p.type === 'move_pulse' 
      ? `<span style="font-size:10px; color:#d83b01; font-weight:bold; margin-left:3px;">+⚡ Импульс</span>`
      : `<span style="font-size:10px; color:#666; font-weight:bold; margin-left:3px;">Без имп.</span>`;

    return `
      🌐 Ось: 
      <select style="padding:1px; font-size:11px;" onchange="window.updateStepField(${idx}, 'axis', this.value)">
        <option value="1" ${p.axis === 1 ? 'selected' : ''}>X</option>
        <option value="2" ${p.axis === 2 ? 'selected' : ''}>Y</option>
        <option value="3" ${p.axis === 3 ? 'selected' : ''}>Z</option>
      </select>
      Поз: <input type="number" step="0.1" style="width:45px; font-size:11px; padding:1px;" value="${p.pos}" oninput="window.updateStepField(${idx}, 'pos', this.value)">
      V: <input type="number" step="0.1" style="width:40px; font-size:11px; padding:1px;" value="${p.spd}" oninput="window.updateStepField(${idx}, 'spd', this.value)">
      ${modeBadge}
    `;
  } else if (p.type === 'delay') {
    return `⏱️ Пауза: <input type="number" style="width:55px; font-size:11px; padding:1px;" value="${p.ms}" oninput="window.updateStepField(${idx}, 'ms', this.value)"> мс`;
  } else if (p.type === 'scpi') {
    return `🔌 SCPI: <input type="text" style="width:130px; font-size:11px; padding:1px;" value="${p.cmd}" oninput="window.updateStepField(${idx}, 'cmd', this.value)">`;
  } else if (p.type === 'pulse') {
    return `⚡ Аппаратный импульс (Выход Y0)`;
  }
  return "";
}

// Вспомогательное обновление контента конкретного статического слота без разрушения его DOM-узла
function updateRowContent(rowElement, idx) {
  const p = pointsQueue[idx];
  if (!p) return;
  
  const chk = rowElement.querySelector('.chk-inline');
  if (chk) chk.checked = p.checked;
  
  const controlsContainer = rowElement.querySelector('.controls-container');
  if (controlsContainer) controlsContainer.innerHTML = getStepControlsHtml(p, idx);
  
  if (idx === currentStepIndex && queueActive) {
    rowElement.classList.add('current-active-step');
  } else {
    rowElement.classList.remove('current-active-step');
  }
}

// НОВЫЙ ПОДХОД: Стабильный координатный Pointer Drag без репарентинга DOM-узлов
export function initPointerDrag(e, index, handleElement) {
  draggedIdx = index;
  
  // Аппаратно блокируем указатель на рукоятке. Теперь сессия 100% непрерывна
  handleElement.setPointerCapture(e.pointerId);
  log(`[POINTER DOWN] Рукоятка ☰ Слота №${index + 1} захвачена. Активирован виртуальный координатный Drag.`);

  const container = document.getElementById('queueContainer');
  const rows = Array.from(container.children);
  
  // Визуально подсвечиваем текущий переносимый слот
  rows[draggedIdx].style.background = "#fff2e6";
  rows[draggedIdx].style.border = "1px solid #d83b01";

  const onPointerMove = (moveEvent) => {
    if (draggedIdx === null) return;

    // Ищем, над границами какого физического слота сейчас зависла мышь оператора
    let targetIndex = null;
    for (let i = 0; i < rows.length; i++) {
      const rect = rows[i].getBoundingClientRect();
      if (moveEvent.clientY >= rect.top && moveEvent.clientY <= rect.bottom) {
        targetIndex = i;
        break;
      }
    }

    // Если мышь зашла на площадь другого слота — делаем мгновенную рокировку данных
    if (targetIndex !== null && targetIndex !== draggedIdx) {
      log(`[POINTER MOVE] Свободный обмен позиций: Шаг №${draggedIdx + 1} ↔ Шаг №${targetIndex + 1}`);

      // 1. Свапаем ячейки данных внутри массива в ОЗУ
      const temp = pointsQueue[draggedIdx];
      pointsQueue[draggedIdx] = pointsQueue[targetIndex];
      pointsQueue[targetIndex] = temp;

      // Корректируем индекс активного шага, если в фоне шло автоматическое сканирование
      if (queueActive) {
        if (currentStepIndex === draggedIdx) currentStepIndex = targetIndex;
        else if (draggedIdx < currentStepIndex && targetIndex >= currentStepIndex) currentStepIndex--;
        else if (draggedIdx > currentStepIndex && targetIndex <= currentStepIndex) currentStepIndex++;
      }

      // 2. Обновляем UI полей ввода для обоих слотов. Сами DOM-узлы остаются неподвижны!
      updateRowContent(rows[draggedIdx], draggedIdx);
      updateRowContent(rows[targetIndex], targetIndex);

      // 3. Переносим визуальные стили захвата на новую физическую строчку
      rows[draggedIdx].style.background = "";
      rows[draggedIdx].style.border = "";
      
      rows[targetIndex].style.background = "#fff2e6";
      rows[targetIndex].style.border = "1px solid #d83b01";

      // Смещаем рабочий индекс источника
      draggedIdx = targetIndex;
    }
  };

  const onPointerUp = (upEvent) => {
    log(`[POINTER UP] Рукоятка освобождена. Pointer Drag успешно завершен.`);
    
    // Освобождаем мышь и удаляем слушатели
    handleElement.releasePointerCapture(upEvent.pointerId);
    handleElement.removeEventListener('pointermove', onPointerMove);
    handleElement.removeEventListener('pointerup', onPointerUp);
    
    draggedIdx = null;
    
    // Чистая финальная перерисовка UI (для обновления жестких инлайн-индексов в кнопках удаления)
    updateQueueUi();
  };

  handleElement.addEventListener('pointermove', onPointerMove);
  handleElement.addEventListener('pointerup', onPointerUp);
}

export function updateQueueUi() {
  let container = document.getElementById('queueContainer'); 
  if (!container) return;
  container.innerHTML = "";

  pointsQueue.forEach((p, idx) => {
    let row = document.createElement('div'); 
    row.className = 'queue-row'; 
    row.setAttribute('data-index', idx);
    if (idx === currentStepIndex && queueActive) { row.classList.add('current-active-step'); }
    
    // touch-action: none аппаратно блокирует системный скролл Windows при зажатии элемента
    row.innerHTML = `
      <input type="checkbox" class="chk-inline" ${p.checked ? 'checked' : ''} onchange="window.handleStepCheck(${idx}, this.checked)">
      <span class="drag-handle" style="cursor: grab; padding: 2px 8px; background: #eaeaea; border: 1px solid #ccc; border-radius: 3px; margin: 0 4px; user-select: none; font-weight: bold; font-size: 12px; color: #555; touch-action: none;">☰</span>
      <span class="step-title" style="font-size:11px; min-width:40px;">№ ${String(idx+1).padStart(2, '0')}:</span>
      <div class="controls-container" style="flex:1; display:flex; gap:4px; align-items:center;">
        ${getStepControlsHtml(p, idx)}
      </div>
      <button class="btn-danger" style="padding: 2px 5px; font-size: 11px; margin-left:5px;" onclick="deletePointInline(${idx})">🗑️</button>
    `;

    const handle = row.querySelector('.drag-handle');
    handle.addEventListener('pointerdown', (e) => initPointerDrag(e, idx, handle));

    container.appendChild(row);
  });

  // Автоскролл: подтягиваем активный шаг в зону видимости контейнера очереди.
  if (queueActive && currentStepIndex >= 0 && currentStepIndex < container.children.length) {
    const activeRow = container.children[currentStepIndex];
    if (activeRow) activeRow.scrollIntoView({ block: 'nearest' });
  }
}

// ОСТАЛЬНАЯ ЛОГИКА ДИСПЕТЧЕРА СЦЕНАРИЕВ
export async function startQueue() {
  if(pointsQueue.length === 0) { log("Ошибка: Сценарий пуст!"); return; }

  const axes = getActiveAxes();
  log(`Авто-подготовка: включение силовых контуров активных осей [${axes.join(', ')}] (Servo ON)...`);
  try {
    // Дожидаемся фактического включения приводов, прежде чем запускать шаги.
    await invoke('enable_all_motors', { axes });
    log("Силовые контуры активных осей заблокированы.");
  } catch(e) {
    log("Внимание, ошибка авто-включения приводов: " + e);
  }

  const checkedIdx = pointsQueue.findIndex(p => p.checked === true);
  if (checkedIdx !== -1) {
    currentStepIndex = checkedIdx;
    log(`Запуск сценария со строчки №${checkedIdx + 1}`);
  } else {
    currentStepIndex = 0;
    log("Точка старта не выбрана. Выполнение начнется с Шага №1");
  }
  queueActive = true;
  queuePaused = false;
  updatePauseBtn();
  persistSession();

  // Откладываем первый шаг, чтобы приводы успели выйти в готовность (одно нажатие).
  log(`Старт через ${SERVO_ENABLE_DELAY_MS} мс после Servo ON...`);
  setTimeout(() => { if (queueActive) executeCurrentStep(); }, SERVO_ENABLE_DELAY_MS);
}

export function stopQueue() {
  queueActive = false;
  queuePaused = false;
  currentStepIndex = -1;
  updatePauseBtn();
  updateQueueUi();
  clearSession();
  log("Автоматическое выполнение скрипта прервано оператором.");
}

// --- Пауза / возобновление сценария ---
// Пауза вступает в силу на ближайшей границе шага: текущее движение
// доводится до конца, после чего выполнение паркуется до возобновления.
export function pauseQueue() {
  if (queueActive && !queuePaused) {
    queuePaused = true;
    updatePauseBtn();
    log("⏸ Сканирование на паузе (вступит в силу на границе текущего шага).");
  }
}

export function resumeQueue() {
  if (queueActive && queuePaused) {
    queuePaused = false;
    updatePauseBtn();
    log("▶ Возобновление сканирования.");
    executeCurrentStep();
  }
}

export function togglePause() {
  if (!queueActive) { log("Сценарий не запущен — пауза недоступна."); return; }
  if (queuePaused) resumeQueue(); else pauseQueue();
}

function updatePauseBtn() {
  const b = document.getElementById('btnPauseQueue');
  if (!b) return;
  if (queueActive && queuePaused) { b.innerText = "ПРОДОЛЖИТЬ"; b.className = "btn-success"; }
  else { b.innerText = "ПАУЗА"; b.className = "btn-warn"; }
}

export async function executeCurrentStep() {
  if (!queueActive) return;

  if (currentStepIndex >= pointsQueue.length) {
    queueActive = false;
    queuePaused = false;
    currentStepIndex = -1;
    updatePauseBtn();
    updateQueueUi();
    clearSession();
    log("🎉 Сценарий сканирования выполнен в полном объеме!");
    return;
  }

  updateQueueUi();
  persistSession();
  const step = pointsQueue[currentStepIndex];

  // Пропуск движений по неподключённым осям: иначе сценарий навсегда зависнет
  // в ожидании доезда, которого не будет (см. active_axes).
  if (step.type === 'move' || step.type === 'move_pulse') {
    const axes = getActiveAxes();
    if (!axes.includes(step.axis)) {
      const aName = step.axis === 1 ? 'X' : (step.axis === 2 ? 'Y' : 'Z');
      log(`Шаг №${currentStepIndex + 1}: ось ${aName} не подключена (active_axes) — пропуск.`);
      advanceStep();
      return;
    }
  }

  switch(step.type) {
    case 'move':
      try {
        await invoke('send_move_abs', { axis: step.axis, pos: step.pos, spd: step.spd, autoPulse: false });
      } catch(e) { 
        log(`Критический сбой движения оси ${step.axis}: ` + e); 
        stopQueue(); 
      }
      break;

    case 'move_pulse':
      try {
        await invoke('send_move_abs', { axis: step.axis, pos: step.pos, spd: step.spd, autoPulse: true });
      } catch(e) { 
        log(`Критический сбой движения оси ${step.axis}: ` + e); 
        stopQueue(); 
      }
      break;

    case 'delay':
      setTimeout(() => { advanceStep(); }, step.ms);
      break;

    case 'scpi':
      const ip = document.getElementById('cfg_scpi_ip').value;
      const port = parseInt(document.getElementById('cfg_scpi_port').value);
      try {
        let res = await invoke('send_scpi_command', { cmd: step.cmd, ip, port });
        log(res);
        advanceStep();
      } catch(err) {
        log(`[SCPI ERROR]: ${err}. Аварийная остановка.`);
        stopQueue();
      }
      break;

    case 'pulse':
      try {
        let res = await invoke('send_pulse_trigger');
        log(res);
        advanceStep();
      } catch(err) {
        log("Ошибка формирования импульса: " + err);
        stopQueue();
      }
      break;
  }
}

export async function handleStepArrival() {
  if (!queueActive) return;
  const step = pointsQueue[currentStepIndex];
  
  if (step && step.type === 'move_pulse') {
    log(`[Аппаратный замер]: Точка достигнута. Контроллер отрабатывает физический импульс...`);
    setTimeout(() => {
      advanceStep();
    }, 200);
  } else {
    advanceStep();
  }
}

export function advanceStep() {
  if (!queueActive) return;
  currentStepIndex++;
  // Если запрошена пауза — паркуемся на этой границе шага до возобновления.
  if (queuePaused) {
    updateQueueUi();
    persistSession();
    log("⏸ Пауза: ожидание возобновления оператором...");
    return;
  }
  executeCurrentStep();
}

async function exportJsonSequence() {
  if (pointsQueue.length === 0) { log("Очередь пуста."); return; }
  try {
    let res = await invoke('save_sequence_json', { content: JSON.stringify(pointsQueue, null, 2) });
    log(res);
  } catch(e) { log("Ошибка сохранения JSON: " + e); }
}

async function importJsonSequence() {
  try {
    let jsonStr = await invoke('load_sequence_json');
    let data = JSON.parse(jsonStr);
    if (Array.isArray(data)) {
      pointsQueue = data.map(p => ({ ...p, checked: false }));
      currentStepIndex = -1;
      updateQueueUi();
      log(`Загружен сценарий: ${pointsQueue.length} шагов.`);
    }
  } catch(e) { log("Импорт отменен/ошибка: " + e); }
}

export function deletePointInline(index) {
  pointsQueue.splice(index, 1);
  if (index <= currentStepIndex) currentStepIndex--;
  updateQueueUi();
}

export function clearSelectedPoints() {
  pointsQueue = [];
  currentStepIndex = -1;
  updateQueueUi();
  log("Сценарий полностью очищен.");
}

window.pointsQueue = pointsQueue;
window.toggleStepInputs = toggleStepInputs; window.addPolymorphicStep = addPolymorphicStep;
window.importJsonSequence = importJsonSequence; window.exportJsonSequence = exportJsonSequence;
window.deletePointInline = deletePointInline; window.clearSelectedPoints = clearSelectedPoints;
window.startQueue = startQueue; window.stopQueue = stopQueue;
window.pauseQueue = pauseQueue; window.resumeQueue = resumeQueue; window.togglePause = togglePause;