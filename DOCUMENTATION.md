# Документация Python-части: Inovance XYZ Scanner

Подробное описание Python-кода репозитория: назначение, архитектура, модель
потоков, API каждого модуля, форматы данных, карты регистров, запуск, сборка,
проверка и известные ограничения.

> Документ описывает состояние рабочей копии на 2026-09-29 (ветка `master`,
> включая незакоммиченные изменения). Rust/Tauri-часть (`src/`, `src_tauri/`)
> здесь затрагивается только там, где она делит с Python контракт регистров.

Связанные документы (детали, которые здесь не дублируются):

| Документ | О чём |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Инварианты ядра, которые нельзя нарушать |
| [`core/README.md`](core/README.md) | Краткое описание ядра servo_core |
| [`python_panel/README.md`](python_panel/README.md) | Краткое описание панели (EN) |
| [`python_panel/SERVO_CORE_INTEGRATION.md`](python_panel/SERVO_CORE_INTEGRATION.md) | Как ядро встроено в панель: варианты A и B |
| [`python_panel/HANDSHAKE_PROTOCOL.md`](python_panel/HANDSHAKE_PROTOCOL.md) | Спецификация handshake-протокола для ладдера (вариант B) |
| [`python_panel/PLC_LADDER_REQUIREMENTS.md`](python_panel/PLC_LADDER_REQUIREMENTS.md) | Что добавить в `MAIN.LD`: оси Y/Z, выходы, концевики, E-stop |

---

## Содержание

1. [Обзор системы](#1-обзор-системы)
2. [Структура репозитория](#2-структура-репозитория)
3. [Установка и запуск](#3-установка-и-запуск)
4. [Архитектура](#4-архитектура)
5. [Пакет `core` (servo_core): справочник модулей](#5-пакет-core-servo_core-справочник-модулей)
6. [Пакет `plc_panel`: справочник модулей](#6-пакет-plc_panel-справочник-модулей)
7. [Пользовательский интерфейс](#7-пользовательский-интерфейс)
8. [Карты регистров Modbus](#8-карты-регистров-modbus)
9. [Файлы конфигурации и данных](#9-файлы-конфигурации-и-данных)
10. [Гарантии надёжности и сценарии отказов](#10-гарантии-надёжности-и-сценарии-отказов)
11. [Проверка и тестирование](#11-проверка-и-тестирование)
12. [Сборка дистрибутива (PyInstaller)](#12-сборка-дистрибутива-pyinstaller)
13. [Как расширять](#13-как-расширять)
14. [Известные проблемы и ограничения](#14-известные-проблемы-и-ограничения)
15. [Глоссарий](#15-глоссарий)

---

## 1. Обзор системы

Система управляет трёхосевым (X/Y/Z) сканером антенн на базе ПЛК
**Inovance Easy521** с сервоприводами. Цепочка управления:

```
Оператор ─► GUI (PyQt5) ─► очередь команд (SQLite) ─► Modbus TCP ─► ПЛК ─► сервоприводы
                                   │
                                   └─► SCPI over TCP ─► измерительный прибор (анализатор)
```

Разделение ответственности — фундаментальное решение проекта:

- **ПЛК** исполняет реальное движение: real-time, разгон/торможение, аппаратная
  безопасность (E-stop, концевики).
- **Python** — супервайзер: принимает команды оператора и сценарии, надёжно
  хранит их в очереди, передаёт на ПЛК, подтверждает выполнение по факту,
  восстанавливается после краша/обрыва связи без потери команд и без двойного
  запуска движения.

Python **не является** real-time-слоем: всё, что требует детерминированного
тайминга, должно жить в ПЛК.

В репозитории две Python-кодовые базы:

| Пакет | Каталог | Роль | Qt |
|---|---|---|---|
| **servo_core** | `core/` | Библиотека-ядро надёжной очереди команд: SQLite WAL, seq, handshake, recovery, watchdog | Не зависит (опциональный мост на PyQt6) |
| **plc_panel** | `python_panel/plc_panel/` | Приложение-панель оператора (HMI): телеметрия, ручное управление, сценарии сканирования | PyQt5 |

Панель использует ядро как библиотеку (см. §4.4).

---

## 2. Структура репозитория

```
innovance/
├── CLAUDE.md                     инварианты ядра (читать перед правками)
├── DOCUMENTATION.md              этот документ
├── MAIN.LD                       программа ПЛК (AutoShop, ладдер)
├── PLC_AXES_YZ_REGISTERS.md      заметки по регистрам осей Y/Z
│
├── core/                         ── servo_core: ядро надёжной очереди ──
│   ├── __init__.py               публичный API пакета
│   ├── config.py                 RegisterMap, ChannelConfig, битовые флаги
│   ├── models.py                 доменные модели и Enum состояний
│   ├── database.py               SQLite (WAL), транзакции, схема, миграции
│   ├── repository.py             DAO: очередь + история + DLQ + seq
│   ├── driver.py                 Modbus-транспорт: ABC + RTU + TCP (pymodbus)
│   ├── protocol.py               команда/seq ↔ регистры
│   ├── executor.py               handshake-автомат одной команды
│   ├── watchdog.py               двунаправленный heartbeat
│   ├── supervisor.py             поток на канал, авто-рестарт, E-stop-гейт
│   ├── service.py                фасад для GUI, backpressure
│   ├── events.py                 Qt-агностичная шина событий
│   ├── qt_bridge.py              EventBus → pyqtSignal (PyQt6)
│   ├── mock.py                   эмулятор ПЛК + mock-драйвер
│   ├── demo.py                   headless-демо (штатный путь + краш)
│   └── pyqt_example.py           пример GUI на PyQt6
│
├── python_panel/                 ── приложение оператора ──
│   ├── main.py                   точка входа `python main.py`
│   ├── requirements.txt          PyQt5, pymodbus
│   ├── app_config.json           пользовательские настройки
│   ├── InovanceScanner.spec      сборка PyInstaller (актуальная)
│   ├── main.spec                 сборка PyInstaller (автосгенерированная, устаревшая)
│   ├── scan_sequence*.json       примеры сценариев сканирования
│   ├── *.md                      интеграция, handshake, требования к ладдеру
│   └── plc_panel/
│       ├── __init__.py, __main__.py
│       ├── paths.py              каталог пользовательских файлов
│       ├── config.py             карты регистров осей/IO, AppSettings
│       ├── codec.py              float ↔ 2 слова Modbus (REAL)
│       ├── models.py             Telemetry, ScanStep, StepType
│       ├── modbus_worker.py      весь Modbus I/O в отдельном QThread
│       ├── service.py            сигнальный фасад PlcService для UI
│       ├── queue_backbone.py     MotionQueue — очередь servo_core (вариант A)
│       ├── scenario.py           ScenarioRunner — исполнитель сценария
│       ├── session.py            ScanSession — резюме сценария после краша
│       ├── supervised_backbone.py полный стек servo_core (вариант B)
│       ├── scpi.py               SCPI-клиент
│       ├── logger.py             файловый журнал
│       ├── queue_demo.py         headless-проверка варианта A
│       ├── supervised_demo.py    headless-проверка варианта B
│       └── ui/
│           ├── main_window.py    главное окно (4 вкладки + журнал)
│           ├── axis_panel.py     карточка отладки оси
│           ├── queue_widget.py   редактор сценария с drag-and-drop
│           ├── charts.py         XY-плоскость и линейные шкалы (QPainter)
│           ├── log_dock.py       плавающий журнал
│           ├── common.py         фабрики виджетов
│           └── styles.py         палитра и QSS
│
├── src/, src_tauri/              альтернативный клиент на Tauri (Rust + JS)
```

---

## 3. Установка и запуск

### 3.1 Требования

- Windows 10/11 (целевая платформа; код кроссплатформенный).
- Python 3.10+ (проверено на 3.11.9; используется синтаксис `X | None`).
- `PyQt5>=5.15`, `pymodbus>=3.0` — для панели.
- `PyQt6` — только для `core/qt_bridge.py` и `core/pyqt_example.py` (опционально).

> **Версия pymodbus.** Драйверы ядра (`core/driver.py`) передают адрес
> устройства как `device_id=` — это имя kwarg в pymodbus 3.13.x (в более
> ранних версиях было `unit=` / `slave=`). `requirements.txt` панели
> разрешает `pymodbus>=3.0`, но в `core/README.md` рекомендуется пин
> `pymodbus==3.13.1`. Панель (`modbus_worker.py`) адрес устройства не передаёт
> и от этого не зависит; вариант B (`supervised_backbone.py`) — зависит.

### 3.2 Установка

```bash
cd python_panel
pip install -r requirements.txt
```

### 3.3 Запуск приложения

```bash
cd python_panel
python main.py            # или: python -m plc_panel
```

При старте:
1. Загружается `app_config.json` (создаётся с умолчаниями, если его нет).
2. Создаются `PlcService` (с рабочим потоком Modbus) и `MainWindow`.
3. Открывается/создаётся очередь `scan_queue.db`.
4. Если найден `scan_session.json` (сценарий прерван крашем) — он загружается,
   и продолжение запланировано на момент подключения к ПЛК.
5. Если `auto_connect = true` — сразу устанавливается связь с ПЛК.

### 3.4 Headless-проверки (без железа и без Qt)

```bash
cd python_panel
python -m plc_panel.queue_demo        # вариант A: durable-очередь + recovery
python -m plc_panel.supervised_demo   # вариант B: полный handshake-стек на MockPlc
python -m compileall plc_panel        # проверка синтаксиса
```

Оба демо должны завершаться строкой про **effectively-once**.

> ⚠️ Команды `python -m servo_core.demo` / `python -m servo_core.pyqt_example`
> из `CLAUDE.md` и `core/README.md` **сейчас не работают**: пакет лежит в
> каталоге `core/`, а `core/demo.py` и `core/pyqt_example.py` импортируют
> `servo_core.*` → `ModuleNotFoundError: No module named 'servo_core'`.
> См. §14.1. Рабочая проверка ядра — `plc_panel.supervised_demo`, который
> гоняет тот же сценарий через настоящие `Supervisor`/`MotionExecutor`.

---

## 4. Архитектура

### 4.1 Слои ядра и правило зависимостей

```
             ┌──────────────┐
  GUI ─────► │ CommandService│  submit(), backpressure
             └──────┬───────┘
                    │ enqueue
             ┌──────▼────────┐        ┌──────────┐
             │CommandRepository│◄────►│ Database │  SQLite WAL, одна БД
             └──────▲────────┘        └──────────┘
                    │ claim / complete / dead_letter / mark_retry
  ┌─────────────────┴──────────────────────────────┐
  │ Supervisor ─► ChannelWorker (поток на канал)   │
  │                 ├─ MotionExecutor ─► PlcProtocol│
  │                 ├─ ModbusWatchdog (свой поток)  │
  │                 └─ ModbusDriver (ABC)           │
  └─────────────────────────┬──────────────────────┘
                            │ реализации
          PymodbusSerialDriver │ PymodbusTcpDriver │ MockModbusDriver
                            
  EventBus ◄── publish от всех слоёв ──► подписчики (QtEventBridge, логгер, тест)
```

**Зависимости направлены внутрь.** `MotionExecutor` знает только абстракции
`ModbusDriver` и `CommandRepository`. Ядро не импортирует PyQt и pymodbus на
уровне модулей (pymodbus импортируется лениво внутри `connect()`), поэтому
`import core` работает headless, а ядро тестируется на `MockPlc`.

### 4.2 Слои панели

```
MainWindow (GUI-поток)
   ├─ PlcService ──(queued signals)──► PlcWorker (QThread) ──► ModbusTcpClient ──► ПЛК
   │      ▲                                   │
   │      └──────── telemetry / log / connected / reconnecting ◄┘
   ├─ ScenarioRunner (GUI-поток) ──► MotionQueue ──► core: Database/Repository/Service/EventBus
   │                                  (scan_queue.db)
   ├─ ScanSession  (scan_session.json)
   ├─ FileLogger   (log/plc_panel_*.log)
   └─ виджеты: ScenarioQueueWidget, AxisDebugPanel, XYPlotWidget, LogDock
```

### 4.3 Модель потоков

| Поток | Кто | Что делает | Что ему запрещено |
|---|---|---|---|
| GUI (главный) | `MainWindow`, `ScenarioRunner`, `MotionQueue` | Отрисовка, сценарий, доступ к `scan_queue.db` | Modbus I/O |
| `QThread` воркера | `PlcWorker` | Единственный владелец `ModbusTcpClient`: опрос, команды, SCPI | Трогать виджеты, БД |
| `worker-<канал>` (вариант B) | `ChannelWorker` | Handshake с ПЛК, recovery | — |
| `wd-<канал>` (вариант B) | `ModbusWatchdog` | Heartbeat/dead-man | Обработка команд |

Связь GUI ↔ воркер идёт **только через сигналы с `Qt.QueuedConnection`**:
методы `PlcService` испускают приватные request-сигналы, которые выполняются в
потоке воркера; результаты возвращаются сигналами в GUI-поток. Поэтому GUI
никогда не блокируется сетью, а сокет pymodbus никогда не используется из двух
потоков одновременно.

`Database` хранит соединение SQLite **per-thread** (`threading.local`), поэтому
каждый поток ядра получает своё соединение; в панели БД трогает только
GUI-поток.

### 4.4 Два варианта интеграции ядра в панель

Ядро servo_core спроектировано под **handshake-ПЛК** (зеркальные регистры
`ack_seq`, `command_state`, флаги READY/ESTOP, heartbeat). Текущий `MAIN.LD`
этого не реализует — он управляется пер-осевыми строб-коилами MC-блоков.
Поэтому существуют два варианта:

| | **Вариант A** — очередь как бэкбон | **Вариант B** — полный стек |
|---|---|---|
| Модуль | `queue_backbone.py` + `scenario.py` | `supervised_backbone.py` |
| Используется из ядра | `Database`, `CommandRepository`, `CommandService`, `EventBus` | всё: `Supervisor`, `MotionExecutor`, `ModbusWatchdog`, `PymodbusTcpDriver` |
| Исполнитель | `ScenarioRunner` на GUI-потоке через `PlcWorker` | `ChannelWorker` в своём потоке |
| Подтверждение | ось в целевой точке по телеметрии | `ack_seq == seq && state == DONE` |
| Дедуп после краша | идемпотентность абсолютной уставки | на ПЛК по `last_processed_seq` |
| Dead-man watchdog | нет | да (D420/D421) |
| Нужна доработка ПЛК | нет | да, см. `HANDSHAKE_PROTOCOL.md` |
| Статус | **активен в GUI** | готов, проверен на `MockPlc`, в GUI не подключён |

Подробно — в `python_panel/SERVO_CORE_INTEGRATION.md`.

---

## 5. Пакет `core` (servo_core): справочник модулей

Публичный API (`core/__init__.py`): `ChannelConfig`, `RegisterMap`, `Database`,
`ModbusDriver`, `ModbusError`, `PymodbusSerialDriver`, `PymodbusTcpDriver`,
`Event`, `EventBus`, `EventType`, `CommandState`, `DeadLetterReason`,
`MotionCommand`, `PlcCommandState`, `QueuedCommand`, `CommandRepository`,
`CommandService`, `QueueFullError`, `ChannelWorker`, `Supervisor`.

### 5.1 `config.py` — карта регистров и параметры канала

Битовые маски регистра `status_flags`:

| Константа | Значение | Смысл |
|---|---|---|
| `FLAG_READY` | `0x0001` | ПЛК готов принять команду |
| `FLAG_ESTOP` | `0x0002` | Активен аварийный стоп |
| `FLAG_GUARD_OPEN` | `0x0004` | Открыто защитное ограждение |

`RegisterMap` (frozen dataclass) — адреса одного ПЛК:

| Поле | По умолчанию | Назначение |
|---|---|---|
| `cmd_block_start` / `cmd_block_length` | `0x0000` / 6 | Блок команды `[target_hi, target_lo, speed, command_code, seq_hi, seq_lo]`, пишется одним FC16 |
| `status_block_start` / `status_block_length` | `0x0100` / 5 | Блок статуса `[ack_seq_hi, ack_seq_lo, command_state, error_code, status_flags]`, читается одним FC03 |
| `wd_python_to_plc` | `0x0200` | Dead-man: Python инкрементирует |
| `wd_plc_to_python` | `0x0201` | Heartbeat: ПЛК инкрементирует |

Значения по умолчанию условные; для Easy521 используется
`HANDSHAKE_REGISTERS` (D400…D421, §8.3).

`ChannelConfig` (frozen dataclass) — параметры канала:

| Поле | По умолч. | Смысл |
|---|---|---|
| `name` | — | Логическое имя канала (ключ в БД) |
| `port` | — | COM-порт или host для TCP |
| `unit_id` | 1 | Адрес устройства Modbus |
| `baudrate` | 19200 | Для RTU |
| `registers` | `RegisterMap()` | Карта регистров |
| `poll_interval_s` | 0.05 | Период опроса статуса при ожидании |
| `command_timeout_s` | 30.0 | Таймаут ожидания READY и выполнения команды |
| `watchdog_interval_s` | 1.0 | Период работы watchdog |
| `watchdog_timeout_s` | 3.0 | Застывание heartbeat ПЛК → потеря связи |
| `max_attempts` | 3 | Попыток до dead-letter |
| `max_pending` | 100 | Лимит длины очереди (backpressure) |
| `reconnect_backoff_s` | 2.0 | Пауза перед переподключением канала |

### 5.2 `models.py` — доменные модели

- **`CommandState`** — состояние команды на стороне Python:
  `PENDING` → `IN_FLIGHT` → `DONE` | `FAILED`.
- **`PlcCommandState`** — зеркало автомата ПЛК: `IDLE, RECEIVED, RUNNING,
  DONE, ERROR, ABORTED, UNKNOWN`. Кодировка регистра — `PLC_STATE_BY_CODE`
  (`0..5`), обратная — `PLC_CODE_BY_STATE`. Неизвестный код → `UNKNOWN`.
- **`DeadLetterReason`** — `PLC_ERROR`, `TIMEOUT_LIMIT`, `INVALID_COMMAND`,
  `SAFETY_LOCK`.
- **`MotionCommand`** (frozen) — полезная нагрузка: `target_position: int`
  (**абсолютная** уставка), `speed: int`, `command_code: int`,
  `operator: str = "system"`. Сериализация `to_payload()` / `from_payload()`
  в JSON-словарь.
- **`QueuedCommand`** (frozen) — команда в очереди: `id, seq, channel, command,
  state, attempts, created_at`.

### 5.3 `database.py` — SQLite

`Database(path)`:

- Режим **WAL**, `synchronous=FULL` (надёжность важнее скорости),
  `busy_timeout=30000`, `isolation_level=None` (ручное управление транзакциями).
- Соединение **на поток** (`threading.local`), свойство `conn`.
- `transaction()` — контекст-менеджер `BEGIN IMMEDIATE … COMMIT`, при исключении
  `ROLLBACK` и проброс.
- `_bootstrap()` создаёт схему и записывает `schema_version`; `_migrate()` —
  точка расширения (сейчас `SCHEMA_VERSION = 1`; при версии БД новее кода
  выбрасывается `RuntimeError`).

Схема:

| Таблица | Ключевые поля | Назначение |
|---|---|---|
| `schema_version` | `version` | Версия схемы |
| `sequence_generator` | `channel` PK, `last_seq` | Монотонный seq на канал |
| `command_queue` | `id`, `seq`, `channel`, `payload` (JSON), `state`, `attempts`, `created_at`, `updated_at` | Живая очередь (PENDING/IN_FLIGHT) |
| `command_history` | `seq`, `channel`, `command_type`, `payload`, `status`, `error_text`, `created_at`, `started_at`, `completed_at` | Аудит всех команд |
| `dead_letter` | `seq`, `channel`, `payload`, `reason`, `error_text`, `created_at` | Отказавшие команды |

Индексы: `idx_queue_channel_state(channel, state, id)`,
`idx_history_channel_seq(channel, seq)`.

### 5.4 `repository.py` — DAO

`CommandRepository(db)`. **Каждый** метод, меняющий очередь, выполняет изменение
очереди и истории/DLQ **в одной транзакции** (инвариант №1).

| Метод | Транзакция | Что делает |
|---|---|---|
| `enqueue(channel, command)` | да | seq += 1; INSERT в очередь (`PENDING`) и в историю |
| `claim_next(channel)` | да | Старейшая `PENDING` → `IN_FLIGHT`; история: `IN_FLIGHT`, `started_at`. `None`, если пусто |
| `resume_in_flight(channel)` | чтение | Все `IN_FLIGHT` (для recovery) |
| `complete(q)` | да | DELETE из очереди; история: `DONE`, `completed_at` |
| `dead_letter(q, reason, text)` | да | DELETE из очереди; INSERT в `dead_letter`; история: `FAILED` |
| `mark_retry(q)` | да | Обратно в `PENDING`, `attempts += 1`; возвращает новое `attempts` |
| `pending_count(channel)` | чтение | Число `PENDING` |
| `history(channel, limit=100)` | чтение | Последние записи истории (новые первыми), `list[dict]` |
| `dead_letters(channel, limit=100)` | чтение | Последние записи DLQ |

Жизненный цикл записи:

```
enqueue ─► PENDING ─claim_next─► IN_FLIGHT ─complete────► (удалена)   history=DONE
              ▲                      │     └dead_letter─► (удалена)   history=FAILED, +dead_letter
              └──────mark_retry──────┘
```

### 5.5 `driver.py` — транспорт

`ModbusDriver` (ABC): `connect()`, `close()`, `is_connected()`,
`read_holding(address, count)` (FC03), `write_registers(address, values)`
(FC16, **одной транзакцией**), `write_register(address, value)` (FC06).
Ошибки транспорта — `ModbusError`.

Реализации:

- `PymodbusSerialDriver(port, unit_id=1, baudrate=19200, timeout=1.0)` — RTU.
- `PymodbusTcpDriver(host, port=502, unit_id=1, timeout=1.0)` — Modbus TCP
  (Easy521 по Ethernet).

Общее: `retries=0` (повторы — политика верхнего уровня), ленивый импорт
pymodbus, внутренний `threading.Lock` защищает только от конкурентного
обращения watchdog-потока **того же** канала. Драйвер не содержит бизнес-логики.

### 5.6 `protocol.py` — кодирование регистров

- `u32_to_regs(value) -> (hi, lo)` и `regs_to_u32(hi, lo)` — 32 бита, **старшее
  слово первым** (в отличие от REAL панели, где младшее первым — §8.4).
- `PlcStatus(ack_seq, state, error_code, flags)` со свойствами `ready`,
  `estop`, `guard_open`, `safe_to_move`.
- `PlcProtocol(registers)`:
  - `encode_command(command, seq)` → `[pos_hi, pos_lo, speed, code, seq_hi, seq_lo]`
    (seq последним — ПЛК не увидит «рваную» команду);
  - `decode_status(regs)` → `PlcStatus`.

### 5.7 `executor.py` — handshake-автомат

`MotionExecutor(cfg, driver, protocol, repo, events)`:

- `process_one()` — взять одну команду (`claim_next`) и провести её; `False`,
  если очередь пуста.
- `recover()` — однократно при старте: для каждой `IN_FLIGHT` публикует
  `RECOVERY` и прогоняет тот же автомат.

Автомат `_run(q)`:

```
read_status
 ├─ ESTOP или GUARD_OPEN ───────────────────► dead_letter(SAFETY_LOCK)
 ├─ ack_seq == q.seq ?
 │    ├─ DONE ───────────────────────────────► complete  (ПЛК уже выполнил — без повтора)
 │    ├─ ERROR/ABORTED ──────────────────────► dead_letter(PLC_ERROR)
 │    └─ RUNNING/RECEIVED ───────────────────► ждать завершения
 └─ иначе: wait_ready ─► send (FC16) ─► SENT ─► wait_completion
                                                ├─ ack==seq && DONE ─► complete
                                                ├─ ack==seq && ERROR ► dead_letter(PLC_ERROR)
                                                └─ таймаут ──────────► retry / dead_letter(TIMEOUT_LIMIT)
ModbusError на любом шаге ────────────────────► retry / dead_letter(TIMEOUT_LIMIT)
```

Retry: если `attempts + 1 >= max_attempts` — dead-letter, иначе `mark_retry`
(команда вернётся в `PENDING` и будет взята снова).

### 5.8 `watchdog.py` — двунаправленный контроль связи

`ModbusWatchdog(cfg, driver, events)` в отдельном потоке каждые
`watchdog_interval_s`:

1. Инкрементирует счётчик и пишет его в `wd_python_to_plc` (dead-man для ПЛК).
2. Читает `wd_plc_to_python`. Если значение изменилось — связь жива; если не
   менялось дольше `watchdog_timeout_s` — «heartbeat ПЛК застыл».

Свойство `connection_ok`; при смене состояния публикуются
`CONNECTION_LOST` / `CONNECTION_RESTORED`. `ModbusError` при обмене → потеря
связи.

### 5.9 `supervisor.py` — поток на канал

`ChannelWorker(cfg, driver, repo, events)`:

- `start()` / `stop()` — жизненный цикл потока `worker-<name>`.
- Цикл `_run` с **авто-перезапуском**: `connect` → `watchdog.start` →
  `_safe_recovery` → `_main_loop`. Любое исключение (`ModbusError` или иное)
  превращается в событие `CONNECTION_LOST`, драйвер закрывается, через
  `reconnect_backoff_s` — новая попытка. Ошибка одного канала не влияет на
  другие (инвариант №9).
- `_safe_recovery` — **E-stop-гейт**: если ПЛК в ESTOP/GUARD, recovery не
  выполняется, публикуется `ESTOP`, канал блокируется до
  `acknowledge_safe_state()` (инвариант №5).
- `acknowledge_safe_state()` только выставляет запрос; разблокировку и
  recovery выполняет поток канала (§14.6).
- `_main_loop` — обрабатывает запрос подтверждения; пока канал не
  заблокирован и watchdog считает связь живой, вызывает
  `executor.process_one()`.

`Supervisor(repo, events)`: `add_channel(cfg, driver)`, `start()`, `stop()`,
`acknowledge_safe_state(channel)`.

### 5.10 `service.py` — фасад для GUI

`CommandService(repo, events, max_pending=100)`:

- `submit(channel, command)` — если `pending_count >= max_pending`, бросает
  `QueueFullError` (backpressure), иначе `enqueue` и событие `ENQUEUED`.
- `pending_count`, `history`, `dead_letters` — проксируют репозиторий.

### 5.11 `events.py` — шина событий

`EventType`: `ENQUEUED, SENT, DONE, RETRY, DEAD_LETTER, RECOVERY,
CONNECTION_LOST, CONNECTION_RESTORED, ESTOP`.

`Event(type, channel="", seq=0, detail="", payload=None)` — frozen, с
фабриками `enqueued/sent/done/retry/dead_letter/recovery`.

`EventBus` — потокобезопасный pub/sub. `publish` вызывает подписчиков
синхронно **в потоке публикующего**; исключения подписчиков подавляются, чтобы
не ронять ядро.

### 5.12 `qt_bridge.py` — мост в Qt (PyQt6)

`QtEventBridge(event_bus)` — `QObject`, превращающий события в сигналы
`command_enqueued/sent/done(str, int)`, `command_retry/dead_letter(str, int, str)`,
`recovery_started(str, int)`, `connection_lost/restored(str, str)`,
`estop_detected(str, str)`. Создавать в GUI-потоке — тогда доставка в слоты
идёт queued-соединением. Без PyQt6 модуль импортируется, но конструктор
бросает `RuntimeError`.

> Панель написана на **PyQt5**, поэтому этот мост в ней не используется
> (панель подписывается на `EventBus` напрямую — см. `ScenarioRunner._on_core_event`).

### 5.13 `mock.py` — эмулятор ПЛК

- `MockPlc(registers, move_time=0.2)` — память регистров в словаре, автомат
  состояний `IDLE→RECEIVED→RUNNING→DONE`, **дедуп** по `last_processed_seq`
  (повтор уже выполненного seq игнорируется), heartbeat каждые 0.4 с,
  `trigger_estop()` / `clear_estop()`. Это исполняемый эталон поведения,
  которого ждёт `HANDSHAKE_PROTOCOL.md`.
- `MockModbusDriver(plc)` — тот же контракт, что у реального драйвера.

### 5.14 `demo.py`, `pyqt_example.py`

Демонстрации: штатное прохождение трёх команд и краш между отправкой и
подтверждением с проверкой, что recovery не запустил движение второй раз;
GUI-пример на PyQt6 с `QtEventBridge`. См. предупреждение в §3.4 об импортах.

---

## 6. Пакет `plc_panel`: справочник модулей

### 6.1 `__main__.py`, `main.py`

`main()` создаёт `QApplication`, применяет `STYLESHEET`, загружает
`AppSettings.load()`, создаёт `PlcService` и `MainWindow`, запускает цикл
событий. `main.py` — обёртка для `python main.py` и для PyInstaller.

### 6.2 `paths.py`

`app_base_dir()` — каталог пользовательских файлов (`app_config.json`, `log/`,
`scan_queue.db`, `scan_session.json`):
- из исходников — `python_panel/`;
- в сборке PyInstaller (`sys.frozen`) — каталог рядом с `.exe` (бандл
  read-only/временный).

### 6.3 `config.py`

- **`AxisRegisters`** — адреса одной оси: коилы команд (`power`, `vel_exec`,
  `reset`, `abs_exec`, `abs_pulse_en`, `read_error_en`, `stop_exec`,
  `set_pos_exec`, `rel_exec`), параметры (`vel_block`, `set_pos_val`,
  `abs_pos`, `abs_spd`, `stop_dec`, `rel_distance`, `rel_speed`) и телеметрия
  (`act_pos`, `act_vel`, `error_block`).
- **`AXES: Dict[int, AxisRegisters]`** — 1=X, 2=Y, 3=Z (§8.1).
- **`AxisSafety`** / **`AXIS_SAFETY`** — концевики и защёлка блокировки по
  осям (§8.2).
- **`IO`** — общие адреса: `BLOCK_ERROR_ID` (D200), выходы Y0/Y1/Y2 (M50–M53,
  D90), `INPUT_X0` (M100), окно концевиков `LIMIT_INPUT_BASE/COUNT`
  (вычисляется из `AXIS_SAFETY`).
- **`GridLimits`** — границы отображения XY-графика и шкал (мм).
- **`AppSettings`** — настройки приложения (§9.1) с `load()`, `save()`,
  `to_config_dict()`. `load()` создаёт файл с умолчаниями, если его нет;
  неизвестные номера осей в `active_axes` отбрасываются, пустой список → все оси.

### 6.4 `codec.py`

REAL (IEEE-754 float32) ↔ два 16-битных слова, **младшее слово первым**:
`float_to_words(v) -> [lo, hi]`, `words_to_float([lo, hi])` (0.0 при
некорректной длине), `floats_to_words(*vals)` — склейка нескольких REAL.

### 6.5 `models.py`

- `AxisTelemetry` — `position`, `velocity`, сырые слова, `servo_error`,
  `axis_error`, свойство `has_error`.
- `Telemetry` — снимок машины: `axes`, `block_error`, `inputs[4]`,
  `outputs[4]`, `power{ось: bool|None}`, `limit_inputs{адрес: bool|None}`,
  `blocked{ось: bool|None}`. `None` означает «не удалось прочитать».
- `StepType` — `move`, `move_pulse`, `delay`, `scpi`, `pulse`.
- `ScanStep` — шаг сценария (`type, axis, pos, spd, ms, cmd`), `is_move`,
  `to_dict()` / `from_dict()` (неизвестный тип → `None`, шаг пропускается).

### 6.6 `modbus_worker.py` — `PlcWorker`

Живёт в `QThread`, единолично владеет `ModbusTcpClient`.

**Сигналы:** `telemetry(Telemetry)`, `connected(str)`, `connection_failed(str)`,
`reconnecting(str)`, `disconnected()`, `command_error(str)`, `log(str)`.

**Подключение и авто-переподключение:**
- `open(ip, port, interval_ms)` запоминает цель (`interval ≥ 20 мс`) и делает
  попытку. При неудаче — `reconnecting` и повтор каждые
  `_RECONNECT_INTERVAL_MS = 2000` мс, пока оператор не нажмёт «отключить».
- При успехе включаются коилы `MC_ReadAxisError` всех осей, запускается
  `QTimer` опроса.
- Потеря связи фиксируется, если сокет закрыт или опрос упал
  `_POLL_FAILURE_LIMIT = 3` раза подряд (единичный сбой → `command_error`).
  Это покрывает «полуоткрытый» сокет после включения/смены VPN.
- `close()` отключает авто-переподключение и закрывает клиента.

**Опрос телеметрии (`_poll`)** за один тик читает: D200; по каждой оси
позицию, скорость, блок ошибок и коил `MC_Power`; выходы M50–M53; «входы»
M100–M103; концевики X1–X6 (FC02); защёлки блокировки M100/M200/M300. Чтения
питания, концевиков и блокировок **толерантны** — их отказ даёт `None`, но не
роняет весь опрос.

**Команды (слоты)** — все протоколируются в журнал вплоть до записей
отдельных регистров (`↳ M13 ← 1`, `↳ D16 ← [...] (≈ 50.000)`):

| Слот | Последовательность Modbus |
|---|---|
| `set_power(axis, on)` | коил `power` = on (уровень) |
| `reset_axis(axis)` | строб `reset` 100 мс |
| `move_absolute(axis, pos, spd, auto_pulse)` | `abs_pulse_en`=auto_pulse → 10 мс → REAL `abs_pos`, `abs_spd` → 20 мс → строб `abs_exec` 40 мс |
| `move_relative(axis, dist, spd)` | REAL `rel_distance`, `rel_speed` → 20 мс → строб `rel_exec` 40 мс |
| `move_velocity(axis, spd, acc, dec)` | 3×REAL в `vel_block` → 50 мс → строб `vel_exec` 100 мс |
| `stop_axis(axis, decel)` | REAL `stop_dec` → 20 мс → строб `stop_exec` 40 мс |
| `set_position(axis, pos)` | REAL `set_pos_val` → 20 мс → строб `set_pos_exec` 40 мс |
| `pulse_trigger()` | строб M50 (SET Y0) 50 мс → 150 мс → строб M51 (RST Y0) 50 мс |
| `set_y0(on)` | постоянный уровень: удержание M50 или M51 |
| `set_output(on)` | M52 (Y1) = on |
| `set_frequency(enabled, freq)` | REAL D90 → 20 мс → M53 = enabled |
| `reset_block(axis)` | строб `block_reset` 100 мс |
| `send_scpi(cmd, ip, port)` | см. `scpi.py` |

«Строб» = передний фронт: коил в 1, удержание, коил в 0. Паузы блокируют поток
воркера (не GUI), поэтому команды и опрос сериализованы.

### 6.7 `service.py` — `PlcService`

Потокобезопасный фасад для UI. Создаёт `QThread`, переносит туда `PlcWorker`,
соединяет приватные request-сигналы со слотами воркера через
`Qt.QueuedConnection` и ретранслирует сигналы-результаты.

Состояние: `is_connected` — фактическое состояние линка; `is_active` —
намерение оператора (нажал «подключить»). Кнопка связи переключается по
`is_active`, чтобы отменять и идущий авто-реконнект.

Публичные методы: `connect`, `disconnect`, `set_power`, `reset_axis`,
`move_absolute`, `move_relative`, `move_velocity`, `stop_axis`, `set_position`,
`pulse_trigger`, `reset_block`, `set_y0`, `set_output`, `set_frequency`,
`send_scpi`, `shutdown` (вызывать один раз при выходе).

### 6.8 `queue_backbone.py` — `MotionQueue` (вариант A)

Добавляет корень репозитория в `sys.path` и импортирует `core` (единый
источник истины, без упаковки).

- Один канал `CHANNEL = "SCAN"` — сценарий это одна упорядоченная
  последовательность по всем осям, одна монотонная нумерация seq.
- В очередь попадают **только** `MOVE` / `MOVE_PULSE` (абсолютные).
- Отображение `ScanStep → MotionCommand`:

  | Поле | Значение |
  |---|---|
  | `target_position` | `round(pos * 1000)` (мкм) |
  | `speed` | `round(spd * 1000)` |
  | `command_code` | `axis \| 0x100` для MOVE_PULSE |
  | `operator` | метка, по умолчанию `"scan"` |

- `decode_move(q) -> Move(axis, pos, spd, pulse)` — обратное преобразование.
- `MotionQueue(db_path, max_pending=500)`: `submit_move`, `claim_next`,
  `notify_sent`, `complete`, `mark_retry`, `dead_letter`, `resume_in_flight`,
  `pending_count`, `history`, `dead_letters`, атрибут `events: EventBus`.
  Переходы SENT/DONE/RETRY/DEAD_LETTER публикуются здесь, так как исполнитель —
  раннер, а не `MotionExecutor`.

### 6.9 `scenario.py` — `ScenarioRunner`

Исполнитель сценария сканирования на GUI-потоке.

**Сигналы:** `step_changed(int)` (индекс активного шага, `-1` — простой),
`finished()`, `log(str)`.

**Управление:** `start(steps, start_index=0)`, `pause()`, `resume()`, `stop()`,
`acknowledge_safe_state()`, `recover()`; свойства `is_active`, `is_paused`,
`current_index`.

**Запуск.** `start()` включает Servo ON на всех `active_axes`, сохраняет
сессию и через `_SERVO_ENABLE_DELAY_MS = 500` мс выполняет первый шаг.

**Исполнение шагов:**

| Тип | Поведение |
|---|---|
| `move` / `move_pulse` | Если ось не в `active_axes` — шаг пропускается. Иначе `submit_move` → `claim_next` → `move_absolute` → `notify_sent` → таймер `move_timeout_ms` |
| `delay` | `QTimer.singleShot(ms)` |
| `scpi` | `send_scpi` + 150 мс |
| `pulse` | `pulse_trigger` (Y0) + 350 мс |

**Подтверждение по факту.** На каждом кадре телеметрии: если
`|position − target| < arrival_tolerance_mm` — таймер останавливается,
`complete()` (одна транзакция), переход к следующему шагу. Для `move_pulse`
перед переходом выдерживается 200 мс — ПЛК сам выдаёт импульс по Done (M14).

**Таймаут хода** считается на каждый ход: `move_timeout_ms` + оценка времени
хода `|target − позиция| / spd` × 1.5 (см. §14.4). Если ось не пришла за это
время: при `attempts + 1 < max_move_attempts` — `mark_retry` и повторная
отправка той же абсолютной команды; иначе — dead-letter и прерывание сценария.

**Пауза** срабатывает на границе шага: текущий шаг доигрывается, раннер
«паркуется» на следующем индексе, `resume()` продолжает с него.

**Стоп.** Команда, взятая в работу, намеренно остаётся `IN_FLIGHT` в БД — её
разберёт `recover()`; раннер никогда не закрывает ход, который ПЛК мог не
завершить.

**Recovery** (вызывается на каждом `connected`):
1. Берёт все `IN_FLIGHT` из прошлого запуска.
2. Ждёт первый кадр телеметрии.
3. Пока активен E-stop (`telemetry.inputs[3]`) — не двигается, пока оператор
   не вызовет `acknowledge_safe_state()`.
4. Команды, у которых ось уже в целевой точке, закрывает без движения;
   первую недоехавшую переотправляет (абсолютная — повтор безопасен), по её
   приезду разбирает остальные.

Recovery и сценарий взаимоисключающие: `start()` во время recovery
откладывается до его завершения, `stop()` отменяет и то и другое (§14.5).

События очереди (`ENQUEUED`, `SENT`, …) зеркалируются в журнал оператора.

### 6.10 `session.py` — `ScanSession`

Снимок выполняемого сценария в `scan_session.json`: `{"running": true,
"index": N, "steps": [...]}`. `begin()` при старте, `update(index)` при каждом
шаге, `end()` (удаление файла) при завершении/стопе/прерывании — файл
переживает только настоящий краш. `load()` возвращает `(steps, index)` или
`None`. Запись best-effort: это удобство, а не гарант корректности (гарантии
даёт очередь).

### 6.11 `supervised_backbone.py` — `SupervisedBackbone` (вариант B)

Поднимает настоящий `Supervisor` над ПЛК с handshake-интерфейсом.

- Канал `CHANNEL = "EASY521"`, карта `HANDSHAKE_REGISTERS` (D400…D421).
- `command_code = opcode | (axis << 4) | 0x100·pulse`; опкоды:
  `OP_MOVE_ABS=1`, `OP_STOP=2`, `OP_SET_POSITION=3`, `OP_RESET=4`;
  `encode_command_code(opcode, axis, pulse)`.
- `ChannelConfig` строится из `AppSettings` (`poll_interval_ms`,
  `move_timeout_ms`, `max_move_attempts`).
- Отдельная БД: `supervised_queue.db` рядом с `queue_db_path`.
- `driver_factory` — внедрение драйвера (по умолчанию `PymodbusTcpDriver`,
  в демо — `MockModbusDriver`).
- API: `start()`, `stop()`, `acknowledge_safe_state()`,
  `submit_move(axis, pos, spd, pulse=False)`, `pending_count()`, `history()`,
  `dead_letters()`, атрибут `events`.

В GUI пока не подключён.

### 6.12 `scpi.py`

`send_command(command, ip, port) -> str`: TCP-соединение (таймаут 3 с),
отправка строки с `\n`; если в команде есть `?` — читает один ответ (до 4096
байт). Ошибки — `OSError`.

### 6.13 `logger.py` — `FileLogger`

Один файл на запуск: `log/plc_panel_YYYYMMDD_HHMMSS.log` (каталог из
`AppSettings.log_dir`). Если каталог создать нельзя — системный temp. Запись
best-effort.

### 6.14 `queue_demo.py`, `supervised_demo.py`

Исполняемые проверки инвариантов (§11).

---

## 7. Пользовательский интерфейс

Главное окно `MainWindow` — строка статуса (кнопка связи, переключатель
журнала), четыре вкладки и док-панель журнала.

### 7.1 Строка статуса

- **Связь**: красная «Отсутствует» → жёлтая «Переподключение…» → зелёная «ОК».
  Нажатие переключает намерение оператора (подключить/отключить), в т.ч.
  отменяет идущий авто-реконнект. При подключении параметры связи из вкладки
  «Настройки» сохраняются в `app_config.json`.
- **Журнал ▼/▲** — показать/скрыть док журнала.

### 7.2 Вкладка «Рабочая панель XYZ»

- «Включить все двигатели» — Servo ON на `active_axes`.
- «АВАРИЙНЫЙ СТОП (все оси)» — останавливает сценарий, `MC_Stop` с
  замедлением 5000 на всех осях, затем снимает питание. Это **программный**
  стоп через Modbus, он не заменяет аппаратный E-stop.
- Карточки осей X/Y/Z: ON/OFF, индикатор питания (по обратному чтению коила
  `MC_Power`: Включён / Выключен / Нет связи / —), цель, скорость, MOVE.
- **Автоматический сценарий сканирования**: конструктор шага (5 типов),
  импорт/экспорт JSON, очистка, список шагов, ЗАПУСК / ПАУЗА(ПРОДОЛЖИТЬ) / СТОП.
- **Мониторинг плоскости XY**: график с сеткой по `GridLimits`, маркер текущей
  позиции, перекрестие и координаты под курсором; строка `X | Y | Z`.

Редактор сценария (`ScenarioQueueWidget`):
- каждая строка редактирует свой `ScanStep` напрямую;
- радиокнопка — шаг, с которого начать запуск;
- «☰» — перетаскивание строки мышью для изменения порядка;
- 🗑 — удаление именно этого шага (сравнение по identity, а не по `==`);
- активный шаг подсвечивается и прокручивается в видимую область.

Файл сценария можно открыть и перетаскиванием `*.json` на окно.

### 7.3 Вкладка «Отладка двигателей»

- Карточка **концевиков и блокировки**: для каждой оси два концевика
  (замкнут/разомкнут/нет связи), индикатор защёлки блокировки и кнопка
  «Сброс блокировки».
- По колонке `AxisDebugPanel` на ось: реальная позиция, сырые слова,
  коды ошибок, линейная шкала, ON/OFF/Сброс, `MC_MoveAbsolute`,
  `MC_SetPosition` (Применить / Сброс в 0.00), `MC_MoveVelocity`
  (КРУИЗ/STOP), `MC_MoveRelative`.

> Относительные перемещения доступны **только** здесь, в ручной отладке, и
> никогда не попадают в очередь (инвариант №4).

### 7.4 Вкладка «Настройки»

Параметры ПЛК (IP, порт, период опроса, автоподключение, подключённые оси),
параметры SCPI-анализатора, границы сетки XYZ («Применить сетку XYZ»),
«Сохранить настройки» → `app_config.json`.

### 7.5 Вкладка «Отладка входов-выходов easy521»

- Y0: HIGH/LOW (постоянный уровень).
- Y1: HIGH/LOW.
- Y2: частота меандра (Гц), Старт/Стоп.

У каждого выхода — индикатор (см. §14.3 о сопоставлении индикаторов).

### 7.6 Журнал

`LogDock` — док слева/справа/снизу, может быть плавающим окном. Автопрокрутка,
очистка, ограничение 5000 строк. Каждая строка `[HH:MM:SS] …` дублируется в
файл `FileLogger`.

---

## 8. Карты регистров Modbus

Источник истины для ладдера — `python_panel/PLC_LADDER_REQUIREMENTS.md`;
в коде — `plc_panel/config.py` (`AXES`, `AXIS_SAFETY`, `IO`), синхронно с
`src_tauri/src/config.rs`. Адреса — «сырые» Modbus-адреса, совпадающие с
номерами `M` и `D` ПЛК.

### 8.1 Оси (вариант A, ручное управление)

| | X (реализована в MAIN.LD) | Y (контракт) | Z (контракт) |
|---|---|---|---|
| MC_Power | M10 | M20 | M30 |
| MC_MoveVelocity | M11 | M21 | M31 |
| MC_Reset | M12 | M22 | M32 |
| MC_MoveAbsolute | M13 | M23 | M33 |
| Авто-импульс по Done | M14 | M24 | M34 |
| MC_ReadAxisError | M15 | M25 | M35 |
| MC_Stop | M16 | M26 | M36 |
| MC_SetPosition | M17 | M27 | M37 |
| MC_MoveRelative | M19 | M29 | M39 |
| Velocity speed/acc/dec | D10/D12/D14 | D100/D102/D104 | D130/D132/D134 |
| MoveAbsolute pos/vel | D16/D18 | D106/D108 | D136/D138 |
| MoveRelative dist/vel | D20/D22 | D110/D112 | D140/D142 |
| SetPosition | D24 | D114 | D144 |
| Stop decel | D50 (заглушка) | D116 | D146 |
| Факт. позиция / скорость | D210 / D214 | D230 / D234 | D250 / D254 |
| Ошибка servo / axis | D220 / D222 | D240 / D242 | D260 / D262 |

Оси Y/Z в `MAIN.LD` физически пока не запрограммированы — их адреса являются
контрактом для будущего ладдера.

### 8.2 Безопасность и дискретный I/O

| Назначение | Адрес | Направление |
|---|---|---|
| Общий ErrorID MC-блоков | D200 | чтение |
| SET Y0 / RST Y0 | M50 / M51 | запись, чтение |
| Y1 (статический уровень) | M52 | запись, чтение |
| Y2 разрешение генератора | M53 | запись, чтение |
| Y2 частота (REAL) | D90 | запись |
| «Входы X0…X3» (коилы) | M100…M103 | чтение |
| Концевики X лев./прав. | вход X1 / X2 (FC02, адр. 1/2) | чтение |
| Концевики Y лев./прав. | вход X3 / X4 (адр. 3/4) | чтение |
| Концевики Z лев./прав. | вход X5 / X6 (адр. 5/6) | чтение |
| Блокировка X / Y / Z (latch) | M100 / M200 / M300 | чтение |
| Сброс блокировки X / Y / Z | M101 / M201 / M301 | импульс |

### 8.3 Handshake-интерфейс (вариант B)

| Блок | Регистры |
|---|---|
| Команда (Python → ПЛК, FC16) | D400 target_hi, D401 target_lo, D402 speed, D403 command_code, D404 seq_hi, D405 seq_lo |
| Статус (ПЛК → Python, FC03) | D410 ack_seq_hi, D411 ack_seq_lo, D412 command_state, D413 error_code, D414 status_flags |
| Watchdog | D420 Python→ПЛК (dead-man), D421 ПЛК→Python (heartbeat) |

Полная логика ладдера — `HANDSHAKE_PROTOCOL.md`.

### 8.4 Порядок слов — важно

| Что | Порядок | Где |
|---|---|---|
| REAL (float32) панели | **младшее слово первым** `[lo, hi]` | `plc_panel/codec.py` |
| 32-битные целые servo_core (seq, target) | **старшее слово первым** `[hi, lo]` | `core/protocol.py` |

В ладдере для варианта B 32-битные значения нужно собирать вручную
(`D404*65536 + D405`), а не нативным `DMOV`.

---

## 9. Файлы конфигурации и данных

Все файлы лежат в `app_base_dir()` (§6.2).

### 9.1 `app_config.json`

| Ключ | Тип | По умолч. в коде | Смысл |
|---|---|---|---|
| `plc_ip` | str | `192.168.1.88` | IP ПЛК |
| `plc_port` | int | 502 | Порт Modbus TCP |
| `poll_interval_ms` | int | 150 | Период опроса телеметрии (мин. 20) |
| `scpi_ip` / `scpi_port` | str / int | `192.168.1.99` / 5025 | Анализатор |
| `auto_connect` | bool | false | Подключаться при запуске |
| `active_axes` | list[int] | `[1,2,3]` | Оси с реально подключённым приводом |
| `arrival_tolerance_mm` | float | 0.4 | Допуск «ось приехала» |
| `move_timeout_ms` | int | 30000 | Запас сверх оценки времени хода до повтора (§14.4) |
| `max_move_attempts` | int | 3 | Попыток до dead-letter (включая первую) |
| `queue_db_path` | str | `<base>/scan_queue.db` | Файл очереди (только чтение из JSON, не сохраняется) |
| `log_dir` | str | `<base>/log` | Каталог логов (только чтение из JSON, не сохраняется) |
| `limits.{x,y,z}_{min,max}` | float | см. `GridLimits` | Границы графика и шкал |

Пример текущего файла:

```json
{
  "plc_ip": "192.168.4.88", "plc_port": 502, "poll_interval_ms": 200,
  "scpi_ip": "192.168.1.99", "scpi_port": 5025, "auto_connect": true,
  "active_axes": [1, 2, 3], "arrival_tolerance_mm": 0.4,
  "move_timeout_ms": 3000, "max_move_attempts": 3,
  "limits": {"x_min": -50.0, "x_max": 3000.0, "y_min": -50.0,
             "y_max": 500.0, "z_min": -10.0, "z_max": 200.0}
}
```

> `move_timeout_ms: 3000` здесь — запас сверх расчётного времени хода, а не
> полный таймаут (см. §14.4).

### 9.2 Формат сценария (`scan_sequence*.json`)

Массив шагов:

```json
[
  {"type": "move",       "axis": 1, "pos": 50.0, "spd": 10.0},
  {"type": "move_pulse", "axis": 2, "pos": 20.0, "spd": 5.0},
  {"type": "delay",      "ms": 1000},
  {"type": "scpi",       "cmd": ":INITiate:IMMediate"},
  {"type": "pulse"}
]
```

`axis`: 1=X, 2=Y, 3=Z. `pos` — абсолютная координата, мм. Шаги неизвестного
типа при импорте молча отбрасываются.

### 9.3 Рантайм-артефакты

| Файл | Кто пишет | Содержимое |
|---|---|---|
| `scan_queue.db` (+ `-wal`, `-shm`) | `MotionQueue` | Очередь, история, DLQ сценариев |
| `supervised_queue.db` | `SupervisedBackbone` | То же для варианта B |
| `scan_session.json` | `ScanSession` | Прерванный сценарий (существует только после краша) |
| `log/plc_panel_*.log` | `FileLogger` | Журнал оператора |

Историю и dead-letter можно смотреть любым SQLite-клиентом:

```sql
SELECT seq, status, error_text, datetime(created_at,'unixepoch','localtime')
FROM command_history WHERE channel='SCAN' ORDER BY id DESC LIMIT 50;

SELECT seq, reason, error_text FROM dead_letter ORDER BY id DESC;
```

---

## 10. Гарантии надёжности и сценарии отказов

### 10.1 Инварианты (кратко; полный текст — `CLAUDE.md`)

1. Снятие команды с очереди и запись истории/DLQ — одна транзакция.
2. Команда закрывается по факту отработки ПЛК, а не по факту записи в шину.
3. Идемпотентность через seq: доставка at-least-once, исполнение at-most-once
   = **effectively-once** (не exactly-once).
4. Только абсолютные команды в очереди.
5. Recovery гейтится E-stop — нужно подтверждение оператора.
6. E-stop вне очереди.
7. Монотонный seq на канал в БД, не сбрасывается.
8. Один Modbus-клиент на канал, не шарится между потоками.
9. Изоляция каналов.

### 10.2 Что происходит при отказах (вариант A, текущий GUI)

| Отказ | Поведение |
|---|---|
| Краш/закрытие приложения во время хода | Ход остаётся `IN_FLIGHT` в БД, сценарий — в `scan_session.json`. После перезапуска и подключения: `recover()` закрывает ход без движения, если ось уже в точке, иначе переотправляет абсолютную команду; сценарий продолжается с сохранённого шага |
| Обрыв TCP / смена VPN | `PlcWorker` переходит в «Переподключение…», повтор каждые 2 с. После восстановления — `recover()` |
| Ось не доехала за таймаут | Повтор до `max_move_attempts`, затем dead-letter и остановка сценария |
| Нет привода на оси | Шаг движения пропускается (ось не в `active_axes`) |
| Очередь переполнена | `QueueFullError` → сценарий прерывается |
| E-stop при recovery | Движение не возобновляется до `acknowledge_safe_state()` |
| Сбой записи в журнал/сессию | Игнорируется (best-effort), управление не страдает |

### 10.3 Вариант B дополнительно

- Дедуп на ПЛК по энергонезависимому `last_processed_seq` — даже повторная
  отправка того же seq не двигает привод.
- Dead-man: если Python завис, D420 перестаёт меняться, ПЛК сам переводит
  привод в безопасное состояние.
- Застывший heartbeat ПЛК (D421) → `CONNECTION_LOST`, канал перестаёт брать
  новые команды.

---

## 11. Проверка и тестирование

### 11.1 Автотесты (pytest)

```bash
pip install pytest
python -m pytest            # из корня репозитория; конфиг — pytest.ini
```

Тесты лежат в `tests/`. Они не требуют железа и дисплея: Qt запускается с
платформой `offscreen`, ПЛК эмулируется `core.mock.MockPlc` или фиктивным
`PlcService`, каждая БД создаётся во временном каталоге. `conftest.py`
добавляет в `sys.path` корень репозитория (для `core`) и `python_panel/`
(для `plc_panel`).

| Файл | Что покрывает |
|---|---|
| `test_core_protocol.py` | u32 hi-first, раскладка командного блока (seq последним), отрицательные позиции, декодирование статуса и флагов |
| `test_core_repository.py` | Монотонный seq (в т.ч. после рестарта), FIFO-claim, complete/dead-letter/retry вместе с историей, откат транзакции, backpressure, изоляция ошибок подписчиков EventBus |
| `test_core_executor.py` | Подтверждение по факту ПЛК, recovery без повторной отправки уже выполненной команды, отправка команды, которую ПЛК не видел, дедуп на `MockPlc`, SAFETY_LOCK при E-stop, retry→dead-letter по таймауту, транспортная ошибка как retryable |
| `test_core_supervisor.py` | Штатный поток команд; E-stop-гейт recovery; `acknowledge_safe_state` не блокирует вызывающего и выполняет recovery в `worker-<канал>` (14.6); старое подтверждение не снимает новый гейт; новые команды ждут при блокировке |
| `test_panel_models.py` | REAL low-first, `ScanStep` ↔ JSON, `ScanStep` ↔ `MotionCommand`, переживание рестарта `MotionQueue`, загрузка `AppSettings`, сопоставление индикаторов выходов (14.3) |
| `test_panel_scenario.py` | Таймаут хода по оценке времени и сохранение retry/dead-letter (14.4); отложенный запуск во время recovery, E-stop, отмена по стопу, отмена при неудачном recovery, отсутствие дублей recovery, одноразовость подтверждения (14.5) |

### 11.2 Исполняемые демо

| Команда | Что проверяет | Ожидаемый результат |
|---|---|---|
| `python -m plc_panel.queue_demo` | 3 хода в очередь, 2 выполнены, на третьем «краш»; рестарт на той же БД, recovery закрывает ход без повторного движения; в истории seq 1,2,3 — все `DONE`, DLQ пуст | `OK: … = effectively-once.` |
| `python -m plc_panel.supervised_demo` | Настоящие `Supervisor`/`MotionExecutor`/`ModbusWatchdog` на `MockPlc`: 3 команды → DONE; краш после FC16, рестарт, recovery; `last_processed_seq` не изменился | `OK: recovery закрыл зависшую команду БЕЗ повторного движения …` |
| `python -m compileall core python_panel/plc_panel` | Синтаксис | без ошибок |
| `python -c "import core"` (из корня, без PyQt6) | Ядро импортируется headless | без ошибок |

Оба демо прогнаны при подготовке этого документа и прошли.

Чего не хватает (см. также `CLAUDE.md`):
- тестов «kill процесса в середине транзакции» с проверкой целостности БД
  (сейчас проверяется только откат по исключению);
- тестов `PlcWorker` (авто-переподключение, счётчик сбоев опроса) — нужен
  фиктивный Modbus-сервер;
- тестов паузы/резюме `ScenarioRunner` и `ScanSession`.

---

## 12. Сборка дистрибутива (PyInstaller)

```bash
cd python_panel
pyinstaller InovanceScanner.spec
# результат: dist/InovanceScanner/
```

Особенности `InovanceScanner.spec`:
- корень репозитория добавлен в `pathex`, иначе PyInstaller не увидит `core`
  (он импортируется через `sys.path`-хак);
- `hiddenimports`: `sqlite3`, `_sqlite3` и все подмодули `pymodbus`;
- **не** используется `collect_submodules('core')` — иначе затянулись бы
  `core.qt_bridge` / `core.pyqt_example`, завязанные на PyQt6 (конфликт с PyQt5);
- `app_config.json` кладётся рядом с exe как стартовые настройки.

`main.spec` — автосгенерированный спек без этих настроек; для сборки не
использовать.

В сборке все пользовательские файлы (конфиг, логи, БД, сессия) живут рядом с
`.exe` (`paths.app_base_dir()`), поэтому каталог установки должен быть
доступен на запись.

---

## 13. Как расширять

### Добавить/изменить адреса оси
Правьте `AXES` / `AXIS_SAFETY` / `IO` в `plc_panel/config.py` **и**
`src_tauri/src/config.rs`, затем `PLC_LADDER_REQUIREMENTS.md`. `LIMIT_INPUT_*`
пересчитываются автоматически.

### Новый тип шага сценария
1. Добавить значение в `StepType` и поддержку в `ScanStep.to_dict/from_dict`.
2. Добавить ветку в `ScenarioRunner._execute`.
3. Добавить пункт в конструктор (`MainWindow._build_trajectory_card`,
   `_build_step_editor`, `_sync_step_editor`, `_add_step`) и редактор строки
   (`ScenarioQueueWidget._build_editors`).
4. Если шаг — движение, он обязан быть абсолютным, чтобы попасть в очередь.

### Новая Modbus-команда
Слот в `PlcWorker` (через `_with_axis` / `_require_client` / `_fail`) →
request-сигнал и метод в `PlcService` → кнопка в UI. Modbus I/O — только в
воркере.

### Изменение схемы БД
Поднять `SCHEMA_VERSION` в `core/database.py` и добавить шаг в `_migrate`.
Существующие таблицы без миграции не менять.

### Переход на вариант B
1. Реализовать в AutoShop §1–§6 `HANDSHAKE_PROTOCOL.md`.
2. Сверить адреса с `HANDSHAKE_REGISTERS`.
3. В `MainWindow` создать `SupervisedBackbone(settings)` вместо/рядом с
   `MotionQueue`; `ScenarioRunner` перевести на `submit_move` + ожидание
   события `DONE` из `backbone.events` вместо детекции по телеметрии.
   Для доставки событий в GUI-поток из потока `worker-EASY521` нужен свой
   мост на PyQt5 (аналог `QtEventBridge`) — прямые вызовы виджетов из
   подписчика EventBus недопустимы.
4. Учесть, что `PlcWorker` и `ChannelWorker` будут держать **два** TCP-соединения
   с одним ПЛК — проверить, что Easy521 это допускает.

### Чего не делать
Не подключать брокеры (Redis/RabbitMQ/Kafka), не делать Modbus I/O в
GUI-потоке, не класть бизнес-логику в `driver.py`, не разделять транзакции
очереди и истории, не возвращать persist-queue.

---

## 14. Известные проблемы и ограничения

Найдены при анализе кода. Пункты 14.3–14.6 исправлены и покрыты тестами
(§11), остальные открыты.

### 14.1 Демо ядра не запускаются (`servo_core` vs `core`)
`core/demo.py` и `core/pyqt_example.py` импортируют `servo_core.*`, а пакет
называется `core`. `python -m servo_core.demo` (команда из `CLAUDE.md`) и
`python -m core.demo` падают с `ModuleNotFoundError`. Варианты исправления:
относительные импорты (`from .config import …`) в демо, либо переименование
каталога в `servo_core` с правкой импортов в `queue_backbone.py`,
`supervised_backbone.py`, `supervised_demo.py` и `InovanceScanner.spec`.

### 14.2 Конфликт адресов M100…M103
`IO.INPUT_X0 = 100` — телеметрия читает коилы M100…M103 как «входы X0…X3».
Но `AXIS_SAFETY` использует M100 как защёлку блокировки X, а M101 — как сброс
блокировки X. Следствия:
- `telemetry.inputs[0]` фактически показывает блокировку оси X, а не вход X0;
- **E-stop-гейт recovery** (`ScenarioRunner._safe_to_move`) смотрит на
  `inputs[3]` = M103. В `PLC_LADDER_REQUIREMENTS.md` §3.4 E-stop описан как
  «X3/M103», тогда как физический вход X3 теперь — левый концевик оси Y.
  Нужно явно определить, какой элемент ПЛК является признаком E-stop, и
  завести для него отдельную константу.

### 14.3 ~~Индикаторы выходов на вкладке I/O смещены~~ — исправлено
Было: опрос читает M50…M53 = `[SET Y0, RST Y0, Y1, Y2_EN]`, а три индикатора
брали `outputs[0..2]`, поэтому Y1 показывал M51, а Y2 — M52.
Теперь коилы индикаторов заданы явно в `IO.OUTPUT_LED_COILS = (M50, M52, M53)`,
а `ui/main_window.output_led_states()` берёт из окна именно их. Мёртвый цикл
обновления несуществующих индикаторов входов удалён.

### 14.4 ~~Фиксированный таймаут хода~~ — исправлено
Было: единый `move_timeout_ms` на любой ход; при значении 3000 из
`app_config.json` ход дольше ~9 с уходил в dead-letter, хотя ось ехала.
Теперь таймаут считается на каждый ход (`ScenarioRunner._move_timeout_ms`):

```
timeout = move_timeout_ms + |target − текущая позиция| / spd · 1000 · 1.5
```

`move_timeout_ms` стал запасом на разгон/торможение и задержки связи. Без
кадра телеметрии по оси или при `spd <= 0` используется только
`move_timeout_ms`. Предполагается, что скорость задаётся в мм/с.

### 14.5 ~~Одновременные recovery и продолжение сессии~~ — исправлено
Было: на `connected` одновременно стартовали recovery и продолжение сессии
и могли перезаписывать `_inflight` друг друга.
Теперь recovery и сценарий взаимоисключающие:
- `start()`, вызванный во время recovery, откладывается и выполняется после
  `Recovery завершён.`;
- неудачный recovery (dead-letter) отменяет отложенный запуск;
- `stop()` (в т.ч. «АВАРИЙНЫЙ СТОП», который теперь вызывает его всегда)
  отменяет recovery и отложенный запуск и останавливает таймер
  recovery-хода, чтобы он не был переотправлен; незавершённые ходы остаются
  `IN_FLIGHT` и сверяются при следующем подключении;
- повторный `recover()` во время recovery игнорируется;
- подтверждение оператора (`acknowledge_safe_state`) действует на один
  recovery — следующий снова ждёт подтверждения при активном E-stop.

### 14.6 ~~`acknowledge_safe_state` вне потока канала~~ — исправлено
Было: `ChannelWorker.acknowledge_safe_state()` запускал `executor.recover()`
в вызывающем (GUI) потоке, что нарушало инвариант №8.
Теперь метод только выставляет `threading.Event`, а recovery выполняет
поток `worker-<канал>` в начале следующей итерации `_main_loop`. Вызов не
блокирует GUI. Запрос, поданный до (пере)подключения, сбрасывается в
`_safe_recovery`, поэтому старое подтверждение не снимет свежий E-stop-гейт.

### 14.7 Прочее
- `AxisDebugPanel._axis_range()` для оси Y использует границы X (только Z имеет
  свои).
- `ModbusWatchdog.connection_ok` изначально `True` — до первого обмена канал
  считается живым.
- В `SupervisedBackbone` `ChannelConfig.port` хранит IP-адрес (поле
  перегружено: для RTU — COM-порт, для TCP — host).
- Относительные перемещения и `MoveVelocity` (ручная отладка) идут мимо очереди
  и не имеют гарантий recovery — это осознанно.
- `mock.py` — упрощённая модель, не эталон реального привода: тайминги и
  профиль регистров на железе будут другими.
- Версия pymodbus в `requirements.txt` не закреплена (см. §3.1).

---

## 15. Глоссарий

| Термин | Значение |
|---|---|
| **ПЛК / Easy521** | Программируемый контроллер Inovance, исполняющий движение |
| **MAIN.LD** | Программа ПЛК на языке релейной логики (AutoShop) |
| **MC-блок** | Функциональный блок PLCopen Motion Control (`MC_Power`, `MC_MoveAbsolute`, …) |
| **M / D** | Коил (бит) / регистр данных (16 бит) ПЛК |
| **REAL** | 32-битное число с плавающей точкой (2 регистра) |
| **FC02 / FC03 / FC05 / FC06 / FC16** | Функции Modbus: чтение дискретных входов / holding-регистров / запись коила / одного регистра / блока регистров |
| **Строб** | Импульс коила 1→0, запускающий MC-блок по переднему фронту |
| **seq** | Монотонный номер команды на канал, ключ идемпотентности |
| **ack_seq** | Номер последней команды, подтверждённой ПЛК |
| **IN_FLIGHT** | Команда взята исполнителем, но не подтверждена |
| **Dead-letter (DLQ)** | Таблица команд, которые не удалось выполнить |
| **Recovery** | Разбор `IN_FLIGHT`-команд после рестарта/переподключения |
| **Backpressure** | Отказ принимать команды при переполненной очереди |
| **Dead-man** | Счётчик от Python; если застыл — ПЛК останавливает привод |
| **Heartbeat** | Счётчик от ПЛК; если застыл — Python считает связь потерянной |
| **Effectively-once** | At-least-once доставка + at-most-once исполнение |
| **SCPI** | Текстовый протокол управления измерительными приборами |
