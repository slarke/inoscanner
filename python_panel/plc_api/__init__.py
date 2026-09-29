"""HTTP REST + WebSocket API управления ПЛК Easy521 на текущей карте регистров.

Пакет не зависит от Qt. Слои:
    ladder      — что реально реализует текущий MAIN.LD (возможности/ограничения)
    transport   — Modbus TCP (pymodbus), только чтение/запись регистров
    sim         — эмулятор текущего ладдера (тесты, режим --simulate)
    controller  — поток-владелец Modbus-клиента: команды, опрос, события
    server      — aiohttp: REST для команд, WebSocket для телеметрии/событий

Запуск:  python -m plc_api [--simulate] [--host 127.0.0.1] [--port 8765]
"""
from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
