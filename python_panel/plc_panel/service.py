"""GUI-facing facade over :class:`plc_panel.modbus_worker.PlcWorker`.

Public methods emit *request* signals that are delivered to the worker's slots
through a queued connection, so the GUI never blocks on Modbus I/O.  Worker
result signals are re-exposed here under stable names.
"""

from __future__ import annotations

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal

from .modbus_worker import PlcWorker


class PlcService(QObject):
    """Thread-safe, signal-based PLC API for the UI layer."""

    # Results forwarded from the worker.
    telemetry = pyqtSignal(object)        # Telemetry
    connected = pyqtSignal(str)
    connection_failed = pyqtSignal(str)
    reconnecting = pyqtSignal(str)        # link down/unreachable, auto-retry running
    disconnected = pyqtSignal()
    command_error = pyqtSignal(str)
    log = pyqtSignal(str)

    # Requests delivered to the worker thread.
    _open = pyqtSignal(str, int, int)
    _close = pyqtSignal()
    _power = pyqtSignal(int, bool)
    _reset = pyqtSignal(int)
    _move_abs = pyqtSignal(int, float, float, bool)
    _move_rel = pyqtSignal(int, float, float)
    _move_vel = pyqtSignal(int, float, float, float)
    _stop = pyqtSignal(int, float)
    _set_pos = pyqtSignal(int, float)
    _pulse = pyqtSignal()
    _y0 = pyqtSignal(bool)
    _output = pyqtSignal(bool)
    _freq = pyqtSignal(bool, float)
    _scpi = pyqtSignal(str, str, int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.is_connected = False   # actual link state
        self.is_active = False      # operator intent (connect requested)

        self._thread = QThread()
        self._worker = PlcWorker()
        self._worker.moveToThread(self._thread)
        self._thread.start()

        self._wire_requests()
        self._wire_results()

    # ------------------------------------------------------------------ #
    # Wiring                                                             #
    # ------------------------------------------------------------------ #
    def _wire_requests(self) -> None:
        queued = Qt.QueuedConnection
        w = self._worker
        self._open.connect(w.open, queued)
        self._close.connect(w.close, queued)
        self._power.connect(w.set_power, queued)
        self._reset.connect(w.reset_axis, queued)
        self._move_abs.connect(w.move_absolute, queued)
        self._move_rel.connect(w.move_relative, queued)
        self._move_vel.connect(w.move_velocity, queued)
        self._stop.connect(w.stop_axis, queued)
        self._set_pos.connect(w.set_position, queued)
        self._pulse.connect(w.pulse_trigger, queued)
        self._y0.connect(w.set_y0, queued)
        self._output.connect(w.set_output, queued)
        self._freq.connect(w.set_frequency, queued)
        self._scpi.connect(w.send_scpi, queued)

    def _wire_results(self) -> None:
        w = self._worker
        w.telemetry.connect(self.telemetry)
        w.connection_failed.connect(self.connection_failed)
        w.reconnecting.connect(self._on_reconnecting)
        w.command_error.connect(self.command_error)
        w.log.connect(self.log)
        w.connected.connect(self._on_connected)
        w.disconnected.connect(self._on_disconnected)

    def _on_reconnecting(self, message: str) -> None:
        self.is_connected = False
        self.reconnecting.emit(message)

    def _on_connected(self, message: str) -> None:
        self.is_connected = True
        self.connected.emit(message)

    def _on_disconnected(self) -> None:
        self.is_connected = False
        self.disconnected.emit()

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def connect(self, ip: str, port: int, interval_ms: int) -> None:
        self.is_active = True
        self._open.emit(ip, port, interval_ms)

    def disconnect(self) -> None:
        self.is_active = False
        self._close.emit()

    def set_power(self, axis: int, on: bool) -> None:
        self._power.emit(axis, on)

    def reset_axis(self, axis: int) -> None:
        self._reset.emit(axis)

    def move_absolute(self, axis: int, pos: float, spd: float, auto_pulse: bool) -> None:
        self._move_abs.emit(axis, pos, spd, auto_pulse)

    def move_relative(self, axis: int, distance: float, speed: float) -> None:
        self._move_rel.emit(axis, distance, speed)

    def move_velocity(self, axis: int, speed: float, acc: float, dec: float) -> None:
        self._move_vel.emit(axis, speed, acc, dec)

    def stop_axis(self, axis: int, decel: float) -> None:
        self._stop.emit(axis, decel)

    def set_position(self, axis: int, pos: float) -> None:
        self._set_pos.emit(axis, pos)

    def pulse_trigger(self) -> None:
        self._pulse.emit()

    def set_y0(self, on: bool) -> None:
        self._y0.emit(on)

    def set_output(self, on: bool) -> None:
        self._output.emit(on)

    def set_frequency(self, enabled: bool, freq: float) -> None:
        self._freq.emit(enabled, freq)

    def send_scpi(self, command: str, ip: str, port: int) -> None:
        self._scpi.emit(command, ip, port)

    def shutdown(self) -> None:
        """Stop the worker thread cleanly; call once on application exit."""
        self.disconnect()
        self._thread.quit()
        self._thread.wait(2000)
