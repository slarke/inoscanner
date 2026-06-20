// src/js/plc.js
const { invoke } = window.__TAURI__.core;
import { log } from './logger.js';

export async function sendPower(axis, state) { 
  let aName = axis === 1 ? 'X' : (axis === 2 ? 'Y' : 'Z');
  log(`Вызов: Servo Power -> ${state ? 'ON' : 'OFF'} для оси ${aName}`);
  try { await invoke('send_power', { axis, status: state }); } catch(e) { log("Ошибка Power: " + e); }
}

export async function sendReset(axis) { 
  let aName = axis === 1 ? 'X' : (axis === 2 ? 'Y' : 'Z');
  try { 
    await invoke('send_reset', { axis });
    log(`Ось ${aName}: Сигнал MC_Reset отправлен в ПЛК`);
  } catch(e) { log("Ошибка сброса: " + e); }
}

export async function moveAbsMain(axis) {
  let prefix = axis === 1 ? 'main_x' : (axis === 2 ? 'main_y' : 'main_z');
  let posVal = parseFloat(document.getElementById(prefix + '_pos').value); 
  let spdVal = parseFloat(document.getElementById(prefix + '_spd').value);
  
  if (isNaN(posVal) || isNaN(spdVal)) return;
  try { 
    await invoke('send_move_abs', { axis, pos: posVal, spd: spdVal, autoPulse: false }); 
    log(`Ручная уставка: Переезд оси ${axis} к точке ${posVal} мм.`); 
  } catch(e) { log("Ошибка MoveAbs: " + e); }
}

export async function moveAbsDbg(axis) {
  let prefix = axis === 1 ? 'dbg_x' : (axis === 2 ? 'dbg_y' : 'dbg_z'); 
  let pos = parseFloat(document.getElementById(prefix + '_abs_pos').value); 
  let spd = parseFloat(document.getElementById(prefix + '_abs_spd').value);
  try { 
    await invoke('send_move_abs', { axis, pos, spd, autoPulse: false }); 
  } catch(e) { log("Ошибка отладки: " + e); }
}

// МОДИФИЦИРОВАНО: Программное изменение позиции (M17/D16 для оси X и аналогично для Y/Z)
export async function setPosDbg(axis) {
  let prefix = axis === 1 ? 'dbg_x' : (axis === 2 ? 'dbg_y' : 'dbg_z');
  let pos = parseFloat(document.getElementById(prefix + '_set_val').value);
  if (isNaN(pos)) { log("Ошибка: Некорректная координата смещения!"); return; }
  try { 
    await invoke('send_set_position', { axis, pos }); 
    log(`Ось ${axis}: Позиция переопределена. Координата приравнена к ${pos} мм`);
  } catch(e) { log("Ошибка смещения положения: " + e); }
}

// МОДИФИЦИРОВАНО: Функция мгновенного сброса текущей позиции в 0.00 мм
export async function resetPosDbg(axis) {
  try { 
    await invoke('send_set_position', { axis, pos: 0.0 }); 
    log(`Ось ${axis}: Выполнено обнуление текущей позиции (Reset Position -> 0.00 мм)`);
  } catch(e) { log("Ошибка сброса позиции: " + e); }
}

// МОДИФИЦИРОВАНО: Включение режима "Круиз" — движение с постоянной скоростью (MC_MoveVelocity)
export async function moveVelDbg(axis) {
  let prefix = axis === 1 ? 'dbg_x' : (axis === 2 ? 'dbg_y' : 'dbg_z');
  let speed = parseFloat(document.getElementById(prefix + '_vel_spd').value);
  let acc = parseFloat(document.getElementById(prefix + '_vel_acc').value);
  let dec = parseFloat(document.getElementById(prefix + '_vel_dec').value);
  
  if (isNaN(speed) || isNaN(acc) || isNaN(dec)) { log("Ошибка: Параметры скорости не заполнены!"); return; }
  try {
    await invoke('send_move_velocity', { axis, speed, acc, dec });
    log(`Ось ${axis}: Активировано непрерывное движение (Круиз) со скоростью ${speed} мм/с`);
  } catch(e) { log("Ошибка запуска круиза: " + e); }
}

// МОДИФИЦИРОВАНО: Аппаратный останов оси в режиме круиза (MC_Stop)
export async function stopVelDbg(axis) {
  let prefix = axis === 1 ? 'dbg_x' : (axis === 2 ? 'dbg_y' : 'dbg_z');
  let dec = parseFloat(document.getElementById(prefix + '_vel_dec').value);
  if (isNaN(dec)) dec = 20.0;
  try {
    await invoke('send_stop', { axis, decel: dec });
    log(`Ось ${axis}: Отправлен сигнал экстренного торможения MC_Stop`);
  } catch(e) { log("Ошибка вызова MC_Stop: " + e); }
}

// МОДИФИЦИРОВАНО: Ручной вызов/чтение ошибки из блока MC_ReadAxisError
export async function readAxisErrorDebug(axis) {
  log(`Запрос: Считывание буфера блока MC_ReadAxisError для оси ${axis}...`);
  // Телеметрия опрашивается автоматически, функция подтверждает актуальность вывода кодов ошибок
}

export async function triggerPulseDebug() {
  log("Кнопка: Ручной запуск замера (импульс Y0) из панели отладки");
  try {
    let res = await invoke('send_pulse_trigger');
    log(res);
  } catch(e) { log("Ошибка импульса Y0: " + e); }
}

export async function setY0Toggle(state) {
  log(`Кнопка: Ручное изменение выхода Y0 ПЛК в состояние -> ${state ? 'HIGH' : 'LOW'}`);
  try {
    await invoke('send_y0_toggle', { status: state });
    log(`Уровень M50 (Y0) успешно изменен на ${state ? 'High (1)' : 'Low (0)'}`);
  } catch(e) { log("Ошибка ручного управления Y0: " + e); }
}

export async function setOutputToggle(state) {
  log(`Кнопка: Изменение выхода Y1 ПЛК в состояние -> ${state ? 'HIGH' : 'LOW'}`);
  try {
    await invoke('send_output_toggle', { status: state });
    log(`Уровень Y1 успешно изменен на ${state ? 'High' : 'Low'}`);
  } catch(e) { log("Ошибка управления Y1: " + e); }
}

// Включение силовых контуров сразу всех подключённых осей (active_axes).
export async function enableAllMotors() {
  const axes = (Array.isArray(window.activeAxes) && window.activeAxes.length) ? window.activeAxes : [1, 2, 3];
  log(`Кнопка: включение приводов осей [${axes.join(', ')}] (Servo ON)`);
  try {
    await invoke('enable_all_motors', { axes });
    log("Все указанные приводы включены.");
  } catch(e) { log("Ошибка включения приводов: " + e); }
}

// Аварийный стоп всех осей: торможение MC_Stop + снятие питания, и останов сценария.
export async function emergencyStopAll() {
  log("⛔ АВАРИЙНЫЙ СТОП всех осей!");
  try {
    await invoke('emergency_stop_all', { decel: 5000.0 });
    log("Команда экстренного торможения и снятия питания отправлена на все оси.");
  } catch(e) { log("Ошибка аварийного стопа: " + e); }
  if (typeof window.stopQueue === 'function') window.stopQueue();
}

export async function setFreqConfig(enabled) {
  let freqVal = parseFloat(document.getElementById('dbg_out_freq').value);
  if (isNaN(freqVal)) { log("Ошибка: Задайте валидное значение частоты!"); return; }
  log(`Кнопка: Сигнал генератора Y2 -> ${enabled ? 'СТАРТ (' + freqVal + ' Гц)' : 'СТОП'}`);
  try {
    await invoke('send_freq_config', { enabled: enabled, freq: freqVal });
    log(`Параметры частоты меандра применены к ПЛК`);
  } catch(e) { log("Ошибка генератора Y2: " + e); }
}

window.sendPower = sendPower; window.sendReset = sendReset; window.moveAbsMain = moveAbsMain;
window.moveAbsDbg = moveAbsDbg; window.setPosDbg = setPosDbg; window.resetPosDbg = resetPosDbg;
window.moveVelDbg = moveVelDbg; window.stopVelDbg = stopVelDbg; window.readAxisErrorDebug = readAxisErrorDebug;
window.triggerPulseDebug = triggerPulseDebug; window.setOutputToggle = setOutputToggle;
window.setFreqConfig = setFreqConfig; window.setY0Toggle = setY0Toggle;
window.enableAllMotors = enableAllMotors; window.emergencyStopAll = emergencyStopAll;