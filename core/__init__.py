"""servo_core — ядро надёжной очереди команд PyQt -> Modbus -> ПЛК -> сервопривод.

Механизмы надёжности (комбинация, а не одна библиотека):
    Persistent Queue + Monotonic Sequence + PLC Acknowledge
    + Idempotency + Recovery Logic + Bidirectional Watchdog + Hardware E-Stop.

Слои:
    database / repository  — единая SQLite, транзакционные очередь+история+DLQ
    driver / protocol      — транспорт Modbus и кодирование регистров
    executor               — handshake-автомат с ПЛК
    watchdog               — двунаправленный контроль связи
    supervisor             — поток на канал, изоляция и авто-перезапуск
    service                — фасад для GUI (backpressure)
    events / qt_bridge     — Qt-агностичная шина + мост PyQt
"""
from __future__ import annotations

from .config import ChannelConfig, RegisterMap
from .database import Database
from .driver import (
    ModbusDriver,
    ModbusError,
    PymodbusSerialDriver,
    PymodbusTcpDriver,
)
from .events import Event, EventBus, EventType
from .models import (
    CommandState,
    DeadLetterReason,
    MotionCommand,
    PlcCommandState,
    QueuedCommand,
)
from .repository import CommandRepository
from .service import CommandService, QueueFullError
from .supervisor import ChannelWorker, Supervisor

__all__ = [
    "ChannelConfig", "RegisterMap", "Database", "ModbusDriver", "ModbusError",
    "PymodbusSerialDriver", "PymodbusTcpDriver",
    "Event", "EventBus", "EventType", "CommandState",
    "DeadLetterReason", "MotionCommand", "PlcCommandState", "QueuedCommand",
    "CommandRepository", "CommandService", "QueueFullError", "ChannelWorker",
    "Supervisor",
]
