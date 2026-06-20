# servo_core

Ядро надёжной очереди команд для цепочки **PyQt → очередь → Modbus → ПЛК → сервопривод**.

Реальное движение (real-time, безопасность) исполняет ПЛК. Python — супервайзер:
надёжно ставит команды в очередь, ведёт handshake с ПЛК, гарантирует отсутствие
потери команд и отсутствие двойного запуска движения после сбоев питания, крашей
приложения или разрыва связи.

## Механизмы надёжности

Надёжность даёт не одна библиотека, а комбинация:

```
Persistent Queue        — единая SQLite (WAL), команда на диске до подтверждения
+ Monotonic Sequence    — seq на канал, монотонный, переживает рестарт
+ PLC Acknowledge       — подтверждение по факту отработки ПЛК (ack_seq == seq)
+ Idempotency           — ПЛК помнит last_processed_seq, повтор не двигает второй раз
+ Recovery Logic        — на старте сверка in-flight команд с состоянием ПЛК
+ Bidirectional Watchdog— dead-man Python→ПЛК и heartbeat ПЛК→Python
+ Hardware E-Stop        — аппаратный, вне очереди; recovery после E-stop гейтится
```

Это даёт **effectively-once**: доставка at-least-once (Python может переотправить),
исполнение at-most-once (дедуп по seq на ПЛК).

## Слои

| Модуль | Ответственность |
|---|---|
| `database.py` | одна SQLite, WAL, транзакции, миграции схемы |
| `repository.py` | DAO: очередь + история + dead-letter + seq, всё транзакционно |
| `driver.py` | транспорт Modbus (ABC + pymodbus 3.x), без бизнес-логики |
| `protocol.py` | кодирование команда/seq ↔ регистры по `RegisterMap` |
| `executor.py` | handshake-автомат с ПЛК, retry/dead-letter, recovery |
| `watchdog.py` | двунаправленный контроль связи, отдельный поток |
| `supervisor.py` | поток на канал, изоляция, авто-перезапуск, E-stop-гейт |
| `service.py` | фасад для GUI, backpressure |
| `events.py` | Qt-агностичная шина событий |
| `qt_bridge.py` | мост EventBus → pyqtSignal (импорт PyQt защищён) |
| `mock.py` | модель ПЛК + mock-драйвер для запуска без железа |

Зависимости направлены внутрь: `executor` зависит от абстракций `ModbusDriver` и
`CommandRepository`, а не от их реализаций. Поэтому ядро тестируется без железа
(mock-драйвер) и без Qt (EventBus напрямую).

## Запуск

```bash
# headless-демо (без железа, без Qt): штатный путь + краш/recovery
python -m servo_core.demo

# GUI-пример (нужен PyQt6)
pip install PyQt6 pymodbus
python -m servo_core.pyqt_example
```

## Подключение реального железа

Заменить `MockModbusDriver(MockPlc(...))` на `PymodbusSerialDriver(...)`:

```python
from servo_core import PymodbusSerialDriver, ChannelConfig, RegisterMap

cfg = ChannelConfig(name="COM3", port="COM3", unit_id=1, baudrate=19200,
                    registers=RegisterMap(cmd_block_start=0x0000,
                                          status_block_start=0x0100))
supervisor.add_channel(cfg, PymodbusSerialDriver("COM3", unit_id=1,
                                                 baudrate=19200))
```

Несколько приводов — `add_channel(...)` на каждый порт; у каждого свой поток,
свой Modbus-клиент и своя секция в общей БД. Ошибка одного порта не блокирует
другие.

## Что остаётся на стороне ПЛК (обязательно)

- Дедупликация по `last_processed_seq`: при `seq <= last_processed_seq` движение
  не запускать, вернуть `ack_seq = seq, state = DONE`.
- Запуск только при `req_seq != ack_seq && status.ready`; payload защёлкивать
  атомарно с приёмом seq.
- Dead-man: если watchdog-регистр от Python перестал меняться — перевести привод
  в безопасное состояние.
- Аварийный стоп — аппаратный, независимый от Python и от прикладной логики ПЛК.

## Закрепление версий

```
pymodbus==3.13.1     # имя kwarg адреса устройства зависит от версии (см. driver.py)
PyQt6>=6.6           # только для GUI; ядро работает headless
# persist-queue НЕ используется: своя SQLite даёт транзакционную связность
# очереди с историей/DLQ (ack очереди и запись истории — одна транзакция).
```
