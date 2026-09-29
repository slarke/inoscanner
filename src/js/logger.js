// src/js/logger.js
const { invoke } = window.__TAURI__.core;

// Экспортируем функцию логирования с автоматической дозаписью в файл на ПК
export async function log(msg) {
  const out = document.getElementById('logOutput');
  if (!out) return;
  
  const time = new Date().toLocaleTimeString();
  const logLine = `[${time}] ${msg}`;
  
  out.innerText += logLine + `\n`;
  out.scrollTop = out.scrollHeight;

  try {
    // Вызов Rust-команды дозаписи строки в файл plc_panel_log.txt
    await invoke('append_to_log_file', { line: logLine });
  } catch(e) {
    console.error("Критический сбой дискового сохранения журналов: ", e);
  }
}

// Пробрасываем в глобальное окно для бесперебойной интеграции сторонних вызовов
window.log = log;