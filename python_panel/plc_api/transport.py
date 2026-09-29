"""Транспорт Modbus TCP: только чтение/запись, без логики команд.

Синхронный клиент pymodbus не потокобезопасен: транспортом владеет ровно один
поток — поток :class:`plc_api.controller.PlcController`.
"""
from __future__ import annotations

from typing import List, Protocol, Sequence


class TransportError(Exception):
    """Сбой обмена: таймаут, обрыв, отказ ПЛК выполнить запрос."""


class ModbusTransport(Protocol):
    def connect(self) -> None: ...
    def close(self) -> None: ...
    def is_open(self) -> bool: ...
    def read_holding(self, address: int, count: int) -> List[int]: ...
    def read_coils(self, address: int, count: int) -> List[bool]: ...
    def read_discrete(self, address: int, count: int) -> List[bool]: ...
    def write_coil(self, address: int, value: bool) -> None: ...
    def write_registers(self, address: int, values: Sequence[int]) -> None: ...


class PymodbusTransport:
    """Modbus TCP поверх pymodbus 3.x (вызовы — как в plc_panel.modbus_worker)."""

    def __init__(self, host: str, port: int = 502, timeout: float = 3.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._client = None

    def connect(self) -> None:
        from pymodbus.client import ModbusTcpClient

        client = ModbusTcpClient(host=self._host, port=self._port,
                                 timeout=self._timeout)
        try:
            ok = client.connect()
        except Exception as exc:  # noqa: BLE001 - любой сбой сокета
            client.close()
            raise TransportError(f"{self._host}:{self._port}: {exc}") from exc
        if not ok:
            client.close()
            raise TransportError(f"не удалось подключиться к {self._host}:{self._port}")
        self._client = client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def is_open(self) -> bool:
        c = self._client
        return c is not None and bool(getattr(c, "connected", True))

    def read_holding(self, address: int, count: int) -> List[int]:
        rr = self._call("read_holding_registers", address, count=count)
        return list(rr.registers)

    def read_coils(self, address: int, count: int) -> List[bool]:
        rr = self._call("read_coils", address, count=count)
        return [bool(b) for b in rr.bits[:count]]

    def read_discrete(self, address: int, count: int) -> List[bool]:
        rr = self._call("read_discrete_inputs", address, count=count)
        return [bool(b) for b in rr.bits[:count]]

    def write_coil(self, address: int, value: bool) -> None:
        self._call("write_coil", address, bool(value))

    def write_registers(self, address: int, values: Sequence[int]) -> None:
        self._call("write_registers", address, list(values))

    def _call(self, method: str, *args, **kwargs):
        if self._client is None:
            raise TransportError("нет соединения с ПЛК")
        try:
            response = getattr(self._client, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - сокет/протокол pymodbus
            raise TransportError(f"{method} @{args[0]}: {exc}") from exc
        if response.isError():
            raise TransportError(f"{method} @{args[0]} отклонён ПЛК: {response}")
        return response
