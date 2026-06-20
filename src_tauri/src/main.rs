// src-tauri/src/main.rs
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod config;

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::OnceLock;
use tokio::sync::Mutex;
use tokio::time::{sleep, Duration};
use tokio_modbus::client::tcp::connect;
use tokio_modbus::client::Context;
use tokio_modbus::prelude::{Reader, Writer};
use tauri::{AppHandle, State, Emitter};
use serde::Serialize;

#[derive(Clone, Serialize)]
struct TelemetryPayload {
    x_pos: f32, x_vel: f32,
    y_pos: f32, y_vel: f32,
    z_pos: f32, z_vel: f32,
    x_pos_raw: Vec<u16>, x_vel_raw: Vec<u16>,
    y_pos_raw: Vec<u16>, y_vel_raw: Vec<u16>,
    z_pos_raw: Vec<u16>, z_vel_raw: Vec<u16>,
    block_error: u16,
    x_servo_error: u16, x_axis_error: u16,
    y_servo_error: u16, y_axis_error: u16,
    z_servo_error: u16, z_axis_error: u16,
    inputs: Vec<bool>,
    outputs: Vec<bool>,
}

/// Статус Modbus-связи, транслируемый во фронтенд (событие `connection-status`).
/// `status` — одно из: "connected", "reconnecting", "disconnected".
#[derive(Clone, Serialize)]
struct ConnStatus {
    status: String,
    message: String,
}

struct PlcManager {
    context: Arc<Mutex<Option<Context>>>,
    /// Намерение оператора держать связь (true между connect_plc и disconnect_plc).
    /// Супервайзер переподключается только пока флаг взведён.
    want_link: Arc<Mutex<bool>>,
    /// Адрес и период опроса для авто-переподключения после обрыва.
    target: Arc<Mutex<Option<(SocketAddr, u64)>>>,
}

fn f32_to_words(val: f32) -> Vec<u16> {
    let bits = val.to_bits();
    let vh = (bits >> 16) as u16;
    let vl = (bits & 0xFFFF) as u16;
    vec![vl, vh]
}

fn words_to_f32(regs: &[u16]) -> f32 {
    if regs.len() < 2 { return 0.0; }
    let vl = regs[0] as u32;
    let vh = regs[1] as u32;
    let bits = (vh << 16) | vl;
    f32::from_bits(bits)
}

// --------------------------------------------------------------------------- //
//  Опрос телеметрии и авто-переподключение                                    //
// --------------------------------------------------------------------------- //

/// Взводит Enable у блоков MC_ReadAxisError (выполняется после каждого коннекта).
async fn rearm_error_coils(ctx: &mut Context) {
    let _ = ctx.write_single_coil(config::M1_READ_ERROR_EN, true).await;
    let _ = ctx.write_single_coil(config::M2_READ_ERROR_EN, true).await;
    let _ = ctx.write_single_coil(config::M3_READ_ERROR_EN, true).await;
}

/// Один цикл чтения телеметрии. Чтение позиции оси X используется как «канарейка»:
/// если оно вернуло ошибку — связь считается потерянной (ошибка пробрасывается).
/// Остальные регистры читаются толерантно (неотмапленные Y/Z не валят опрос).
async fn poll_once(ctx: &mut Context) -> std::io::Result<TelemetryPayload> {
    let x_p_regs = ctx.read_holding_registers(config::D1_ACT_POS, 2).await?;

    let x_v_regs = ctx.read_holding_registers(config::D1_ACT_VEL, 2).await.unwrap_or(vec![0, 0]);
    let y_p_regs = ctx.read_holding_registers(config::D2_ACT_POS, 2).await.unwrap_or(vec![0, 0]);
    let y_v_regs = ctx.read_holding_registers(config::D2_ACT_VEL, 2).await.unwrap_or(vec![0, 0]);
    let z_p_regs = ctx.read_holding_registers(config::D3_ACT_POS, 2).await.unwrap_or(vec![0, 0]);
    let z_v_regs = ctx.read_holding_registers(config::D3_ACT_VEL, 2).await.unwrap_or(vec![0, 0]);

    let block_err_reg = ctx.read_holding_registers(config::D_BLOCK_ERROR_ID, 1).await.unwrap_or(vec![0]);
    let block_error = block_err_reg[0];

    let x_err_regs = ctx.read_holding_registers(config::D1_SERVO_ERROR_ID, 3).await.unwrap_or(vec![0, 0, 0]);
    let y_err_regs = ctx.read_holding_registers(config::D2_SERVO_ERROR_ID, 3).await.unwrap_or(vec![0, 0, 0]);
    let z_err_regs = ctx.read_holding_registers(config::D3_SERVO_ERROR_ID, 3).await.unwrap_or(vec![0, 0, 0]);

    let out_coils = ctx.read_coils(config::M_Y0_SET, 4).await.unwrap_or(vec![false, false, false, false]);
    let in_coils = ctx.read_coils(config::M_INPUT_X0, 4).await.unwrap_or(vec![false, false, false, false]);

    Ok(TelemetryPayload {
        x_pos: words_to_f32(&x_p_regs), x_vel: words_to_f32(&x_v_regs),
        y_pos: words_to_f32(&y_p_regs), y_vel: words_to_f32(&y_v_regs),
        z_pos: words_to_f32(&z_p_regs), z_vel: words_to_f32(&z_v_regs),
        x_pos_raw: x_p_regs, x_vel_raw: x_v_regs,
        y_pos_raw: y_p_regs, y_vel_raw: y_v_regs,
        z_pos_raw: z_p_regs, z_vel_raw: z_v_regs,
        block_error,
        x_servo_error: x_err_regs[0], x_axis_error: x_err_regs[2],
        y_servo_error: y_err_regs[0], y_axis_error: y_err_regs[2],
        z_servo_error: z_err_regs[0], z_axis_error: z_err_regs[2],
        inputs: in_coils,
        outputs: out_coils,
    })
}

fn emit_status(app: &AppHandle, status: &str, message: &str) {
    let _ = app.emit("connection-status", ConnStatus {
        status: status.to_string(),
        message: message.to_string(),
    });
}

/// Фоновый супервайзер связи: опрашивает телеметрию, детектирует обрыв и
/// автоматически переподключается (например, после смены/включения VPN), пока
/// взведён `want_link`. Транслирует статус во фронтенд.
async fn supervisor(
    ctx_arc: Arc<Mutex<Option<Context>>>,
    want_arc: Arc<Mutex<bool>>,
    target_arc: Arc<Mutex<Option<(SocketAddr, u64)>>>,
    app: AppHandle,
    interval_ms: u64,
) {
    const RECONNECT_MS: u64 = 2000;   // пауза между попытками переподключения
    const FAIL_LIMIT: u32 = 3;        // подряд сбоев опроса до объявления обрыва
    let mut failures: u32 = 0;

    loop {
        if !*want_arc.lock().await { break; }

        // 1. Гарантируем наличие соединения; иначе — переподключаемся.
        let connected = { ctx_arc.lock().await.is_some() };
        if !connected {
            let target = *target_arc.lock().await;
            if let Some((addr, _)) = target {
                emit_status(&app, "reconnecting", &format!("Переподключение к {}...", addr));
                match connect(addr).await {
                    Ok(mut c) => {
                        rearm_error_coils(&mut c).await;
                        *ctx_arc.lock().await = Some(c);
                        failures = 0;
                        emit_status(&app, "connected", "Связь восстановлена");
                    }
                    Err(_) => {
                        sleep(Duration::from_millis(RECONNECT_MS)).await;
                        continue;
                    }
                }
            } else {
                break;
            }
        }

        // 2. Один цикл опроса. Ошибка «канарейки» — потеря связи.
        {
            let mut guard = ctx_arc.lock().await;
            if let Some(ref mut ctx) = *guard {
                match poll_once(ctx).await {
                    Ok(payload) => {
                        failures = 0;
                        let _ = app.emit("telemetry-update", payload);
                    }
                    Err(_) => {
                        failures += 1;
                        if failures >= FAIL_LIMIT {
                            *guard = None;
                            failures = 0;
                            emit_status(&app, "reconnecting", "Связь потеряна, переподключение...");
                        }
                    }
                }
            }
        }

        sleep(Duration::from_millis(interval_ms)).await;
    }

    emit_status(&app, "disconnected", "Соединение закрыто");
}

#[tauri::command]
async fn connect_plc(
    state: State<'_, PlcManager>,
    app_handle: AppHandle,
    ip: String,
    port: u16,
    interval_ms: u64,
) -> Result<String, String> {
    if *state.want_link.lock().await {
        return Ok("Уже подключено".to_string());
    }

    let socket_addr: SocketAddr = format!("{}:{}", ip, port)
        .parse()
        .map_err(|e| format!("Неверный формат адреса: {}", e))?;

    println!("[INFO] Подключение к ПЛК {}...", socket_addr);

    // Первичный коннект выполняем синхронно — чтобы UI сразу узнал результат.
    let mut ctx = connect(socket_addr).await.map_err(|e| format!("Ошибка сети Modbus: {}", e))?;
    rearm_error_coils(&mut ctx).await;

    *state.context.lock().await = Some(ctx);
    *state.target.lock().await = Some((socket_addr, interval_ms));
    *state.want_link.lock().await = true;

    emit_status(&app_handle, "connected", &format!("Соединение установлено: {}:{}", ip, port));

    // Дальше опросом и переподключением занимается супервайзер.
    let ctx_arc = state.context.clone();
    let want_arc = state.want_link.clone();
    let target_arc = state.target.clone();
    let app = app_handle.clone();
    tokio::spawn(async move {
        supervisor(ctx_arc, want_arc, target_arc, app, interval_ms).await;
    });

    Ok(format!("Успешное соединение с {}:{}", ip, port))
}

#[tauri::command]
async fn disconnect_plc(state: State<'_, PlcManager>, app_handle: AppHandle) -> Result<(), String> {
    // Сначала снимаем намерение — иначе супервайзер тут же переподключится.
    *state.want_link.lock().await = false;
    *state.context.lock().await = None;
    *state.target.lock().await = None;
    emit_status(&app_handle, "disconnected", "Сессия закрыта оператором");
    Ok(())
}

#[tauri::command]
async fn send_power(state: State<'_, PlcManager>, axis: u8, status: bool) -> Result<(), String> {
    let mut ctx_lock = state.context.lock().await;
    if let Some(ref mut ctx) = *ctx_lock {
        let addr = match axis {
            1 => config::M1_POWER,
            2 => config::M2_POWER,
            3 => config::M3_POWER,
            _ => return Err("Неверный номер оси".to_string()),
        };
        ctx.write_single_coil(addr, status).await.map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Включить силовые контуры (Servo ON) сразу нескольких осей.
/// Пустой список трактуется как «все оси» (1,2,3).
#[tauri::command]
async fn enable_all_motors(state: State<'_, PlcManager>, axes: Vec<u8>) -> Result<(), String> {
    let list = if axes.is_empty() { vec![1u8, 2, 3] } else { axes };
    let mut ctx_lock = state.context.lock().await;
    if let Some(ref mut ctx) = *ctx_lock {
        for ax in list {
            let coil = match ax {
                1 => config::M1_POWER,
                2 => config::M2_POWER,
                3 => config::M3_POWER,
                _ => continue,
            };
            ctx.write_single_coil(coil, true).await.map_err(|e| e.to_string())?;
            sleep(Duration::from_millis(10)).await;
        }
    } else {
        return Err("Нет связи с ПЛК".to_string());
    }
    Ok(())
}

/// Аварийный стоп всех осей: торможение MC_Stop с большим замедлением, затем
/// снятие питания (Servo OFF) с каждой оси. Best-effort: сбой одной оси не
/// прерывает обработку остальных.
#[tauri::command]
async fn emergency_stop_all(state: State<'_, PlcManager>, decel: f32) -> Result<(), String> {
    let mut ctx_lock = state.context.lock().await;
    if let Some(ref mut ctx) = *ctx_lock {
        let axes: [(u16, u16, u16); 3] = [
            (config::M1_STOP_EXEC, config::D1_STOP_DEC, config::M1_POWER),
            (config::M2_STOP_EXEC, config::D2_STOP_DEC, config::M2_POWER),
            (config::M3_STOP_EXEC, config::D3_STOP_DEC, config::M3_POWER),
        ];
        let dwords = f32_to_words(decel);
        // 1. Заряжаем замедление и строб MC_Stop на каждой оси.
        for (stop_coil, dec_reg, _) in axes.iter() {
            let _ = ctx.write_multiple_registers(*dec_reg, &dwords).await;
            let _ = ctx.write_single_coil(*stop_coil, true).await;
        }
        sleep(Duration::from_millis(40)).await;
        // 2. Снимаем строб и питание.
        for (stop_coil, _, power_coil) in axes.iter() {
            let _ = ctx.write_single_coil(*stop_coil, false).await;
            let _ = ctx.write_single_coil(*power_coil, false).await;
        }
    } else {
        return Err("Нет связи с ПЛК".to_string());
    }
    Ok(())
}

#[tauri::command]
async fn send_reset(state: State<'_, PlcManager>, axis: u8) -> Result<(), String> {
    let mut ctx_lock = state.context.lock().await;
    if let Some(ref mut ctx) = *ctx_lock {
        let addr = match axis {
            1 => config::M1_RESET,
            2 => config::M2_RESET,
            3 => config::M3_RESET,
            _ => return Err("Неверный номер оси".to_string()),
        };
        ctx.write_single_coil(addr, true).await.map_err(|e| e.to_string())?;
        sleep(Duration::from_millis(100)).await;
        ctx.write_single_coil(addr, false).await.map_err(|e| e.to_string())?;
    }
    Ok(())
}

// МОДИФИЦИРОВАНО: Реализация Варианта 1. Передача флага режима разрешения импульса auto_pulse
#[tauri::command]
async fn send_move_abs(state: State<'_, PlcManager>, axis: u8, pos: f32, spd: f32, auto_pulse: bool) -> Result<(), String> {
    let mut ctx_lock = state.context.lock().await;
    if let Some(ref mut ctx) = *ctx_lock {
        let (base_p, base_s, coil_exec, coil_pulse_en) = match axis {
            1 => (config::D1_ABS_POS, config::D1_ABS_SPD, config::M1_ABS_EXEC, config::M1_ABS_PULSE_EN),
            2 => (config::D2_ABS_POS, config::D2_ABS_SPD, config::M2_ABS_EXEC, config::M2_ABS_PULSE_EN),
            3 => (config::D3_ABS_POS, config::D3_ABS_SPD, config::M3_ABS_EXEC, config::M3_ABS_PULSE_EN),
            _ => return Err("Неверный номер оси".to_string()),
        };

        // 1. Выставляем модификатор режима: отправлять ли импульс по доезду (M14 / M24 / M34)
        ctx.write_single_coil(coil_pulse_en, auto_pulse).await.map_err(|e| e.to_string())?;
        sleep(Duration::from_millis(10)).await;

        // 2. Загружаем числовые параметры траектории сканера
        ctx.write_multiple_registers(base_p, &f32_to_words(pos)).await.map_err(|e| e.to_string())?;
        ctx.write_multiple_registers(base_s, &f32_to_words(spd)).await.map_err(|e| e.to_string())?;

        // 3. Строб переднего фронта запуска блока движения MC_MoveAbsolute (M13 / M23 / M33)
        sleep(Duration::from_millis(20)).await;
        ctx.write_single_coil(coil_exec, true).await.map_err(|e| e.to_string())?;
        sleep(Duration::from_millis(40)).await;
        ctx.write_single_coil(coil_exec, false).await.map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
async fn send_move_velocity(state: State<'_, PlcManager>, axis: u8, speed: f32, acc: f32, dec: f32) -> Result<(), String> {
    let mut ctx_lock = state.context.lock().await;
    if let Some(ref mut ctx) = *ctx_lock {
        let (base_v, coil) = match axis {
            1 => (config::D1_VEL_SPD, config::M1_VEL_EXEC),
            2 => (config::D2_VEL_SPD, config::M2_VEL_EXEC),
            3 => (config::D3_VEL_SPD, config::M3_VEL_EXEC),
            _ => return Err("Неверный номер оси".to_string()),
        };

        let mut words = Vec::new();
        words.extend(f32_to_words(speed));
        words.extend(f32_to_words(acc));
        words.extend(f32_to_words(dec));

        ctx.write_multiple_registers(base_v, &words).await.map_err(|e| e.to_string())?;

        sleep(Duration::from_millis(50)).await;
        ctx.write_single_coil(coil, true).await.map_err(|e| e.to_string())?;
        sleep(Duration::from_millis(100)).await;
        ctx.write_single_coil(coil, false).await.map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
async fn send_stop(state: State<'_, PlcManager>, axis: u8, decel: f32) -> Result<(), String> {
    let mut ctx_lock = state.context.lock().await;
    if let Some(ref mut ctx) = *ctx_lock {
        let (coil, reg_dec) = match axis {
            1 => (config::M1_STOP_EXEC, config::D1_STOP_DEC),
            2 => (config::M2_STOP_EXEC, config::D2_STOP_DEC),
            3 => (config::M3_STOP_EXEC, config::D3_STOP_DEC),
            _ => return Err("Неверный номер оси".to_string()),
        };

        ctx.write_multiple_registers(reg_dec, &f32_to_words(decel)).await.map_err(|e| e.to_string())?;

        sleep(Duration::from_millis(20)).await;
        ctx.write_single_coil(coil, true).await.map_err(|e| e.to_string())?;
        sleep(Duration::from_millis(40)).await;
        ctx.write_single_coil(coil, false).await.map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
async fn send_set_position(state: State<'_, PlcManager>, axis: u8, pos: f32) -> Result<(), String> {
    let mut ctx_lock = state.context.lock().await;
    if let Some(ref mut ctx) = *ctx_lock {
        let (coil, reg_pos) = match axis {
            1 => (config::M1_SET_POS_EXEC, config::D1_SET_POS_VAL),
            2 => (config::M2_SET_POS_EXEC, config::D2_SET_POS_VAL),
            3 => (config::M3_SET_POS_EXEC, config::D3_SET_POS_VAL),
            _ => return Err("Неверный номер оси".to_string()),
        };

        ctx.write_multiple_registers(reg_pos, &f32_to_words(pos)).await.map_err(|e| e.to_string())?;

        sleep(Duration::from_millis(20)).await;
        ctx.write_single_coil(coil, true).await.map_err(|e| e.to_string())?;
        sleep(Duration::from_millis(40)).await;
        ctx.write_single_coil(coil, false).await.map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
async fn send_pulse_trigger(state: State<'_, PlcManager>) -> Result<String, String> {
    let mut ctx_guard = state.context.lock().await;
    if let Some(ref mut ctx) = *ctx_guard {
        ctx.write_single_coil(config::M_Y0_SET, true).await.map_err(|e| e.to_string())?;
        sleep(Duration::from_millis(50)).await;
        ctx.write_single_coil(config::M_Y0_SET, false).await.map_err(|e| e.to_string())?;

        sleep(Duration::from_millis(150)).await;

        ctx.write_single_coil(config::M_Y0_RESET, true).await.map_err(|e| e.to_string())?;
        sleep(Duration::from_millis(50)).await;
        ctx.write_single_coil(config::M_Y0_RESET, false).await.map_err(|e| e.to_string())?;

        Ok("Импульсный цикл авто-замера успешно отработан бэкендом".to_string())
    } else {
        Err("Нет связи с ПЛК по Modbus TCP".to_string())
    }
}

#[tauri::command]
async fn send_y0_toggle(state: State<'_, PlcManager>, status: bool) -> Result<(), String> {
    let mut ctx_lock = state.context.lock().await;
    if let Some(ref mut ctx) = *ctx_lock {
        if status {
            ctx.write_single_coil(config::M_Y0_SET, true).await.map_err(|e| e.to_string())?;
            sleep(Duration::from_millis(50)).await;
            ctx.write_single_coil(config::M_Y0_SET, false).await.map_err(|e| e.to_string())?;
        } else {
            ctx.write_single_coil(config::M_Y0_RESET, true).await.map_err(|e| e.to_string())?;
            sleep(Duration::from_millis(50)).await;
            ctx.write_single_coil(config::M_Y0_RESET, false).await.map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

#[tauri::command]
async fn send_output_toggle(state: State<'_, PlcManager>, status: bool) -> Result<(), String> {
    let mut ctx_lock = state.context.lock().await;
    if let Some(ref mut ctx) = *ctx_lock {
        ctx.write_single_coil(config::M_Y1_TOGGLE, status).await.map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
async fn send_freq_config(state: State<'_, PlcManager>, enabled: bool, freq: f32) -> Result<(), String> {
    let mut ctx_lock = state.context.lock().await;
    if let Some(ref mut ctx) = *ctx_lock {
        ctx.write_multiple_registers(config::D_Y2_FREQ_VAL, &f32_to_words(freq)).await.map_err(|e| e.to_string())?;
        sleep(Duration::from_millis(20)).await;
        ctx.write_single_coil(config::M_Y2_FREQ_EN, enabled).await.map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn save_sequence_json(content: String) -> Result<String, String> {
    let file_path = rfd::FileDialog::new()
        .set_title("Экспорт сценария сканирования")
        .set_file_name("scan_sequence.json")
        .add_filter("Сценарий JSON", &["json"])
        .save_file();

    if let Some(path) = file_path {
        std::fs::write(&path, content).map_err(|e| format!("Ошибка записи: {}", e))?;
        Ok("Сценарий успешно экспортирован".to_string())
    } else { Ok("Экспорт отменен".to_string()) }
}

#[tauri::command]
fn load_sequence_json() -> Result<String, String> {
    let file_path = rfd::FileDialog::new()
        .set_title("Импорт сценария сканирования")
        .add_filter("Сценарий JSON", &["json"])
        .pick_file();

    if let Some(path) = file_path {
        std::fs::read_to_string(path).map_err(|e| format!("Ошибка чтения: {}", e))
    } else { Err("Импорт отменен".to_string()) }
}

// --------------------------------------------------------------------------- //
//  Файлы рядом с приложением: логи, конфиг, сессия сканирования               //
// --------------------------------------------------------------------------- //

/// Каталог рядом с исполняемым файлом (в dev — target/debug). Туда кладём
/// app_config.json, log/, scan_session.json — чтобы данные жили рядом с .exe.
fn app_base_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(std::env::temp_dir)
}

/// Преобразование дней эпохи Unix в (год, месяц, день) — алгоритм Хиннанта,
/// без внешних зависимостей. Используется для имени лог-файла (UTC).
fn civil_from_days(z0: i64) -> (i64, u32, u32) {
    let z = z0 + 719468;
    let era = (if z >= 0 { z } else { z - 146096 }) / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m as u32, d as u32)
}

fn utc_stamp() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let days = (secs / 86400) as i64;
    let rem = secs % 86400;
    let (h, mi, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    let (y, m, d) = civil_from_days(days);
    format!("{:04}{:02}{:02}_{:02}{:02}{:02}", y, m, d, h, mi, s)
}

/// Путь к лог-файлу текущего запуска (создаётся один раз: log/plc_panel_<stamp>.log).
fn run_log_path() -> PathBuf {
    static LOG_PATH: OnceLock<PathBuf> = OnceLock::new();
    LOG_PATH
        .get_or_init(|| {
            let dir = app_base_dir().join("log");
            let _ = std::fs::create_dir_all(&dir);
            dir.join(format!("plc_panel_{}.log", utc_stamp()))
        })
        .clone()
}

#[tauri::command]
fn append_to_log_file(line: String) -> Result<(), String> {
    use std::fs::OpenOptions;
    use std::io::Write;
    let path = run_log_path();
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| e.to_string())?;
    writeln!(file, "{}", line).map_err(|e| e.to_string())?;
    Ok(())
}

fn config_path() -> PathBuf { app_base_dir().join("app_config.json") }
fn session_path() -> PathBuf { app_base_dir().join("scan_session.json") }

/// Возвращает содержимое app_config.json (или "{}" если файла нет).
#[tauri::command]
fn load_app_config() -> Result<String, String> {
    Ok(std::fs::read_to_string(config_path()).unwrap_or_else(|_| "{}".to_string()))
}

#[tauri::command]
fn save_app_config(content: String) -> Result<(), String> {
    std::fs::write(config_path(), content).map_err(|e| e.to_string())
}

/// Снимок прерванного сценария (пусто, если файла нет). См. crash-resume.
#[tauri::command]
fn load_scan_session() -> Result<String, String> {
    Ok(std::fs::read_to_string(session_path()).unwrap_or_default())
}

#[tauri::command]
fn save_scan_session(content: String) -> Result<(), String> {
    std::fs::write(session_path(), content).map_err(|e| e.to_string())
}

#[tauri::command]
fn clear_scan_session() -> Result<(), String> {
    let _ = std::fs::remove_file(session_path());
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .manage(PlcManager {
            context: Arc::new(Mutex::new(None)),
            want_link: Arc::new(Mutex::new(false)),
            target: Arc::new(Mutex::new(None)),
        })
        .invoke_handler(tauri::generate_handler![
            connect_plc, disconnect_plc, send_power, send_reset,
            send_move_abs, send_move_velocity, send_pulse_trigger, send_stop,
            send_set_position, save_sequence_json, load_sequence_json, append_to_log_file,
            send_output_toggle, send_freq_config, send_y0_toggle,
            enable_all_motors, emergency_stop_all,
            load_app_config, save_app_config,
            load_scan_session, save_scan_session, clear_scan_session
        ])
        .run(tauri::generate_context!())
        .expect("Ошибка запуска среды Tauri");
}
