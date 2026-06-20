// src-tauri/src/main.rs
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod config;

use std::net::SocketAddr;
use std::sync::Arc;
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

struct PlcManager {
    context: Arc<Mutex<Option<Context>>>,
    is_polling: Arc<Mutex<bool>>,
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

#[tauri::command]
async fn connect_plc(
    state: State<'_, PlcManager>, 
    app_handle: AppHandle, 
    ip: String, 
    port: u16, 
    interval_ms: u64
) -> Result<String, String> {
    let mut ctx_guard = state.context.lock().await;
    if ctx_guard.is_some() {
        return Ok("Уже подключено".to_string());
    }

    let socket_addr: SocketAddr = format!("{}:{}", ip, port)
        .parse()
        .map_err(|e| format!("Неверный формат адреса: {}", e))?;

    println!("[INFO] Подключение к ПЛК {}...", socket_addr);
    
    let mut ctx = connect(socket_addr).await.map_err(|e| format!("Ошибка сети Modbus: {}", e))?;

    let _ = ctx.write_single_coil(config::M1_READ_ERROR_EN, true).await;
    let _ = ctx.write_single_coil(config::M2_READ_ERROR_EN, true).await;
    let _ = ctx.write_single_coil(config::M3_READ_ERROR_EN, true).await;

    *ctx_guard = Some(ctx);

    let mut polling_guard = state.is_polling.lock().await;
    *polling_guard = true;

    let ctx_clone = state.context.clone();
    let polling_clone = state.is_polling.clone();

    tokio::spawn(async move {
        while *polling_clone.lock().await {
            let mut ctx_lock = ctx_clone.lock().await;
            if let Some(ref mut ctx) = *ctx_lock {
                
                let x_p_regs = ctx.read_holding_registers(config::D1_ACT_POS, 2).await.unwrap_or(vec![0, 0]);
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

                let payload = TelemetryPayload {
                    x_pos: words_to_f32(&x_p_regs), x_vel: words_to_f32(&x_v_regs),
                    y_pos: words_to_f32(&y_p_regs), y_vel: words_to_f32(&y_v_regs),
                    z_pos: words_to_f32(&z_p_regs), z_vel: words_to_f32(&z_v_regs),
                    x_pos_raw: x_p_regs, x_vel_raw: x_v_regs,
                    y_pos_raw: y_p_regs, y_vel_raw: y_v_regs,
                    z_pos_raw: z_p_regs, z_vel_raw: z_v_regs,
                    block_error,
                    x_servo_error: x_err_regs[0], x_axis_error:  x_err_regs[2], 
                    y_servo_error: y_err_regs[0], y_axis_error:  y_err_regs[2], 
                    z_servo_error: z_err_regs[0], z_axis_error:  z_err_regs[2], 
                    inputs: in_coils,
                    outputs: out_coils,
                };

                let _ = app_handle.emit("telemetry-update", payload);
            }
            drop(ctx_lock);
            sleep(Duration::from_millis(interval_ms)).await;
        }
    });

    Ok(format!("Успешное соединение с {}:{}", ip, port))
}

#[tauri::command]
async fn disconnect_plc(state: State<'_, PlcManager>) -> Result<(), String> {
    let mut polling_guard = state.is_polling.lock().await;
    *polling_guard = false;
    let mut ctx_guard = state.context.lock().await;
    *ctx_guard = None;
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

#[tauri::command]
fn append_to_log_file(line: String) -> Result<(), String> {
    use std::fs::OpenOptions;
    use std::io::Write;
    let mut log_path = std::env::temp_dir();
    log_path.push("plc_panel_log.txt");
    let mut file = OpenOptions::new().create(true).write(true).append(true).open(&log_path).map_err(|e| e.to_string())?;
    writeln!(file, "{}", line).map_err(|e| e.to_string())?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .manage(PlcManager {
            context: Arc::new(Mutex::new(None)),
            is_polling: Arc::new(Mutex::new(false)),
        })
        .invoke_handler(tauri::generate_handler![
            connect_plc, disconnect_plc, send_power, send_reset,      
            send_move_abs, send_move_velocity, send_pulse_trigger, send_stop,          
            send_set_position, save_sequence_json, load_sequence_json, append_to_log_file,
            send_output_toggle, send_freq_config, send_y0_toggle
        ])
        .run(tauri::generate_context!())
        .expect("Ошибка запуска среды Tauri");
}