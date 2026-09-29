"""Конфигурация канала и карта регистров ПЛК.

Адреса регистров задаются под конкретный проект/привод. Блок команды
пишется одним FC16, поэтому его регистры идут подряд.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --- битовые маски регистра флагов (status_flags / safety) ---
FLAG_READY = 0x0001
FLAG_ESTOP = 0x0002
FLAG_GUARD_OPEN = 0x0004


@dataclass(frozen=True)
class RegisterMap:
    """Карта регистров одного ПЛК."""

    # Блок команды (Python -> ПЛК), пишется одной транзакцией FC16.
    # Порядок: [target_hi, target_lo, speed, command_code, seq_hi, seq_lo].
    # seq логически последний — ПЛК не увидит «рваную» команду.
    cmd_block_start: int = 0x0000
    cmd_block_length: int = 6

    # Блок статуса (ПЛК -> Python), читается одним FC03.
    # Порядок: [ack_seq_hi, ack_seq_lo, command_state, error_code, status_flags].
    status_block_start: int = 0x0100
    status_block_length: int = 5

    # Watchdog (двунаправленный).
    wd_python_to_plc: int = 0x0200   # Python инкрементирует (dead-man для ПЛК)
    wd_plc_to_python: int = 0x0201   # ПЛК инкрементирует (heartbeat для Python)


@dataclass(frozen=True)
class ChannelConfig:
    """Параметры одного канала (порт/ПЛК/привод)."""

    name: str                       # логическое имя, напр. "COM3"
    port: str                       # serial-порт или host для TCP
    unit_id: int = 1                # device_id на шине Modbus
    baudrate: int = 19200
    registers: RegisterMap = field(default_factory=RegisterMap)

    poll_interval_s: float = 0.05   # период опроса статуса при ожидании
    command_timeout_s: float = 30.0 # таймаут выполнения одной команды
    watchdog_interval_s: float = 1.0
    watchdog_timeout_s: float = 3.0
    max_attempts: int = 3           # попыток до dead-letter
    max_pending: int = 100          # backpressure: предел длины очереди
    reconnect_backoff_s: float = 2.0
