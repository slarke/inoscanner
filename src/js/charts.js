// src/js/charts.js
import { log } from './logger.js';

// МОДИФИЦИРОВАНО: Хелпер научной нотации для рисок шкал графиков при больших лимитах
function formatChartLabel(val) {
  if (Math.abs(val) >= 10000 || (Math.abs(val) < 0.01 && val !== 0)) {
    return val.toExponential(1); // Сокращенный вывод экспоненты на графике (1 знак)
  }
  return Math.round(val).toString();
}

export function drawXYPlot(x, y, limits) {
  const canvas = document.getElementById('xyPlotCanvas');
  if (!canvas) return;
  
  const parentElement = canvas.parentElement;
  if (!parentElement || parentElement.clientWidth <= 0) {
    return; 
  }

  canvas.width = parentElement.clientWidth - 24;
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height, padding = 40;
  
  ctx.clearRect(0, 0, w, h); 
  ctx.fillStyle = "#fcfcfc";
  ctx.fillRect(padding, padding, w - padding * 2, h - padding * 2); 
  ctx.strokeStyle = "#e5e5e5";
  
  const scaleX = (val) => padding + ((val - limits.xMin) / (limits.xMax - limits.xMin)) * (w - padding * 2);
  const scaleY = (val) => (h - padding) - ((val - limits.yMin) / (limits.yMax - limits.yMin)) * (h - padding * 2);
  
  ctx.font = "10px Segoe UI"; 
  ctx.fillStyle = "#555"; 
  ctx.textAlign = "center";
  
  for (let i = 0; i <= 10; i++) {
    let xVal = limits.xMin + i * (limits.xMax - limits.xMin) / 10;
    let px = scaleX(xVal);
    ctx.beginPath(); ctx.setLineDash([4, 4]); ctx.moveTo(px, padding); ctx.lineTo(px, h - padding); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillText(formatChartLabel(xVal), px, h - padding + 15); // Применение форматирования рисок X
    
    let yVal = limits.yMin + i * (limits.yMax - limits.yMin) / 10;
    let py = scaleY(yVal);
    ctx.beginPath(); ctx.setLineDash([4, 4]); ctx.moveTo(padding, py); ctx.lineTo(w - padding, py); ctx.stroke();
    ctx.setLineDash([]); ctx.textAlign = "right";
    ctx.fillText(formatChartLabel(yVal), padding - 8, py + 3); ctx.textAlign = "center"; // Применение форматирования рисок Y
  }
  
  ctx.strokeStyle = "#333"; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(padding, h - padding); ctx.lineTo(w - padding, h - padding); ctx.moveTo(padding, padding); ctx.lineTo(padding, h - padding); ctx.stroke();
  
  let cx = Math.max(padding, Math.min(scaleX(x), w - padding)); 
  let cy = Math.max(padding, Math.min(scaleY(y), h - padding));
  
  ctx.fillStyle = "#0078d4"; ctx.beginPath();
  ctx.arc(cx, cy, 8, 0, Math.PI * 2); ctx.fill(); ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.stroke();
}

export function drawLinearAxis(canvasId, value, color, limits) {
  const canvas = document.getElementById(canvasId); 
  if (!canvas) return;
  
  if (canvas.parentElement && canvas.parentElement.clientWidth <= 0) {
    return; 
  }
  
  const ctx = canvas.getContext('2d'); 
  const w = canvas.width, h = canvas.height, pad = 30, trackY = h / 2;
  
  ctx.clearRect(0, 0, w, h); 
  ctx.strokeStyle = "#e0e0e0"; 
  ctx.lineWidth = 4; 
  ctx.beginPath(); ctx.moveTo(pad, trackY); ctx.lineTo(w - pad, trackY); ctx.stroke();
  
  ctx.font = "10px Segoe UI"; ctx.fillStyle = "#555"; ctx.textAlign = "center"; ctx.strokeStyle = "#aaaaaa"; ctx.lineWidth = 1;
  
  // МОДИФИЦИРОВАНО: Динамический выбор границ в зависимости от типа оси
  let min = limits.xMin;
  let max = limits.xMax;
  if (canvasId === 'zLinearCanvas') {
    min = limits.zMin;
    max = limits.zMax;
  }
  
  for (let i = 0; i <= 5; i++) {
    let val = min + i * (max - min) / 5;
    let px = pad + (i * (w - pad * 2) / 5);
    ctx.beginPath(); ctx.moveTo(px, trackY - 5);
    ctx.lineTo(px, trackY + 5); ctx.stroke(); 
    ctx.fillText(formatChartLabel(val), px, trackY + 18); // Применение форматирования
  }
  
  let range = max - min;
  let clampedVal = Math.max(min, Math.min(value, max));
  let markerX = pad + ((clampedVal - min) / range) * (w - pad * 2);
  ctx.fillStyle = color; ctx.beginPath(); ctx.arc(markerX, trackY, 7, 0, Math.PI * 2); ctx.fill();
}