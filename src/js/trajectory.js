// src/js/trajectory.js
const { invoke } = window.__TAURI__.core;
import { log } from './logger.js';

// Массив полиморфных шагов скрипта: может содержать move, delay, scpi, pulse
export let pointsQueue = []; 
export let queueActive = false;
export let currentStepIndex = -1;

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
}

// ОСТАЛЬНАЯ ЛОГИКА ДИСПЕТЧЕРА СЦЕНАРИЕВ (БЕЗ ИЗМЕНЕНИЙ)
export function startQueue() { 
  if(pointsQueue.length === 0) { log("Ошибка: Сценарий пуст!"); return; }
  
  log("Авто-подготовка: Включение силовых контуров всех приводов (Servo ON)...");
  try {
    invoke('send_power', { axis: 1, status: true });
    invoke('send_power', { axis: 2, status: true });
    invoke('send_power', { axis: 3, status: true });
    log("Силовые контуры осей X, Y, Z успешно заблокированы.");
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
  executeCurrentStep();
}

export function stopQueue() { 
  queueActive = false; 
  currentStepIndex = -1; 
  updateQueueUi();
  log("Автоматическое выполнение скрипта прервано оператором.");
}

export async function executeCurrentStep() {
  if (!queueActive) return;
  
  if (currentStepIndex >= pointsQueue.length) {
    queueActive = false;
    currentStepIndex = -1;
    updateQueueUi();
    log("🎉 Сценарий сканирования выполнен в полном объеме!");
    return;
  }

  updateQueueUi();
  const step = pointsQueue[currentStepIndex];

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