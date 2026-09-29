"""Пример интеграции ядра в PyQt6 (справочный, требует установленного PyQt6).

Запуск:  pip install PyQt6 pymodbus  &&  python -m servo_core.pyqt_example

Показывает правильную развязку потоков:
  • GUI-поток только вызывает service.submit() и читает сигналы;
  • вся работа с Modbus идёт в worker-потоках Supervisor;
  • QtEventBridge доставляет события в GUI через сигналы (queued connection),
    поэтому виджеты не трогаются из чужих потоков.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget,
)

from servo_core.config import ChannelConfig, RegisterMap
from servo_core.database import Database
from servo_core.events import EventBus
from servo_core.mock import MockModbusDriver, MockPlc
from servo_core.models import MotionCommand
from servo_core.qt_bridge import QtEventBridge
from servo_core.service import CommandService, QueueFullError
from servo_core.supervisor import Supervisor


class ServoPanel(QWidget):
    def __init__(self, service: CommandService, bridge: QtEventBridge,
                 supervisor: Supervisor):
        super().__init__()
        self._service = service
        self._supervisor = supervisor
        self.setWindowTitle("Servo queue demo")

        self._log = QListWidget()
        self._status = QLabel("готов")
        send_btn = QPushButton("Отправить команду (+1000)")
        ack_btn = QPushButton("Подтвердить безопасное состояние (после E-stop)")

        layout = QVBoxLayout(self)
        layout.addWidget(self._status)
        layout.addWidget(send_btn)
        layout.addWidget(ack_btn)
        layout.addWidget(self._log)

        send_btn.clicked.connect(self._on_send)
        ack_btn.clicked.connect(lambda: supervisor.acknowledge_safe_state("COM3"))

        # Подключаем сигналы моста к слотам GUI (queued: безопасно из потоков).
        bridge.command_enqueued.connect(
            lambda ch, seq: self._append(f"в очередь: {ch} seq={seq}"))
        bridge.command_sent.connect(
            lambda ch, seq: self._append(f"отправлено: {ch} seq={seq}"))
        bridge.command_done.connect(
            lambda ch, seq: self._append(f"ВЫПОЛНЕНО: {ch} seq={seq}"))
        bridge.command_dead_letter.connect(
            lambda ch, seq, why: self._append(f"DEAD-LETTER: {ch} seq={seq} ({why})"))
        bridge.connection_lost.connect(
            lambda ch, why: self._status.setText(f"СВЯЗЬ ПОТЕРЯНА: {ch} ({why})"))
        bridge.connection_restored.connect(
            lambda ch, _: self._status.setText(f"связь восстановлена: {ch}"))
        bridge.estop_detected.connect(
            lambda ch, why: self._status.setText(f"E-STOP: {ch} ({why})"))

        self._target = 0

    def _on_send(self) -> None:
        self._target += 1000
        try:
            self._service.submit("COM3", MotionCommand(
                target_position=self._target, speed=500, command_code=1,
                operator="gui"))
        except QueueFullError as exc:
            self._append(f"отказ (backpressure): {exc}")

    def _append(self, text: str) -> None:
        self._log.addItem(text)

    def closeEvent(self, event) -> None:
        self._supervisor.stop()
        super().closeEvent(event)


def main() -> None:
    registers = RegisterMap()
    cfg = ChannelConfig(name="COM3", port="mock", registers=registers,
                        poll_interval_s=0.02)
    db = Database(Path(tempfile.mkdtemp()) / "servo.db")
    from servo_core.repository import CommandRepository
    repo = CommandRepository(db)
    events = EventBus()
    service = CommandService(repo, events, max_pending=cfg.max_pending)

    supervisor = Supervisor(repo, events)
    supervisor.add_channel(cfg, MockModbusDriver(MockPlc(registers)))

    app = QApplication(sys.argv)
    bridge = QtEventBridge(events)          # живёт в GUI-потоке
    panel = ServoPanel(service, bridge, supervisor)
    panel.resize(480, 360)
    panel.show()
    supervisor.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
