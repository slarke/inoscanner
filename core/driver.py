"""Транспортный слой: только чтение/запись регистров Modbus.

Драйвер НЕ знает про seq, команды, состояния — никакой бизнес-логики.
Это делает его заменяемым и подменяемым моком (см. mock.py).
"""
from __future__ import annotations

import abc
import threading
from typing import Sequence


class ModbusError(Exception):
    """Ошибка транспортного уровня: таймаут, обрыв, исключение протокола."""


class ModbusDriver(abc.ABC):
    """Интерфейс транспорта. Реализации: pymodbus (железо) и mock (тесты)."""

    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    @abc.abstractmethod
    def read_holding(self, address: int, count: int) -> list[int]:
        """FC03: прочитать count holding-регистров начиная с address."""

    @abc.abstractmethod
    def write_registers(self, address: int, values: Sequence[int]) -> None:
        """FC16: записать блок регистров ОДНОЙ транзакцией."""

    @abc.abstractmethod
    def write_register(self, address: int, value: int) -> None:
        """FC06: записать один регистр (watchdog, флаги)."""


class PymodbusSerialDriver(ModbusDriver):
    """Реализация поверх pymodbus 3.x (RTU).

    Синхронный клиент pymodbus НЕ потокобезопасен, поэтому один драйвер
    принадлежит одному worker-потоку и нигде не шарится. Внутренний lock
    защищает только от обращений watchdog-потока того же канала.

    NB: имя kwarg адреса устройства зависит от версии pymodbus
        (unit= -> slave= -> device_id=). Здесь — под 3.13.x (device_id).
        При другой версии поправьте здесь, в одном месте.
    """

    def __init__(
        self,
        port: str,
        unit_id: int = 1,
        baudrate: int = 19200,
        timeout: float = 1.0,
    ):
        self._port = port
        self._unit = unit_id
        self._baudrate = baudrate
        self._timeout = timeout
        self._client = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        from pymodbus.client import ModbusSerialClient

        self._client = ModbusSerialClient(
            port=self._port,
            baudrate=self._baudrate,
            timeout=self._timeout,
            retries=0,  # повторы — выше уровнем, со своей политикой
        )
        if not self._client.connect():
            raise ModbusError(f"Не удалось открыть порт {self._port}")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def is_connected(self) -> bool:
        return self._client is not None and self._client.connected

    def read_holding(self, address: int, count: int) -> list[int]:
        with self._lock:
            self._require()
            rr = self._client.read_holding_registers(
                address=address, count=count, device_id=self._unit
            )
            if rr.isError():
                raise ModbusError(f"read_holding @{address}: {rr}")
            return list(rr.registers)

    def write_registers(self, address: int, values: Sequence[int]) -> None:
        with self._lock:
            self._require()
            rq = self._client.write_registers(
                address=address, values=list(values), device_id=self._unit
            )
            if rq.isError():
                raise ModbusError(f"write_registers @{address}: {rq}")

    def write_register(self, address: int, value: int) -> None:
        with self._lock:
            self._require()
            rq = self._client.write_register(
                address=address, value=value, device_id=self._unit
            )
            if rq.isError():
                raise ModbusError(f"write_register @{address}: {rq}")

    def _require(self) -> None:
        if self._client is None:
            raise ModbusError("Драйвер не подключён")


class PymodbusTcpDriver(ModbusDriver):
    """Реализация ModbusDriver поверх pymodbus TCP (Modbus/TCP).

    Тот же контракт, что и PymodbusSerialDriver, но транспорт — Ethernet
    (Inovance Easy521 по TCP). Синхронный клиент pymodbus НЕ потокобезопасен:
    один драйвер принадлежит одному worker-потоку канала и нигде не шарится.
    Внутренний lock защищает только от обращений watchdog-потока того же канала.

    NB: имя kwarg адреса устройства зависит от версии pymodbus
        (unit= -> slave= -> device_id=). Здесь — под 3.13.x (device_id), как и в
        PymodbusSerialDriver. При другой версии правьте в одном месте.
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = 1,
        timeout: float = 1.0,
    ):
        self._host = host
        self._port = port
        self._unit = unit_id
        self._timeout = timeout
        self._client = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        from pymodbus.client import ModbusTcpClient

        self._client = ModbusTcpClient(
            host=self._host, port=self._port, timeout=self._timeout
        )
        if not self._client.connect():
            raise ModbusError(
                f"Не удалось открыть Modbus TCP {self._host}:{self._port}"
            )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def is_connected(self) -> bool:
        return self._client is not None and self._client.connected

    def read_holding(self, address: int, count: int) -> list[int]:
        with self._lock:
            self._require()
            rr = self._client.read_holding_registers(
                address=address, count=count, device_id=self._unit
            )
            if rr.isError():
                raise ModbusError(f"read_holding @{address}: {rr}")
            return list(rr.registers)

    def write_registers(self, address: int, values: Sequence[int]) -> None:
        with self._lock:
            self._require()
            rq = self._client.write_registers(
                address=address, values=list(values), device_id=self._unit
            )
            if rq.isError():
                raise ModbusError(f"write_registers @{address}: {rq}")

    def write_register(self, address: int, value: int) -> None:
        with self._lock:
            self._require()
            rq = self._client.write_register(
                address=address, value=value, device_id=self._unit
            )
            if rq.isError():
                raise ModbusError(f"write_register @{address}: {rq}")

    def _require(self) -> None:
        if self._client is None:
            raise ModbusError("Драйвер не подключён")
