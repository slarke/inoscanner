"""Modbus TCP worker.

All PLC I/O happens on a single worker thread that owns the client, so the
periodic polling and one-shot commands never touch the socket concurrently.
The GUI talks to it only through queued-signal slots (see
:class:`plc_panel.service.PlcService`).
"""

from __future__ import annotations

import time
from typing import List, Optional, Sequence

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

try:  # pymodbus >= 3.x
    from pymodbus.client import ModbusTcpClient
except ImportError:  # pragma: no cover - pymodbus 2.x fallback
    from pymodbus.client.sync import ModbusTcpClient  # type: ignore

from . import scpi
from .codec import float_to_words, floats_to_words, words_to_float
from .config import AXES, IO
from .models import AxisTelemetry, Telemetry


def _ms(milliseconds: int) -> None:
    """Block the worker thread for the given time (mirrors PLC strobe timing)."""
    time.sleep(milliseconds / 1000.0)


#: How often to retry the TCP link after a drop / while it is unreachable (ms).
_RECONNECT_INTERVAL_MS = 2000
#: Consecutive poll failures tolerated before declaring the link lost.
_POLL_FAILURE_LIMIT = 3


class PlcWorker(QObject):
    """Owns the Modbus client; lives on a dedicated :class:`QThread`."""

    telemetry = pyqtSignal(object)        # Telemetry
    connected = pyqtSignal(str)
    connection_failed = pyqtSignal(str)
    reconnecting = pyqtSignal(str)        # link down / unreachable, retry running
    disconnected = pyqtSignal()
    command_error = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[ModbusTcpClient] = None
        self._poll_timer: Optional[QTimer] = None
        self._reconnect_timer: Optional[QTimer] = None
        # Desired link, kept across drops so we can transparently reconnect
        # (e.g. after a VPN turns on / switches and the socket breaks).
        self._target: Optional[tuple] = None   # (ip, port, interval_ms)
        self._want_link = False
        self._poll_failures = 0

    # ------------------------------------------------------------------ #
    # Connection lifecycle                                               #
    # ------------------------------------------------------------------ #
    @pyqtSlot(str, int, int)
    def open(self, ip: str, port: int, interval_ms: int) -> None:
        if self._client is not None:
            self.connected.emit("Уже подключено")
            return
        # Remember the target so the auto-reconnect loop can re-establish the
        # link after any drop without the operator pressing "connect" again.
        self._target = (ip, port, max(20, interval_ms))
        self._want_link = True
        self._connect_attempt(initial=True)

    @pyqtSlot()
    def close(self) -> None:
        self._want_link = False
        self._stop_reconnect()
        self._teardown_client()
        self.disconnected.emit()

    # ------------------------------------------------------------------ #
    # Connection establishment & auto-reconnect                          #
    # ------------------------------------------------------------------ #
    def _connect_attempt(self, initial: bool = False) -> None:
        if self._client is not None or self._target is None:
            return
        ip, port, interval = self._target
        client = ModbusTcpClient(host=ip, port=port, timeout=3)
        ok = False
        try:
            ok = client.connect()
        except Exception:  # noqa: BLE001 - any transport error -> retry
            ok = False
        if not ok:
            client.close()
            verb = "Не удалось подключиться" if initial else "Повторное подключение не удалось"
            self.reconnecting.emit(
                f"{verb} к {ip}:{port}. Повтор каждые "
                f"{_RECONNECT_INTERVAL_MS // 1000} с…")
            self._schedule_reconnect()
            return

        self._client = client
        self._poll_failures = 0
        for axis in AXES.values():
            try:
                self._write_coil(axis.read_error_en, True)
            except Exception:  # noqa: BLE001 - non-fatal, link is up
                pass

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(interval)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()

        self._stop_reconnect()
        self.connected.emit(f"Успешное соединение с {ip}:{port}")

    def _schedule_reconnect(self) -> None:
        if not self._want_link:
            return
        if self._reconnect_timer is None:
            self._reconnect_timer = QTimer(self)
            self._reconnect_timer.setInterval(_RECONNECT_INTERVAL_MS)
            self._reconnect_timer.timeout.connect(self._on_reconnect_tick)
        if not self._reconnect_timer.isActive():
            self._reconnect_timer.start()

    def _on_reconnect_tick(self) -> None:
        if not self._want_link or self._client is not None:
            self._stop_reconnect()
            return
        self._connect_attempt()

    def _stop_reconnect(self) -> None:
        if self._reconnect_timer is not None and self._reconnect_timer.isActive():
            self._reconnect_timer.stop()

    def _teardown_client(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer.deleteLater()
            self._poll_timer = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def _handle_link_loss(self, detail: str) -> None:
        """Tear the dead socket down and start retrying until it comes back."""
        self._teardown_client()
        self.reconnecting.emit(
            f"Связь с ПЛК потеряна ({detail}). Переподключение каждые "
            f"{_RECONNECT_INTERVAL_MS // 1000} с…")
        self._schedule_reconnect()

    def _link_alive(self) -> bool:
        c = self._client
        if c is None:
            return False
        try:
            return bool(getattr(c, "connected", True))
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ #
    # Motion commands                                                    #
    # ------------------------------------------------------------------ #
    @pyqtSlot(int, bool)
    def set_power(self, axis: int, on: bool) -> None:
        self.log.emit(
            f"Кнопка MC_Power: ось {self._axis_name(axis)} → "
            f"{'ВКЛ (Servo ON)' if on else 'ВЫКЛ (Servo OFF)'}")
        self._with_axis(axis, "Power", lambda a: self._write_coil(a.power, on))

    @pyqtSlot(int)
    def reset_axis(self, axis: int) -> None:
        self.log.emit(f"Кнопка MC_Reset: ось {self._axis_name(axis)} (строб-сброс ошибок)")

        def action(a):
            self._pulse_coil(a.reset, 100)
        self._with_axis(axis, "Reset", action)

    @pyqtSlot(int, float, float, bool)
    def move_absolute(self, axis: int, pos: float, spd: float, auto_pulse: bool) -> None:
        self.log.emit(
            f"Кнопка MC_MoveAbsolute: ось {self._axis_name(axis)}, позиция={pos} мм, "
            f"скорость={spd}, авто-импульс={'да' if auto_pulse else 'нет'}")

        def action(a):
            self._write_coil(a.abs_pulse_en, auto_pulse)
            _ms(10)
            self._write_registers(a.abs_pos, float_to_words(pos))
            self._write_registers(a.abs_spd, float_to_words(spd))
            _ms(20)
            self._pulse_coil(a.abs_exec, 40)
        self._with_axis(axis, "MoveAbsolute", action)

    @pyqtSlot(int, float, float)
    def move_relative(self, axis: int, distance: float, speed: float) -> None:
        self.log.emit(
            f"Кнопка MC_MoveRelative: ось {self._axis_name(axis)}, "
            f"смещение={distance} мм, скорость={speed}")

        def action(a):
            self._write_registers(a.rel_distance, float_to_words(distance))
            self._write_registers(a.rel_speed, float_to_words(speed))
            _ms(20)
            self._pulse_coil(a.rel_exec, 40)
        self._with_axis(axis, "MoveRelative", action)

    @pyqtSlot(int, float, float, float)
    def move_velocity(self, axis: int, speed: float, acc: float, dec: float) -> None:
        self.log.emit(
            f"Кнопка MC_MoveVelocity: ось {self._axis_name(axis)}, скорость={speed}, "
            f"разгон={acc}, замедление={dec}")

        def action(a):
            self._write_registers(a.vel_block, floats_to_words(speed, acc, dec))
            _ms(50)
            self._pulse_coil(a.vel_exec, 100)
        self._with_axis(axis, "MoveVelocity", action)

    @pyqtSlot(int, float)
    def stop_axis(self, axis: int, decel: float) -> None:
        self.log.emit(
            f"Кнопка MC_Stop: ось {self._axis_name(axis)}, замедление={decel}")

        def action(a):
            self._write_registers(a.stop_dec, float_to_words(decel))
            _ms(20)
            self._pulse_coil(a.stop_exec, 40)
        self._with_axis(axis, "Stop", action)

    @pyqtSlot(int, float)
    def set_position(self, axis: int, pos: float) -> None:
        self.log.emit(
            f"Кнопка MC_SetPosition: ось {self._axis_name(axis)}, новая позиция={pos} мм")

        def action(a):
            self._write_registers(a.set_pos_val, float_to_words(pos))
            _ms(20)
            self._pulse_coil(a.set_pos_exec, 40)
        self._with_axis(axis, "SetPosition", action)

    # ------------------------------------------------------------------ #
    # Easy521 discrete I/O                                               #
    # ------------------------------------------------------------------ #
    @pyqtSlot()
    def pulse_trigger(self) -> None:
        self.log.emit("Кнопка: импульс Y0 (цикл авто-замера SET→RESET)")
        if not self._require_client("Импульс Y0"):
            return
        try:
            self._pulse_coil(IO.Y0_SET, 50)
            _ms(150)
            self._pulse_coil(IO.Y0_RESET, 50)
            self.log.emit("Импульсный цикл авто-замера отработан (Y0)")
        except Exception as exc:  # noqa: BLE001 - report any transport failure
            self._fail("Импульс Y0", exc)

    @pyqtSlot(bool)
    def set_y0(self, on: bool) -> None:
        self.log.emit(f"Кнопка: выход Y0 → постоянный {'HIGH' if on else 'LOW'}")
        if not self._require_client("Управление Y0"):
            return
        try:
            # Hold the SET coil for a steady HIGH and the RST coil for a steady
            # LOW (the two are mutually exclusive), instead of a short strobe.
            if on:
                self._write_coil(IO.Y0_RESET, False)
                self._write_coil(IO.Y0_SET, True)
            else:
                self._write_coil(IO.Y0_SET, False)
                self._write_coil(IO.Y0_RESET, True)
            self.log.emit(f"Выход Y0 установлен в постоянный {'HIGH' if on else 'LOW'}")
        except Exception as exc:  # noqa: BLE001
            self._fail("Управление Y0", exc)

    @pyqtSlot(bool)
    def set_output(self, on: bool) -> None:
        self.log.emit(f"Кнопка: выход Y1 → {'HIGH' if on else 'LOW'}")
        if not self._require_client("Управление Y1"):
            return
        try:
            self._write_coil(IO.Y1_TOGGLE, on)
            self.log.emit(f"Выход Y1 -> {'HIGH' if on else 'LOW'}")
        except Exception as exc:  # noqa: BLE001
            self._fail("Управление Y1", exc)

    @pyqtSlot(bool, float)
    def set_frequency(self, enabled: bool, freq: float) -> None:
        self.log.emit(
            f"Кнопка: генератор Y2 → {'СТАРТ ' + str(freq) + ' Гц' if enabled else 'СТОП'}")
        if not self._require_client("Генератор Y2"):
            return
        try:
            self._write_registers(IO.Y2_FREQ_VAL, float_to_words(freq))
            _ms(20)
            self._write_coil(IO.Y2_FREQ_EN, enabled)
            self.log.emit(
                f"Генератор Y2 -> {'СТАРТ ' + str(freq) + ' Гц' if enabled else 'СТОП'}"
            )
        except Exception as exc:  # noqa: BLE001
            self._fail("Генератор Y2", exc)

    @pyqtSlot(str, str, int)
    def send_scpi(self, command: str, ip: str, port: int) -> None:
        self.log.emit(f"Команда SCPI → {ip}:{port}: «{command}»")
        try:
            self.log.emit(scpi.send_command(command, ip, port))
        except OSError as exc:
            self._fail("SCPI", exc)

    # ------------------------------------------------------------------ #
    # Polling                                                            #
    # ------------------------------------------------------------------ #
    def _poll(self) -> None:
        if self._client is None:
            return
        if not self._link_alive():
            self._handle_link_loss("сокет закрыт")
            return
        try:
            snapshot = Telemetry(block_error=self._read_uint(IO.BLOCK_ERROR_ID, 1)[0])
            for number, regs in AXES.items():
                pos_raw = self._read_uint(regs.act_pos, 2)
                vel_raw = self._read_uint(regs.act_vel, 2)
                errors = self._read_uint(regs.error_block, 3)
                snapshot.axes[number] = AxisTelemetry(
                    position=words_to_float(pos_raw),
                    velocity=words_to_float(vel_raw),
                    position_raw=pos_raw,
                    velocity_raw=vel_raw,
                    servo_error=errors[0],
                    axis_error=errors[2],
                )
            snapshot.outputs = self._read_bits(IO.OUTPUT_BASE, IO.DISCRETE_COUNT)
            snapshot.inputs = self._read_bits(IO.INPUT_X0, IO.DISCRETE_COUNT)
            self.telemetry.emit(snapshot)
            self._poll_failures = 0
        except Exception as exc:  # noqa: BLE001 - distinguish glitch vs lost link
            self._poll_failures += 1
            if not self._link_alive() or self._poll_failures >= _POLL_FAILURE_LIMIT:
                self._handle_link_loss(str(exc))
            else:
                self.command_error.emit(f"Сбой опроса телеметрии: {exc}")

    # ------------------------------------------------------------------ #
    # Low-level Modbus helpers                                           #
    # ------------------------------------------------------------------ #
    def _read_uint(self, address: int, count: int) -> List[int]:
        response = self._client.read_holding_registers(address, count=count)
        if response.isError():
            # Raise so _poll's failure counter advances and a persistently
            # broken link (e.g. half-open after a VPN switch) is detected.
            raise IOError(f"чтение регистров {address} отклонено: {response}")
        return list(response.registers)

    def _read_bits(self, address: int, count: int) -> List[bool]:
        response = self._client.read_coils(address, count=count)
        if response.isError():
            raise IOError(f"чтение coils {address} отклонено: {response}")
        return [bool(b) for b in response.bits[:count]]

    def _write_coil(self, address: int, value: bool) -> None:
        self.log.emit(f"   ↳ M{address} ← {int(value)}")
        response = self._client.write_coil(address, value)
        if response.isError():
            raise IOError(f"запись coil {address} отклонена")

    def _write_registers(self, address: int, values: Sequence[int]) -> None:
        words = list(values)
        # Decode each consecutive REAL (2 words) for a human-readable log.
        reals = [words_to_float(words[i:i + 2]) for i in range(0, len(words) - 1, 2)]
        readable = ", ".join(f"{value:.3f}" for value in reals)
        self.log.emit(f"   ↳ D{address} ← {words}  (≈ {readable})")
        response = self._client.write_registers(address, words)
        if response.isError():
            raise IOError(f"запись регистров {address} отклонена")

    def _pulse_coil(self, address: int, hold_ms: int) -> None:
        """Rising-edge strobe: set the coil, hold, then clear it."""
        self._write_coil(address, True)
        _ms(hold_ms)
        self._write_coil(address, False)

    # ------------------------------------------------------------------ #
    # Command plumbing                                                   #
    # ------------------------------------------------------------------ #
    def _with_axis(self, axis: int, label: str, action) -> None:
        if axis not in AXES:
            self.command_error.emit(f"{label}: неверный номер оси {axis}")
            return
        if not self._require_client(label):
            return
        try:
            action(AXES[axis])
        except Exception as exc:  # noqa: BLE001
            self._fail(f"{label} (ось {AXES[axis].name})", exc)

    @staticmethod
    def _axis_name(axis: int) -> str:
        return AXES[axis].name if axis in AXES else f"#{axis}"

    def _require_client(self, label: str) -> bool:
        if self._client is None:
            self.command_error.emit(f"{label}: нет связи с ПЛК")
            return False
        return True

    def _fail(self, label: str, exc: Exception) -> None:
        self.command_error.emit(f"Ошибка «{label}»: {exc}")
