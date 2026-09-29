"""Запуск API: ``python -m plc_api`` (из каталога python_panel).

Параметры связи с ПЛК, допуск приезда, подключённые оси и мягкие пределы
берутся из app_config.json (как у панели) и переопределяются аргументами.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys

from plc_panel.config import CONFIG_PATH, AppSettings

from .controller import ControllerConfig, PlcController
from .sim import SimulatedPlc
from .transport import PymodbusTransport


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):   # кириллица в консоли cp1252
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass

    p = argparse.ArgumentParser(prog="plc_api", description=__doc__)
    p.add_argument("--config", default=str(CONFIG_PATH), help="путь к app_config.json")
    p.add_argument("--plc-ip", help="IP ПЛК (по умолчанию из конфига)")
    p.add_argument("--plc-port", type=int, help="порт Modbus TCP ПЛК")
    p.add_argument("--host", default="127.0.0.1", help="адрес HTTP-сервера")
    p.add_argument("--port", type=int, default=8765, help="порт HTTP-сервера")
    p.add_argument("--token", default=os.environ.get("PLC_API_TOKEN"),
                   help="Bearer-токен (или переменная PLC_API_TOKEN)")
    p.add_argument("--simulate", action="store_true",
                   help="эмулятор текущего ладдера вместо реального ПЛК")
    p.add_argument("--no-soft-limits", action="store_true",
                   help="не проверять уставки по limits из конфига")
    p.add_argument("--insecure", action="store_true",
                   help="разрешить внешний адрес без токена")
    args = p.parse_args(argv)

    if not _is_loopback(args.host) and not args.token and not args.insecure:
        p.error("сервер на внешнем адресе управляет оборудованием: задайте "
                "--token (или PLC_API_TOKEN), либо явно --insecure")

    from aiohttp import web   # импорт после разбора аргументов (--help без aiohttp)

    from .server import create_app

    settings = AppSettings.load(args.config)
    plc_ip = args.plc_ip or settings.plc_ip
    plc_port = args.plc_port or settings.plc_port
    transport = SimulatedPlc() if args.simulate else PymodbusTransport(plc_ip, plc_port)
    config = ControllerConfig.from_settings(settings,
                                            enforce_limits=not args.no_soft_limits)
    controller = PlcController(transport, config)

    target = "эмулятор ладдера" if args.simulate else f"{plc_ip}:{plc_port}"
    print(f"PLC API: ПЛК = {target}; http://{args.host}:{args.port}/api/v1 "
          f"(WebSocket: /api/v1/ws); токен: {'да' if args.token else 'нет'}",
          flush=True)
    web.run_app(create_app(controller, token=args.token),
                host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
