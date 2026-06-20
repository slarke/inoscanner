// src-tauri/src/config.rs
// Конфигурация регистров системы автоматизации XYZ

// --- ДВИГАТЕЛЬ 1 (ОСЬ X) ---
pub const M1_POWER: u16 = 10;        // M10 - Блок MC_Power
pub const M1_VEL_EXEC: u16 = 11;     // M11 - Блок MC_MoveVelocity
pub const M1_RESET: u16 = 12;        // M12 - Блок MC_Reset
pub const M1_ABS_EXEC: u16 = 13;     // M13 - Блок MC_MoveAbsolute (Execute)
pub const M1_ABS_PULSE_EN: u16 = 14; // M14 - Флаг разрешения импульса по Done (Вариант 1)
pub const M1_READ_ERROR_EN: u16 = 15;// M15 - Enable для MC_ReadAxisError
pub const M1_STOP_EXEC: u16 = 16;     // M16 - Execute для MC_Stop
pub const M1_SET_POS_EXEC: u16 = 17;  // M17 - Execute для MC_SetPosition

pub const D1_VEL_SPD: u16 = 10;      // D10 - Скорость круиза
pub const D1_VEL_ACC: u16 = 12;      // D12 - Разгон круиза
pub const D1_VEL_DEC: u16 = 14;      // D14 - Замедление круиза
pub const D1_ABS_POS: u16 = 16;      // D16 - Позиция ABS (MC_MoveAbsolute Position) [MAIN.LD]
pub const D1_ABS_SPD: u16 = 18;      // D18 - Скорость ABS (MC_MoveAbsolute Velocity) [MAIN.LD]
pub const D1_SET_POS_VAL: u16 = 24;   // D24 - Значение позиции для MC_SetPosition (REAL) [MAIN.LD]
pub const D1_STOP_DEC: u16 = 50;     // D50 - Уставка замедления для MC_Stop (REAL)

pub const D1_ACT_POS: u16 = 210;     // D210 - Текущая позиция (REAL)
pub const D1_ACT_VEL: u16 = 214;     // D214 - Текущая скорость (REAL)
pub const D1_SERVO_ERROR_ID: u16 = 220; // D220 - Код ошибки сервопривода (ServoErrorID)
pub const D1_AXIS_ERROR_ID: u16 = 222;  // D222 - Код ошибки оси (AxisErrorID)

// --- ДВИГАТЕЛЬ 2 (ОСЬ Y) ---
pub const M2_POWER: u16 = 20;        // M20
pub const M2_VEL_EXEC: u16 = 21;     // M21
pub const M2_RESET: u16 = 22;        // M22
pub const M2_ABS_EXEC: u16 = 23;     // M23
pub const M2_ABS_PULSE_EN: u16 = 24; // M24 - Флаг разрешения импульса по Done для Y (Вариант 1)
pub const M2_READ_ERROR_EN: u16 = 25;// M25
pub const M2_STOP_EXEC: u16 = 26;     // M26
pub const M2_SET_POS_EXEC: u16 = 27;  // M27

// Карта оси Y приведена к чистому непересекающемуся окну (контракт для будущей
// лестничной программы Axis_1). Прежние адреса пересекались с блоком ошибок оси
// X (D220/D222). Параметры: D100-D117, телеметрия: D230/D240.
pub const D2_VEL_SPD: u16 = 100;     // D100 - Скорость круиза
pub const D2_VEL_ACC: u16 = 102;     // D102 - Разгон круиза
pub const D2_VEL_DEC: u16 = 104;     // D104 - Замедление круиза
pub const D2_ABS_POS: u16 = 106;     // D106 - Позиция ABS
pub const D2_ABS_SPD: u16 = 108;     // D108 - Скорость ABS
pub const D2_SET_POS_VAL: u16 = 114;  // D114 - MC_SetPosition
pub const D2_STOP_DEC: u16 = 116;    // D116 - Замедление MC_Stop
// D110/D112 зарезервированы под MC_MoveRelative (Distance/Velocity)

pub const D2_ACT_POS: u16 = 230;     // D230 - Текущая позиция
pub const D2_ACT_VEL: u16 = 234;     // D234 - Текущая скорость
pub const D2_SERVO_ERROR_ID: u16 = 240; // D240 - ServoErrorID
pub const D2_AXIS_ERROR_ID: u16 = 242;  // D242 - AxisErrorID

// --- ДВИГАТЕЛЬ 3 (ОСЬ Z / ПРИБЛИЖЕНИЕ К АНТЕННЕ) ---
pub const M3_POWER: u16 = 30;        // M30
pub const M3_VEL_EXEC: u16 = 31;     // M31
pub const M3_RESET: u16 = 32;        // M32
pub const M3_ABS_EXEC: u16 = 33;     // M33
pub const M3_ABS_PULSE_EN: u16 = 34; // M34 - Флаг разрешения импульса по Done для Z (Вариант 1)
pub const M3_READ_ERROR_EN: u16 = 35;// M35
pub const M3_STOP_EXEC: u16 = 36;     // M36
pub const M3_SET_POS_EXEC: u16 = 37;  // M37

// Карта оси Z приведена к чистому непересекающемуся окну (контракт для будущей
// лестничной программы Axis_2). Параметры: D130-D147, телеметрия: D250/D260.
pub const D3_VEL_SPD: u16 = 130;     // D130 - Скорость круиза
pub const D3_VEL_ACC: u16 = 132;     // D132 - Разгон круиза
pub const D3_VEL_DEC: u16 = 134;     // D134 - Замедление круиза
pub const D3_ABS_POS: u16 = 136;     // D136 - Позиция ABS
pub const D3_ABS_SPD: u16 = 138;     // D138 - Скорость ABS
pub const D3_SET_POS_VAL: u16 = 144;  // D144 - MC_SetPosition
pub const D3_STOP_DEC: u16 = 146;    // D146 - Замедление MC_Stop
// D140/D142 зарезервированы под MC_MoveRelative (Distance/Velocity)

pub const D3_ACT_POS: u16 = 250;     // D250 - Текущая позиция
pub const D3_ACT_VEL: u16 = 254;     // D254 - Текущая скорость
pub const D3_SERVO_ERROR_ID: u16 = 260; // D260 - ServoErrorID
pub const D3_AXIS_ERROR_ID: u16 = 262;  // D262 - AxisErrorID

// --- ОБЩАЯ ДИАГНОСТИКА СИСТЕМЫ ---
pub const D_BLOCK_ERROR_ID: u16 = 200;  // D200 - Общий ErrorID для всех блоков управления движением

// --- ОТЛАДКА И СИГНАЛЫ ВЫХОДОВ Easy521 ---
pub const M_Y0_SET: u16 = 50;         // M50 - Команда УСТАНОВКИ выхода Y0 (Логическая 1)
pub const M_Y0_RESET: u16 = 51;       // M51 - Команда СБРОСА выхода Y0 (Логический 0)
pub const M_Y1_TOGGLE: u16 = 52;      // M52 - Статическое включение уровня (Выход Y1)
pub const M_Y2_FREQ_EN: u16 = 53;     // M53 - Разрешение генератора частоты (Выход Y2)
pub const D_Y2_FREQ_VAL: u16 = 90;    // D90 - Заданное значение частоты (REAL)

// --- ОТЛАДКА И МОНИТОРИНГ ВХОДОВ Easy521 ---
pub const M_INPUT_X0: u16 = 100;      // M100 - X0 (Home X)
pub const M_INPUT_X1: u16 = 101;      // M101 - X1 (Home Y)
pub const M_INPUT_X2: u16 = 102;      // M102 - X2 (VNA Ready)
pub const M_INPUT_X3: u16 = 103;      // M103 - X3 (E-Stop)