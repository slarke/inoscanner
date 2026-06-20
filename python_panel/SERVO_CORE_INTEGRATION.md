# Интеграция servo_core (`core/`) в python_panel

Сводный документ: как ядро надёжной очереди команд **servo_core** интегрировано
в панель управления сканером. Описывает оба варианта интеграции, архитектурное
решение за ними, инвентарь файлов, карты регистров, сопоставление инвариантов,
запуск и проверку.

Связанные документы:
- [`PLC_LADDER_REQUIREMENTS.md`](PLC_LADDER_REQUIREMENTS.md) — какие блоки добавить
  на лестничной диаграмме (оси Y/Z, выходы Y1/Y2, входы X0…X3, E-stop).
- [`HANDSHAKE_PROTOCOL.md`](HANDSHAKE_PROTOCOL.md) — реализация handshake-контракта
  (ack_seq/command_state/heartbeat) в AutoShop для Easy521.

---

## 1. Контекст и архитектурный конфликт

В репозитории две взаимодополняющие кодовые базы:

- **`core/` (servo_core)** — ядро надёжной очереди: одна SQLite в WAL,
  транзакционные `очередь + история + dead-letter`, монотонный `seq` на канал,
  recovery после краша, двунаправленный watchdog, супервайзер с потоком на канал.
- **`python_panel/`** — PyQt5-панель: реальный Modbus TCP к Inovance-ПЛК
  (`MAIN.LD`), пер-осевые MC-блоки, детекция приезда по телеметрии, SCPI.

**Конфликт контрактов ПЛК.** servo_core построен вокруг *handshake-ПЛК*: он пишет
один командный блок `[target_hi, target_lo, speed, command_code, seq]` и читает
статус `[ack_seq, command_state, error_code, flags]`. Его корректность
(effectively-once, дедуп, recovery, ack-по-факту) **зависит от того, что ПЛК
реализует**: дедуп по `last_processed_seq`, зеркало `ack_seq`, автомат
`command_state`, флаги ready/estop, heartbeat.

`MAIN.LD` **ничего из этого не реализует** — он стробит пер-осевые M-коилы, пишет
пер-осевые D-регистры и детектит приезд опросом фактической позиции. Поэтому
протокольный слой servo_core (`MotionExecutor`/`PlcProtocol`/`PymodbusSerialDriver`)
нельзя «навести» на `MAIN.LD` как есть.

Отсюда — **два варианта интеграции**, оба реализованы:

| | Вариант A — очередь как бэкбон | Вариант B — полный handshake-стек |
|---|---|---|
| Модуль | `queue_backbone.py` | `supervised_backbone.py` |
| Что из core используется | `Database`, `CommandRepository`, `CommandService`, `EventBus` | весь стек: `Supervisor`→`ChannelWorker`→`MotionExecutor` + `ModbusWatchdog` |
| Исполнение | на GUI-потоке, ack по **телеметрии** | в отдельном потоке, ack по **ack_seq** |
| Требует доработки ПЛК | **нет** (работает с текущим MAIN.LD) | **да** (см. HANDSHAKE_PROTOCOL.md) |
| Дедуп после краша | по абсолютной уставке (повтор безопасен) | на ПЛК по `last_processed_seq` |
| Dead-man watchdog | нет | да (D420/D421) |
| Статус | активен в GUI | готов, включается после ладдера |

Оба сосуществуют. GUI сейчас ходит через Вариант A; Вариант B включается после
реализации handshake на ПЛК.

---

## 2. Вариант A — очередь как бэкбон

### 2.1 Идея
Переиспользовать те части servo_core, **корректность которых не зависит** от
отсутствующего handshake: durable-очередь, транзакционные история/DLQ, `seq`,
recovery незавершённых записей. Исполнение остаётся на реальном `PlcWorker`
(строб-коилы + телеметрия), связку делает раннер.

В durable-очередь идут **только абсолютные перемещения** (`MOVE`/`MOVE_PULSE`):
именно они не должны теряться/повторяться в двойное движение (инвариант #4 —
только абсолютные). Шаги оркестрации (`delay`/`scpi`/`pulse`) — это секвенсер,
исполняются инлайн.

### 2.2 Отображение шага в команду
`ScanStep` → frozen `MotionCommand` ядра (без правки `core/`):

| MotionCommand | значение |
|---|---|
| `target_position` | позиция в мкм: `round(pos * 1000)` |
| `speed` | скорость ×1000 |
| `command_code` | `axis \| PULSE_BIT(0x100)` для MOVE_PULSE |
| `operator` | метка оператора |

### 2.3 Поток исполнения (раннер)
```
submit_move(step)             enqueue (ENQUEUED, +history, +seq) — транзакция
   -> claim_next()            PENDING -> IN_FLIGHT — транзакция
   -> move_absolute(...)      строб на реальный ПЛК (SENT)
   -> [телеметрия]            ось в точке (в пределах arrival_tolerance_mm)?
   -> complete()              IN_FLIGHT -> удалить + history=DONE — транзакция (DONE)
   -> advance                 следующий шаг
```
Таймаут хода → `mark_retry` (до `max_move_attempts`), затем `dead_letter`
(сценарий прерывается). `MOVE_PULSE`: ПЛК сам выдаёт импульс по Done (M14), раннер
делает дуэлл.

### 2.4 Recovery (гейтится E-stop)
На каждом (пере)подключении `runner.recover()` сверяет команды, оставшиеся
`IN_FLIGHT` после краша, с живой телеметрией:
- ось уже в целевой точке → `complete` без повторного движения (идемпотентность);
- иначе → переотправка **абсолютной** команды (повтор безопасен);
- пока активен вход E-stop (X3) — движение не возобновляется, ждём
  `acknowledge_safe_state()`.

### 2.5 Сопоставление инвариантов CLAUDE.md (исполнение на реальном железе)
| Инвариант ядра | Как реализован поверх MAIN.LD |
|---|---|
| Транзакционная связность | через `CommandRepository` (очередь+история в одной транзакции) |
| ack по факту | `DONE` только когда телеметрия показала ось в точке |
| Только абсолютные/идемпотентные | в очередь идут лишь `MOVE`/`MOVE_PULSE`; relative — вне очереди |
| Recovery гейтится E-stop | вход X3; `acknowledge_safe_state()` |
| monotonic seq в БД | `CommandRepository` (без изменений) |

### 2.6 Файлы
| Файл | Назначение |
|---|---|
| `plc_panel/queue_backbone.py` | `MotionQueue` — фасад над `Database`+`Repository`+`Service`+`EventBus`; маппинг шага в `MotionCommand`; канал `"SCAN"` |
| `plc_panel/scenario.py` | queue-backed `ScenarioRunner` (enqueue→claim→исполнение→ack по телеметрии→complete; retry/DLQ; recovery) |
| `plc_panel/queue_demo.py` | headless-проверка durability/recovery |
| `config.py` | `AppSettings.queue_db_path`, `move_timeout_ms`, `max_move_attempts` |
| `ui/main_window.py` | строит `MotionQueue`, передаёт раннеру, зовёт `recover()` на `connected` |

БД по умолчанию — `python_panel/scan_queue.db` (рантайм-артефакт).

---

## 3. Вариант B — полный handshake-стек

### 3.1 Идея
Поднять **реальные** `Supervisor` / `MotionExecutor` / `ModbusWatchdog` ядра над
ПЛК, который реализует handshake-контракт. Исполнение уходит в отдельный поток
(`ChannelWorker`), GUI только `submit`-ит и слушает `EventBus`.

### 3.2 Кодировка команды
servo_core шлёт один обобщённый командный блок; ПЛК-диспетчер по `command_code`
выбирает операцию и ось:
```
command_code = opcode | (axis << 4) | PULSE_BIT
    opcode 1 = MoveAbsolute, 2 = Stop, 3 = SetPosition, 4 = Reset
    axis 1=X 2=Y 3=Z       PULSE_BIT = 0x100
```
`target_position`/`speed` — мм-REAL'ы, масштабированные ×1000.

### 3.3 Транспорт
servo_core содержал только RTU-драйвер. Добавлен **`PymodbusTcpDriver`** в
`core/driver.py` (Easy521 по Ethernet), экспортирован из `core/__init__.py`. Тот
же контракт `ModbusDriver`, lazy-импорт pymodbus, lock против watchdog-потока.

### 3.4 Что должен реализовать ПЛК
Полная спецификация ладдера — в [`HANDSHAKE_PROTOCOL.md`](HANDSHAKE_PROTOCOL.md):
командный/статусный блоки (D400…D414), watchdog (D420/D421), автомат
`IDLE→RECEIVED→RUNNING→DONE/ERROR`, дедуп по retentive `last_processed_seq`,
флаги READY/ESTOP/GUARD, heartbeat и dead-man. Исполняемый эталон поведения —
`core/mock.py::MockPlc`.

### 3.5 Файлы
| Файл | Назначение |
|---|---|
| `core/driver.py` | добавлен `PymodbusTcpDriver` |
| `core/__init__.py` | экспорт `PymodbusTcpDriver` |
| `plc_panel/supervised_backbone.py` | `SupervisedBackbone`: `Supervisor`+`Executor`+`Watchdog` над `HANDSHAKE_REGISTERS` с TCP-драйвером; `submit_move`, `acknowledge_safe_state` |
| `plc_panel/supervised_demo.py` | headless-проверка полного стека через `MockPlc` |

---

## 4. Карты регистров

### 4.1 Пер-осевые (Вариант A / отладка / реальные MC-блоки)
REAL = 2 слова, младшее первым. Оси Y/Z приведены к непересекающимся окнам
(прежние черновые карты конфликтовали — `Y.act_pos` накрывал блок ошибок X).

| | X (из MAIN.LD) | Y | Z |
|---|---|---|---|
| Coils | M10–M19 | M20–M29 | M30–M39 |
| Параметры | D10–D25 (+D50) | D100–D117 | D130–D147 |
| Телеметрия pos/vel | D210 / D214 | D230 / D234 | D250 / D254 |
| Ошибки servo/axis | D220 / D222 | D240 / D242 | D260 / D262 |
| Общий ErrorID блоков | D200 | | |

Синхронизированы в `plc_panel/config.py` (`AXES`) и `src_tauri/src/config.rs`.
Детали и чек-лист блоков — в `PLC_LADDER_REQUIREMENTS.md`.

### 4.2 Handshake-интерфейс (Вариант B)
Свободное окно D400+, совпадает с `HANDSHAKE_REGISTERS` в `supervised_backbone.py`:

| Блок | Регистры |
|---|---|
| Командный (Py→ПЛК, FC16) | D400 `target_hi`, D401 `target_lo`, D402 `speed`, D403 `command_code`, D404 `seq_hi`, D405 `seq_lo` |
| Статус (ПЛК→Py, FC03) | D410 `ack_seq_hi`, D411 `ack_seq_lo`, D412 `command_state`, D413 `error_code`, D414 `status_flags` |
| Watchdog | D420 (Py→ПЛК dead-man), D421 (ПЛК→Py heartbeat) |

Коды `command_state`: 0 IDLE, 1 RECEIVED, 2 RUNNING, 3 DONE, 4 ERROR, 5 ABORTED.
Биты `status_flags`: READY 0x01, ESTOP 0x02, GUARD_OPEN 0x04.

> ⚠️ 32-битные значения у servo_core идут **старшим словом первым**; в ладдере
> собирать вручную (`req_seq = D404*65536 + D405`), не нативным `DMOV`.

---

## 5. Запуск и проверка

```bash
cd python_panel
pip install -r requirements.txt

# приложение (Вариант A активен)
python main.py                       # или: python -m plc_panel

# проверка durable-очереди без Qt/железа (Вариант A)
python -m plc_panel.queue_demo       # заканчивается строкой про effectively-once

# проверка полного handshake-стека без Qt/железа (Вариант B, MockPlc)
python -m plc_panel.supervised_demo  # effectively-once после краша/recovery

# синтаксис
python -m compileall plc_panel
```

Результаты проверки (на момент интеграции):
- `queue_demo` — краш до подтверждения → рестарт → recovery без повторного
  движения → 3×DONE.
- `supervised_demo` — 3 команды через `Supervisor`/`Executor` на рабочих потоках;
  краш после отправки → рестарт → recovery закрыл зависшую команду без повторного
  движения (`last_processed_seq` неизменен) = effectively-once.
- `core` импортируется headless (без PyQt), `PymodbusTcpDriver` доступен.

---

## 6. Какой вариант выбрать

- **Вариант A** — по умолчанию: ничего на ПЛК дорабатывать не нужно, работает с
  текущим `MAIN.LD`. Гарантия: ни одна моторная команда не потеряется и не
  выполнится дважды после краша; история и dead-letter персистентны.
- **Вариант B** — после реализации §1–§6 `HANDSHAKE_PROTOCOL.md` в AutoShop. Даёт
  дедуп на самом ПЛК, dead-man-защиту и фоновое исполнение в отдельном потоке —
  самые строгие гарантии.

Чтобы переключить сценарный раннер на Вариант B (submit + ожидание события `DONE`
от `EventBus` вместо детекции по телеметрии), нужна правка `scenario.py` и
`main_window.py` — пока не сделана сознательно (GUI остаётся на Варианте A до
готовности ладдера).

---

## 7. Границы (что НЕ делалось)

- `core/` не менялся по логике; добавлен только `PymodbusTcpDriver` (аддитивно,
  ядро по-прежнему импортируется headless и Qt-агностично).
- Оси Y/Z в `MAIN.LD` физически не реализованы — их карты регистров это
  «контракт вперёд» для будущего ладдера.
- Handshake-регистры на ПЛК пока не существуют — Вариант B проверен только против
  `MockPlc`; на железе включать после доработки AutoShop.
- GUI-переключение между вариантами не реализовано (раннер привязан к Варианту A).
