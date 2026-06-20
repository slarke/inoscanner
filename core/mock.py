"""Программная модель ПЛК и mock-драйвер: запуск ядра без железа.

MockPlc реализует автомат состояний, идемпотентность по last_processed_seq
и heartbeat. MockModbusDriver предоставляет тот же интерфейс, что и реальный
драйвер, поэтому ядро не видит разницы.
"""
from __future__ import annotations

import threading
import time
from typing import Sequence

from .config import FLAG_ESTOP, FLAG_GUARD_OPEN, FLAG_READY, RegisterMap
from .driver import ModbusDriver, ModbusError
from .models import PLC_CODE_BY_STATE, PlcCommandState
from .protocol import regs_to_u32, u32_to_regs


class MockPlc:
    """Эмулятор ПЛК поверх словаря «регистров»."""

    def __init__(self, registers: RegisterMap, move_time: float = 0.2):
        self._r = registers
        self._move_time = move_time
        self._mem: dict[int, int] = {}
        self._last_processed_seq = 0
        self._lock = threading.Lock()

        self._set_status(0, PlcCommandState.IDLE, 0, FLAG_READY)
        self._mem[self._r.wd_plc_to_python] = 0
        threading.Thread(target=self._heartbeat, daemon=True).start()

    # --- «регистровый» интерфейс для драйвера ---
    def read(self, address: int, count: int) -> list[int]:
        with self._lock:
            return [self._mem.get(address + i, 0) for i in range(count)]

    def write_block(self, address: int, values: Sequence[int]) -> None:
        with self._lock:
            for i, v in enumerate(values):
                self._mem[address + i] = v & 0xFFFF
            if address == self._r.cmd_block_start:
                self._on_command_written()

    def write_single(self, address: int, value: int) -> None:
        with self._lock:
            self._mem[address] = value & 0xFFFF

    # --- управление безопасностью (для демонстрации E-stop) ---
    def trigger_estop(self) -> None:
        with self._lock:
            b = self._r.status_block_start
            self._mem[b + 4] = self._mem.get(b + 4, 0) | FLAG_ESTOP
            self._mem[b + 4] &= ~FLAG_READY & 0xFFFF

    def clear_estop(self) -> None:
        with self._lock:
            b = self._r.status_block_start
            self._mem[b + 4] = (self._mem.get(b + 4, 0) & ~FLAG_ESTOP) | FLAG_READY

    # --- внутренняя логика ПЛК ---
    def _on_command_written(self) -> None:
        seq = regs_to_u32(
            self._mem.get(self._r.cmd_block_start + 4, 0),
            self._mem.get(self._r.cmd_block_start + 5, 0),
        )
        # Идемпотентность: уже обработанный (или более старый) seq не запускает
        # движение второй раз — защита от повторной отправки после краша.
        if seq <= self._last_processed_seq:
            return
        self._set_status(self._last_processed_seq, PlcCommandState.RECEIVED, 0,
                         FLAG_READY)
        threading.Thread(target=self._execute_move, args=(seq,), daemon=True).start()

    def _execute_move(self, seq: int) -> None:
        with self._lock:
            self._set_status(seq, PlcCommandState.RUNNING, 0, 0)  # not ready
        time.sleep(self._move_time)  # эмуляция движения (вне lock)
        with self._lock:
            self._last_processed_seq = seq
            self._set_status(seq, PlcCommandState.DONE, 0, FLAG_READY)

    def _set_status(self, ack_seq: int, state: PlcCommandState, err: int,
                    flags: int) -> None:
        hi, lo = u32_to_regs(ack_seq)
        b = self._r.status_block_start
        self._mem[b + 0] = hi
        self._mem[b + 1] = lo
        self._mem[b + 2] = PLC_CODE_BY_STATE[state]
        self._mem[b + 3] = err
        self._mem[b + 4] = flags

    def _heartbeat(self) -> None:
        while True:
            with self._lock:
                cur = self._mem.get(self._r.wd_plc_to_python, 0)
                self._mem[self._r.wd_plc_to_python] = (cur + 1) & 0xFFFF
            time.sleep(0.4)


class MockModbusDriver(ModbusDriver):
    """Драйвер поверх MockPlc — тот же контракт, что и у реального."""

    def __init__(self, plc: MockPlc):
        self._plc = plc
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def read_holding(self, address: int, count: int) -> list[int]:
        if not self._connected:
            raise ModbusError("mock: не подключён")
        return self._plc.read(address, count)

    def write_registers(self, address: int, values: Sequence[int]) -> None:
        if not self._connected:
            raise ModbusError("mock: не подключён")
        self._plc.write_block(address, list(values))

    def write_register(self, address: int, value: int) -> None:
        if not self._connected:
            raise ModbusError("mock: не подключён")
        self._plc.write_single(address, value)
