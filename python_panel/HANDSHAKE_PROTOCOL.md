# Handshake-протокол servo_core на Inovance Easy521 (AutoShop)

Как реализовать на лестничной диаграмме контракт, который ожидает полный стек
servo_core (`MotionExecutor` / `Supervisor` / `ModbusWatchdog`): **ack_seq**,
**command_state**, **флаги безопасности** и **двунаправленный heartbeat**. Без
этого контракта работает только «Вариант A» (очередь как бэкбон,
`queue_backbone.py`); с ним — «Вариант B» (`supervised_backbone.py`).

Исполняемый эталон этого протокола — `core/mock.py` (`MockPlc`): ладдер должен
вести себя так же. Python-сторона уже готова и проверена headless:
`python -m plc_panel.supervised_demo`.

Этот интерфейс — **супервайзерный**, поверх пер-осевых MC-блоков из
`PLC_LADDER_REQUIREMENTS.md`. Он не заменяет их, а диспетчеризует.

---

## 1. Карта регистров (Modbus holding / D-регистры)

Свободное окно D400+ (не пересекается с пер-осевыми картами и телеметрией).
Должно совпадать с `HANDSHAKE_REGISTERS` в `plc_panel/supervised_backbone.py`.

### Командный блок — Python → ПЛК, одна транзакция FC16 (D400…D405)
| D | Поле | Описание |
|---|------|----------|
| D400 | `target_hi` | старшее слово абсолютной позиции (µm, signed 32-bit) |
| D401 | `target_lo` | младшее слово позиции |
| D402 | `speed` | скорость (milli-units, 16-bit) |
| D403 | `command_code` | опкод+ось+флаг импульса (см. §3) |
| D404 | `seq_hi` | старшее слово запроса `req_seq` (32-bit) |
| D405 | `seq_lo` | младшее слово `req_seq` |

`seq` записывается **логически последним** в одном FC16 — ПЛК никогда не увидит
«рваную» команду (полупорция payload без нового seq).

### Блок статуса — ПЛК → Python, чтение FC03 (D410…D414)
| D | Поле | Описание |
|---|------|----------|
| D410 | `ack_seq_hi` | старшее слово подтверждённого seq |
| D411 | `ack_seq_lo` | младшее слово |
| D412 | `command_state` | код состояния (см. §2) |
| D413 | `error_code` | код ошибки (0 = нет) |
| D414 | `status_flags` | биты READY/ESTOP/GUARD (см. §2) |

### Watchdog (двунаправленный)
| D | Направление | Описание |
|---|-------------|----------|
| D420 | Python → ПЛК | dead-man: Python инкрементирует каждый цикл |
| D421 | ПЛК → Python | heartbeat: ПЛК инкрементирует каждый цикл |

### Внутренние (retentive!) переменные ПЛК
- `last_processed_seq` (32-бит, пара D-регистров, **энергонезависимая**) —
  для дедупликации, должна переживать сброс питания ПЛК.
- защёлки payload (`latched_target`, `latched_speed`, `latched_code`).

---

## 2. Кодировки

`command_state` (D412) — точно как `core/models.py::PLC_STATE_BY_CODE`:

| Код | Состояние |
|-----|-----------|
| 0 | IDLE |
| 1 | RECEIVED |
| 2 | RUNNING |
| 3 | DONE |
| 4 | ERROR |
| 5 | ABORTED |

`status_flags` (D414) — биты как в `core/config.py`:

| Бит | Маска | Значение |
|-----|-------|----------|
| 0 | `0x0001` | READY — можно принимать команду |
| 1 | `0x0002` | ESTOP — аварийный стоп активен |
| 2 | `0x0004` | GUARD_OPEN — защита открыта |

> Executor двигается только при `READY && !ESTOP && !GUARD`. Recovery при старте
> гейтится `ESTOP/GUARD`: при их наличии движение НЕ возобновляется, пока
> оператор не вызовет `acknowledge_safe_state()`.

---

## 3. command_code (D403)

```
command_code = opcode | (axis << 4) | (PULSE_BIT)
    opcode (биты 0..3): 1=MoveAbsolute, 2=Stop, 3=SetPosition, 4=Reset
    axis   (биты 4..7): 1=X, 2=Y, 3=Z
    PULSE_BIT = 0x100 : выдать импульс замера Y0 по Done
```

Декодирование в ладдере: `opcode = D403 AND 16#000F`,
`axis = (D403 AND 16#00F0) / 16` (сдвиг вправо на 4), `pulse = D403 AND 16#0100`.

> ⚠️ **Порядок слов 32-битных значений.** servo_core кладёт **старшее слово
> первым** (`hi` в D404/D410, `lo` в D405/D411). Стандартный double-word
> `DMOV D404` на Easy521 трактует D404 как младшее слово — будет перестановка.
> Поэтому собирать/раскладывать seq и target **вручную**:
> `req_seq = D404 * 65536 + D405`, и при ответе `D410 = ack >> 16`,
> `D411 = ack AND 16#FFFF`. То же для `target`.

---

## 4. Автомат handshake (логика ладдера)

Выполняется каждый цикл скана. Псевдоладдер (инструкции AutoShop: `LD`,
`CMP/DCMP`, `MOV/DMOV`, `SET/RST`, `TON`, импульсные контакты):

### 4.1 Сборка слов
```
req_seq  := D404 * 65536 + D405          ; вручную, hi-first
ack_seq  := D410 * 65536 + D411
```

### 4.2 Готовность (READY) и безопасность
```
estop      := X3                          ; аппаратный E-stop
guard_open := <вход защиты, если есть>
all_idle   := NOT(AxisX.Busy OR AxisY.Busy OR AxisZ.Busy)
no_error   := (D413 = 0)

IF estop      THEN set bit ESTOP in D414 ELSE clear
IF guard_open THEN set bit GUARD in D414 ELSE clear
IF all_idle AND no_error AND NOT estop AND NOT guard_open
   THEN set bit READY in D414 ELSE clear READY
```

### 4.3 Обнаружение новой команды + дедуп (идемпотентность)
```
new_request := (req_seq <> ack_seq) AND READY

IF new_request THEN
    IF req_seq <= last_processed_seq THEN
        ; уже выполняли (повтор после краша Python) — НЕ двигаться,
        ; просто подтвердить:
        D410 := req_seq >> 16 ; D411 := req_seq AND 16#FFFF
        D412 := 3 (DONE)
    ELSE
        ; новая команда — защёлкнуть payload атомарно (за один скан)
        latched_target := D400 * 65536 + D401
        latched_speed  := D402
        latched_code   := D403
        D412 := 1 (RECEIVED)
        start_dispatch := TRUE
```

### 4.4 Диспетчеризация по command_code
```
opcode := latched_code AND 16#000F
axis   := (latched_code AND 16#00F0) / 16
pulse  := latched_code AND 16#0100

CASE opcode OF
  1 (MoveAbsolute):
        MOV latched_target -> D(abs_pos оси axis)   ; D16 / D106 / D136
        MOV latched_speed  -> D(abs_spd оси axis)   ; D18 / D108 / D138
        IF pulse THEN SET M(abs_pulse_en оси axis)  ; M14 / M24 / M34
                 ELSE RST M(abs_pulse_en оси axis)
        импульс M(abs_exec оси axis)                ; M13 / M23 / M33
  2 (Stop):
        MOV latched_speed -> D(stop_dec оси axis)
        импульс M(stop_exec оси axis)               ; M16 / M26 / M36
  3 (SetPosition):
        MOV latched_target -> D(set_pos_val оси axis); D24 / D114 / D144
        импульс M(set_pos_exec оси axis)            ; M17 / M27 / M37
  4 (Reset):
        импульс M(reset оси axis)                   ; M12 / M22 / M32
END CASE
D412 := 2 (RUNNING)
```

### 4.5 Завершение по факту MC-блока
```
done  := MC-блок выбранной оси .Done
error := MC-блок выбранной оси .Error

IF running AND done THEN
    last_processed_seq := req_seq            ; retentive!
    D410 := req_seq >> 16 ; D411 := req_seq AND 16#FFFF
    D412 := 3 (DONE)
    ; авто-импульс замера, если запрошен — расширяет сеть TPR -> out_pulse -> Y0
IF running AND error THEN
    D413 := <код ошибки MC-блока / D200>
    D410 := req_seq >> 16 ; D411 := req_seq AND 16#FFFF
    D412 := 4 (ERROR)
```

Executor (Python) закрывает команду в БД только при `ack_seq == req_seq &&
state == DONE`; при `ERROR/ABORTED` — уводит в dead-letter; иначе ждёт/повторяет.

---

## 5. Heartbeat и dead-man

### 5.1 Heartbeat ПЛК → Python (D421)
Каждый цикл (или по таймеру 100–500 мс):
```
D421 := (D421 + 1) AND 16#FFFF
```
`ModbusWatchdog` Python видит, что D421 меняется → связь жива. Замер «застывания»
по `watchdog_timeout_s` (по умолч. 3 с).

### 5.2 Dead-man Python → ПЛК (D420)
Python каждый `watchdog_interval_s` (1 с) пишет растущий счётчик в D420.
ПЛК следит за изменением D420; если значение **не менялось дольше таймаута**
(например 3 с) — Python завис/оборвался, перевести привод в безопасное
состояние:
```
TON deadman_timer (PT := 3 c)
deadman_timer.IN := (D420 = prev_D420)     ; не меняется -> считаем
IF D420 <> prev_D420 THEN reset timer ; prev_D420 := D420
IF deadman_timer.Q THEN
    MC_Stop на всех осях ; RST READY ; (опц.) RST MC_Power
```
Это независимая защита: даже если прикладная логика зависнет, застывший
dead-man приведёт привод в безопасное состояние.

---

## 6. Критичные требования (иначе гарантии не держатся)

1. **`last_processed_seq` — энергонезависимый** (retentive D), иначе после
   сброса питания ПЛК потеряет дедуп и сможет повторно выполнить старый seq.
2. **Атомарная защёлка payload**: считать D400…D403 в `latched_*` в том же
   скане, где обнаружен новый `req_seq`. seq Python пишет последним — но
   защёлкивайте именно по факту смены seq, а не по «частям».
3. **Движение только при `req_seq != ack_seq && READY`** — иначе возможен
   двойной запуск.
4. **Сборка/разбор 32-бит — hi-слово первым** (§3), не полагаться на нативный
   порядок double-word.
5. **READY снимать на время движения** (ось Busy) — Executor ждёт READY перед
   отправкой следующей.

---

## 7. Соответствие Python-стороне

| ПЛК | Python (`plc_panel/supervised_backbone.py`) |
|-----|---------------------------------------------|
| D400…D405 командный блок | `HANDSHAKE_REGISTERS.cmd_block_start = 400` |
| D410…D414 статус | `status_block_start = 410` |
| D420 / D421 watchdog | `wd_python_to_plc = 420` / `wd_plc_to_python = 421` |
| command_state коды | `core/models.py::PLC_STATE_BY_CODE` |
| status_flags биты | `core/config.py::FLAG_READY/ESTOP/GUARD_OPEN` |
| command_code | `encode_command_code(opcode, axis, pulse)` |

Параметры тайминга канала (`poll_interval_s`, `command_timeout_s`,
`watchdog_interval_s`, `watchdog_timeout_s`, `max_attempts`) — в
`ChannelConfig` (см. `core/config.py`) и пробрасываются из `AppSettings`.

Проверка Python-стека без железа (эталон `MockPlc` ведёт себя точно по этому
протоколу):

```bash
python -m plc_panel.supervised_demo   # effectively-once после краша/recovery
```

---

## 8. Когда что использовать

- **Вариант A — `queue_backbone.py`** (исполнение на GUI-потоке, ack по
  телеметрии): работает с **текущим** MAIN.LD, не требует доработки ладдера.
- **Вариант B — `supervised_backbone.py`** (этот протокол): включать **после**
  реализации §1–§6 в AutoShop. Даёт настоящий handshake, дедуп на ПЛК, dead-man
  и фоновое исполнение в отдельном потоке — самые строгие гарантии.
