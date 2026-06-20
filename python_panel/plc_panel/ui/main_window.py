"""Top-level window: tabs, telemetry routing, journal and connection control."""

from __future__ import annotations

import json
import time
from typing import Dict, List

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import AXES, AppSettings
from ..logger import FileLogger
from ..models import ScanStep, StepType
from ..queue_backbone import MotionQueue
from ..scenario import ScenarioRunner
from ..service import PlcService
from ..session import ScanSession
from .axis_panel import AxisDebugPanel
from .charts import XYPlotWidget
from .common import button, card, double_spin, int_spin, led, set_led
from .log_dock import LogDock
from .queue_widget import ScenarioQueueWidget
from .styles import COLOR_X, COLOR_Y, COLOR_Z, INPUT_COLORS, OUTPUT_COLORS

_AXIS_COLORS = {1: COLOR_X, 2: COLOR_Y, 3: COLOR_Z}

#: Deceleration used by the emergency-stop button (fast controlled stop).
_EMERGENCY_DECEL = 5000.0


def _format_value(value: float) -> str:
    if abs(value) >= 10000 or (0 < abs(value) < 0.01):
        return f"{value:.2e}"
    return f"{value:.2f}"


class MainWindow(QMainWindow):
    """The whole HMI: a tabbed central panel plus a dockable PLC journal."""

    def __init__(self, service: PlcService, settings: AppSettings) -> None:
        super().__init__()
        self.setObjectName("appRoot")
        self.setWindowTitle("Inovance XYZ Antenna Scanner Panel")
        self.resize(1360, 820)
        self.setAcceptDrops(True)  # accept dropped *.json sequence files

        self._service = service
        self._settings = settings
        self._file_logger = FileLogger(settings.log_dir)
        # Durable command-queue backbone (servo_core) shared by the runner.
        self._queue_backbone = MotionQueue(settings.queue_db_path)
        # Crash-resume: persists the running scenario across an unexpected exit.
        self._session = ScanSession()
        self._runner = ScenarioRunner(
            service, settings, self._queue_backbone, self._session, self)
        self._pending_resume = None  # step index to resume once connected

        self._axis_panels: Dict[int, AxisDebugPanel] = {}
        self._main_targets: Dict[int, tuple] = {}
        self._input_leds: List[QLabel] = []
        self._output_leds: List[QLabel] = []

        self._build_ui()
        self._connect_signals()
        self.log("Панель управления инициализирована.")

        # Crash recovery: if a scan was interrupted, reload it and arrange to
        # continue once the PLC link is up.
        saved = self._session.load()
        if saved:
            steps, idx = saved
            self._queue.set_steps(steps)
            self._pending_resume = idx
            self.log(
                f"Обнаружен прерванный сценарий ({len(steps)} шагов). "
                f"Продолжу с шага №{idx + 1} после подключения к ПЛК.")

        if self._settings.auto_connect:
            self.log("Автоподключение включено — устанавливаю связь с ПЛК…")
            # Defer until the event loop runs so the worker thread is ready.
            QTimer.singleShot(0, self._start_connection)

    # ------------------------------------------------------------------ #
    # Construction                                                       #
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        self.setCentralWidget(self._build_main_panel())

        # The journal is a dock widget: docked right by default, but movable,
        # floatable and closable (reopen via the «Журнал» toggle).
        self._log_dock = LogDock(self)
        self.addDockWidget(Qt.RightDockWidgetArea, self._log_dock)
        self.resizeDocks([self._log_dock], [380], Qt.Horizontal)
        self._log_dock.visibilityChanged.connect(self._on_dock_visibility)

    def _build_main_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)

        status = QHBoxLayout()
        self._connect_btn = button("Связь: Отсутствует", "danger", self._toggle_connect)
        status.addWidget(self._connect_btn)
        self._journal_btn = button("Журнал ▼", "", self._toggle_journal)
        status.addWidget(self._journal_btn)
        status.addStretch(1)
        layout.addLayout(status)

        tabs = QTabWidget()
        # Let every tab grow to fit its full caption instead of being elided.
        tabs.setElideMode(Qt.ElideNone)
        tabs.setUsesScrollButtons(False)
        tabs.tabBar().setExpanding(False)
        tabs.addTab(self._build_work_tab(), "   Рабочая панель XYZ   ")
        tabs.addTab(self._build_debug_tab(), "   Отладка двигателей   ")
        tabs.addTab(self._build_settings_tab(), "   Настройки   ")
        tabs.addTab(self._build_io_tab(), "   Отладка входов-выходов easy521   ")
        layout.addWidget(tabs)
        return panel

    # --- Tab 1: working panel ----------------------------------------- #
    def _build_work_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        master_row = QHBoxLayout()
        master_row.addWidget(
            button("Включить все двигатели", "success", self._enable_all_motors))
        self._estop_btn = button(
            "АВАРИЙНЫЙ СТОП (все оси)", "danger", self._emergency_stop)
        master_row.addWidget(self._estop_btn)
        master_row.addStretch(1)
        layout.addLayout(master_row)

        axes_row = QHBoxLayout()
        defaults = {1: (100.0, 10.0), 2: (100.0, 10.0), 3: (20.0, 5.0)}
        for axis, regs in AXES.items():
            pos_default, spd_default = defaults[axis]
            frame, card_layout = card(f"Управление Осью {regs.name}",
                                      accent=_AXIS_COLORS[axis])
            on_off = QHBoxLayout()
            on_off.addWidget(button(f"{regs.name} ON", "success",
                                    lambda _=False, a=axis: self._service.set_power(a, True)))
            on_off.addWidget(button(f"{regs.name} OFF", "danger",
                                    lambda _=False, a=axis: self._service.set_power(a, False)))
            card_layout.addLayout(on_off)

            pos = double_spin(pos_default, width=80)
            spd = double_spin(spd_default, width=70)
            self._main_targets[axis] = (pos, spd)
            inputs = QHBoxLayout()
            inputs.addWidget(QLabel("Цель (мм):"))
            inputs.addWidget(pos)
            inputs.addWidget(QLabel("V:"))
            inputs.addWidget(spd)
            card_layout.addLayout(inputs)

            # All MOVE buttons share one (primary) colour, regardless of axis.
            card_layout.addWidget(
                button(f"MOVE {regs.name}", "",
                       lambda _=False, a=axis: self._move_main(a)))
            axes_row.addWidget(frame)
        layout.addLayout(axes_row)

        # A movable splitter lets the operator rebalance the two panels' width.
        bottom = QSplitter(Qt.Horizontal)
        bottom.setChildrenCollapsible(False)
        bottom.addWidget(self._build_trajectory_card())
        bottom.addWidget(self._build_monitor_card())
        bottom.setStretchFactor(0, 1)
        bottom.setStretchFactor(1, 2)
        bottom.setSizes([520, 760])
        layout.addWidget(bottom, stretch=1)
        return tab

    def _build_trajectory_card(self) -> QWidget:
        frame, layout = card("Автоматический Сценарий Сканирования")

        builder = QHBoxLayout()
        self._step_type = QComboBox()
        self._step_type.addItem("1) Движение в точку", StepType.MOVE)
        self._step_type.addItem("2) Движение с импульсом", StepType.MOVE_PULSE)
        self._step_type.addItem("3) Задержка (мс)", StepType.DELAY)
        self._step_type.addItem("4) SCPI команда", StepType.SCPI)
        self._step_type.addItem("5) Отправка импульса (Y0)", StepType.PULSE)
        self._step_type.currentIndexChanged.connect(self._sync_step_editor)
        builder.addWidget(QLabel("Действие:"))
        builder.addWidget(self._step_type)
        builder.addWidget(self._build_step_editor())
        builder.addWidget(button("Добавить", "", self._add_step))
        builder.addStretch(1)
        layout.addLayout(builder)

        io_row = QHBoxLayout()
        # Import and Export share one colour; Clear stays danger-red.
        io_row.addWidget(button("Импорт JSON", "success", self._import_sequence))
        io_row.addWidget(button("Экспорт JSON", "success", self._export_sequence))
        io_row.addWidget(button("Очистить все", "danger", self._clear_sequence))
        io_row.addStretch(1)
        layout.addLayout(io_row)

        controls = QHBoxLayout()
        self._queue = ScenarioQueueWidget()
        controls.addWidget(self._queue, stretch=1)
        run_col = QVBoxLayout()
        run_col.addWidget(button("ЗАПУСК", "success", self._start_sequence))
        self._pause_btn = button("ПАУЗА", "warn", self._toggle_pause)
        run_col.addWidget(self._pause_btn)
        run_col.addWidget(button("СТОП", "danger", self._stop_sequence))
        run_col.addStretch(1)
        controls.addLayout(run_col)
        layout.addLayout(controls, stretch=1)  # let the queue take all spare height
        return frame

    def _build_step_editor(self) -> QWidget:
        """A stacked editor whose page follows the selected step type."""
        self._step_editor = QStackedWidget()

        move = QWidget()
        move_row = QHBoxLayout(move)
        move_row.setContentsMargins(0, 0, 0, 0)
        self._q_axis = QComboBox()
        for number, regs in AXES.items():
            self._q_axis.addItem(f"Ось {regs.name}", number)
        self._q_coord = double_spin(50.0, width=70)
        self._q_speed = double_spin(10.0, width=60)
        for w in (QLabel("Привод:"), self._q_axis, QLabel("Коорд:"),
                  self._q_coord, QLabel("V:"), self._q_speed):
            move_row.addWidget(w)

        delay = QWidget()
        delay_row = QHBoxLayout(delay)
        delay_row.setContentsMargins(0, 0, 0, 0)
        self._q_delay = int_spin(1000, width=80)
        delay_row.addWidget(QLabel("Пауза:"))
        delay_row.addWidget(self._q_delay)
        delay_row.addWidget(QLabel("мс"))

        scpi = QWidget()
        scpi_row = QHBoxLayout(scpi)
        scpi_row.setContentsMargins(0, 0, 0, 0)
        self._q_scpi = QLineEdit(":INITiate:IMMediate")
        self._q_scpi.setMinimumWidth(160)
        scpi_row.addWidget(QLabel("Cmd:"))
        scpi_row.addWidget(self._q_scpi)

        pulse = QLabel("(Выход Y0 ПЛК)")

        # move and move_pulse share page 0; see _sync_step_editor for the map.
        self._step_editor.addWidget(move)    # 0 move
        self._step_editor.addWidget(delay)   # 1 delay
        self._step_editor.addWidget(scpi)    # 2 scpi
        self._step_editor.addWidget(pulse)   # 3 pulse
        return self._step_editor

    def _build_monitor_card(self) -> QWidget:
        frame, layout = card("")
        title = QLabel("Мониторинг плоскости XY")
        title.setObjectName("cardTitle")
        layout.addWidget(title)

        self._xy_plot = XYPlotWidget(self._settings.limits)
        layout.addWidget(self._xy_plot)
        self._main_stats = QLabel("X: 0.00 | Y: 0.00 | Z: 0.00")
        self._main_stats.setStyleSheet("font-size: 16px; font-weight: bold; color: #0078d4;")
        layout.addWidget(self._main_stats)
        return frame

    # --- Tab 2: motor debug ------------------------------------------- #
    def _build_debug_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        # Every column uses the first column's (X / blue) colour scheme.
        for axis, regs in AXES.items():
            panel = AxisDebugPanel(axis, regs, COLOR_X,
                                   self._service, self._settings.limits)
            self._axis_panels[axis] = panel
            layout.addWidget(panel)
        return tab

    # --- Tab 3: settings ---------------------------------------------- #
    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        layout = QHBoxLayout()
        outer.addLayout(layout)
        s = self._settings

        plc_frame, plc = card("Параметры связи ПЛК")
        self._cfg_ip = QLineEdit(s.plc_ip)
        self._cfg_port = int_spin(s.plc_port, maximum=65535)
        self._cfg_poll = int_spin(s.poll_interval_ms, maximum=60000)
        plc.addLayout(self._field("IP адрес:", self._cfg_ip))
        plc.addLayout(self._field("Порт Modbus:", self._cfg_port))
        plc.addLayout(self._field("Период опроса (мс):", self._cfg_poll))
        self._cfg_auto_connect = QCheckBox(
            "Автоматическая установка связи при запуске")
        self._cfg_auto_connect.setChecked(s.auto_connect)
        plc.addWidget(self._cfg_auto_connect)

        axes_box = QHBoxLayout()
        axes_box.addWidget(QLabel("Подключённые оси:"))
        self._cfg_axes = {}
        for axis, regs in AXES.items():
            cb = QCheckBox(regs.name)
            cb.setChecked(axis in s.active_axes)
            self._cfg_axes[axis] = cb
            axes_box.addWidget(cb)
        axes_box.addStretch(1)
        plc.addLayout(axes_box)

        plc.addStretch(1)
        layout.addWidget(plc_frame)

        scpi_frame, scpi = card("Параметры Анализатора (SCPI)")
        self._cfg_scpi_ip = QLineEdit(s.scpi_ip)
        self._cfg_scpi_port = int_spin(s.scpi_port, maximum=65535)
        scpi.addLayout(self._field("IP адрес:", self._cfg_scpi_ip))
        scpi.addLayout(self._field("Порт прибора:", self._cfg_scpi_port))
        scpi.addStretch(1)
        layout.addWidget(scpi_frame)

        grid_frame, grid = card("Конфигурация сетки XYZ шкал", accent=COLOR_Z)
        lim = s.limits
        self._cfg_limits = {
            "x_min": double_spin(lim.x_min, step=1.0),
            "x_max": double_spin(lim.x_max, step=1.0),
            "y_min": double_spin(lim.y_min, step=1.0),
            "y_max": double_spin(lim.y_max, step=1.0),
            "z_min": double_spin(lim.z_min, step=1.0),
            "z_max": double_spin(lim.z_max, step=1.0),
        }
        labels = {
            "x_min": "Мин. X:", "x_max": "Макс. X:", "y_min": "Мин. Y:",
            "y_max": "Макс. Y:", "z_min": "Мин. Z:", "z_max": "Макс. Z:",
        }
        for key, spin in self._cfg_limits.items():
            grid.addLayout(self._field(labels[key], spin))
        grid.addWidget(button("Применить сетку XYZ", "z", self._apply_grid))
        grid.addStretch(1)
        layout.addWidget(grid_frame)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        save_row.addWidget(
            button("💾 Сохранить настройки", "success", self._save_settings))
        outer.addLayout(save_row)
        return tab

    # --- Tab 4: Easy521 I/O ------------------------------------------- #
    def _build_io_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)

        in_frame, inputs = card("Мониторинг дискретных входов (Easy521)", accent=COLOR_X)
        input_labels = [
            ("X0", "Датчик исходного положения Home X"),
            ("X1", "Датчик исходного положения Home Y"),
            ("X2", "Сигнал готовности прибора (VNA Ready)"),
            ("X3", "Кнопка аварийного отключения (E-Stop)"),
        ]
        for name, description in input_labels:
            dot = led()
            self._input_leds.append(dot)
            row = QHBoxLayout()
            row.addWidget(dot)
            tag = QLabel(f"[{name}]")
            tag.setStyleSheet("font-family: monospace; font-weight: bold;")
            row.addWidget(tag)
            row.addWidget(QLabel(description))
            row.addStretch(1)
            inputs.addLayout(row)
        inputs.addStretch(1)
        layout.addWidget(in_frame)

        out_frame, outputs = card("Управление дискретными выходами (Easy521)",
                                  accent="#d83b01")
        outputs.addWidget(self._output_block(
            "Выход Y0: Запуск замера",
            ("HIGH (Вкл)", lambda: self._service.set_y0(True)),
            ("LOW (Выкл)", lambda: self._service.set_y0(False))))
        outputs.addWidget(self._output_block(
            "Выход Y1: Статический уровень / Затвор",
            ("HIGH (Вкл)", lambda: self._service.set_output(True)),
            ("LOW (Выкл)", lambda: self._service.set_output(False))))
        outputs.addWidget(self._freq_block())
        outputs.addStretch(1)
        layout.addWidget(out_frame)
        return tab

    def _output_block(self, title: str, high, low) -> QWidget:
        led_index = len(self._output_leds)
        frame = QWidget()
        layout = QVBoxLayout(frame)
        head = QHBoxLayout()
        dot = led(12)
        self._output_leds.append(dot)
        head.addWidget(dot)
        label = QLabel(title)
        label.setStyleSheet("font-weight: bold;")
        head.addWidget(label)
        head.addStretch(1)
        layout.addLayout(head)
        buttons = QHBoxLayout()
        buttons.addWidget(button(high[0], "success", high[1]))
        buttons.addWidget(button(low[0], "danger", low[1]))
        layout.addLayout(buttons)
        return frame

    def _freq_block(self) -> QWidget:
        frame = QWidget()
        layout = QVBoxLayout(frame)
        head = QHBoxLayout()
        dot = led(12)
        self._output_leds.append(dot)
        head.addWidget(dot)
        label = QLabel("Выход Y2: Непрерывная генерация частоты")
        label.setStyleSheet("font-weight: bold;")
        head.addWidget(label)
        head.addStretch(1)
        layout.addLayout(head)
        self._freq_value = int_spin(1000, maximum=1_000_000, width=100)
        layout.addLayout(self._field("Частота меандра (Гц):", self._freq_value))
        buttons = QHBoxLayout()
        buttons.addWidget(button("Старт", "z",
                                 lambda: self._service.set_frequency(True, self._freq_value.value())))
        buttons.addWidget(button("Стоп", "danger",
                                 lambda: self._service.set_frequency(False, self._freq_value.value())))
        layout.addLayout(buttons)
        return frame

    @staticmethod
    def _field(text: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel(text))
        row.addStretch(1)
        row.addWidget(widget)
        return row

    # ------------------------------------------------------------------ #
    # Signal wiring                                                      #
    # ------------------------------------------------------------------ #
    def _connect_signals(self) -> None:
        self._service.telemetry.connect(self._on_telemetry)
        self._service.connected.connect(self._on_connected)
        self._service.disconnected.connect(self._on_disconnected)
        self._service.reconnecting.connect(self._on_reconnecting)
        self._service.connection_failed.connect(self._on_connection_failed)
        self._service.command_error.connect(self.log)
        self._service.log.connect(self.log)
        self._runner.log.connect(self.log)
        self._runner.step_changed.connect(self._queue.set_active_index)
        self._runner.finished.connect(self._on_runner_finished)
        # On every (re)connect, reconcile any move left IN_FLIGHT by a crash.
        self._service.connected.connect(lambda _msg: self._runner.recover())
        # Resume an interrupted scan (from scan_session.json) once connected.
        self._service.connected.connect(self._on_connected_resume)

    # ------------------------------------------------------------------ #
    # Connection                                                         #
    # ------------------------------------------------------------------ #
    def _toggle_connect(self) -> None:
        # Toggle on operator *intent*, not on the (possibly mid-reconnect) link
        # state, so the button cancels an in-progress auto-reconnect too.
        if self._service.is_active:
            self._service.disconnect()
            return
        self._start_connection()

    def _start_connection(self) -> None:
        """Read the connection fields into settings and open the PLC link."""
        self._settings.plc_ip = self._cfg_ip.text().strip()
        self._settings.plc_port = self._cfg_port.value()
        self._settings.poll_interval_ms = self._cfg_poll.value()
        self._settings.scpi_ip = self._cfg_scpi_ip.text().strip()
        self._settings.scpi_port = self._cfg_scpi_port.value()
        self._settings.save()  # persist edited connection settings
        self.log(f"Старт Modbus TCP сессии на {self._settings.plc_ip}:"
                 f"{self._settings.plc_port}...")
        self._service.connect(self._settings.plc_ip, self._settings.plc_port,
                              self._settings.poll_interval_ms)

    def _on_connected(self, message: str) -> None:
        self._connect_btn.setText("Связь: ОК")
        self._connect_btn.setProperty("kind", "success")
        self._restyle(self._connect_btn)
        self.log(message)

    def _on_disconnected(self) -> None:
        # A drop while the operator still wants the link is handled by the
        # auto-reconnect path (_on_reconnecting), so don't reset the UI here.
        if self._service.is_active:
            return
        self._connect_btn.setText("Связь: Отсутствует")
        self._connect_btn.setProperty("kind", "danger")
        self._restyle(self._connect_btn)
        self.log("Сессия закрыта.")

    def _on_reconnecting(self, message: str) -> None:
        self._connect_btn.setText("Связь: Переподключение…")
        self._connect_btn.setProperty("kind", "warn")
        self._restyle(self._connect_btn)
        self.log(message)

    def _on_connection_failed(self, message: str) -> None:
        self.log("Ошибка соединения: " + message)

    # ------------------------------------------------------------------ #
    # Floating journal                                                   #
    # ------------------------------------------------------------------ #
    def _toggle_journal(self) -> None:
        # setVisible drives visibilityChanged, which syncs the button caption.
        self._log_dock.setVisible(not self._log_dock.isVisible())
        if self._log_dock.isVisible():
            self._log_dock.raise_()

    def _on_dock_visibility(self, visible: bool) -> None:
        self._journal_btn.setText("Журнал ▼" if visible else "Журнал ▲")

    @staticmethod
    def _restyle(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # ------------------------------------------------------------------ #
    # Telemetry                                                          #
    # ------------------------------------------------------------------ #
    def _on_telemetry(self, telemetry) -> None:
        x = telemetry.axes.get(1)
        y = telemetry.axes.get(2)
        z = telemetry.axes.get(3)
        if x and y and z:
            self._main_stats.setText(
                f"X: {_format_value(x.position)} | Y: {_format_value(y.position)} | "
                f"Z: {_format_value(z.position)}")
            self._xy_plot.set_position(x.position, y.position)

        for axis, panel in self._axis_panels.items():
            data = telemetry.axes.get(axis)
            if data is not None:
                panel.update_telemetry(data)

        for i, dot in enumerate(self._input_leds):
            if i < len(telemetry.inputs):
                set_led(dot, telemetry.inputs[i], INPUT_COLORS[i])
        for i, dot in enumerate(self._output_leds):
            if i < len(telemetry.outputs):
                set_led(dot, telemetry.outputs[i], OUTPUT_COLORS[i])

    # ------------------------------------------------------------------ #
    # Working-panel actions                                              #
    # ------------------------------------------------------------------ #
    def _move_main(self, axis: int) -> None:
        pos, spd = self._main_targets[axis]
        self._service.move_absolute(axis, pos.value(), spd.value(), False)
        self.log(f"Ручная уставка: переезд оси {AXES[axis].name} к {pos.value()} мм.")

    def _enable_all_motors(self) -> None:
        """Servo-ON every connected axis at once (MC_Power)."""
        active = self._settings.active_axes
        for axis in active:
            self._service.set_power(axis, True)
        self.log(f"Включение подключённых двигателей (Servo ON): {active}.")

    def _emergency_stop(self) -> None:
        """Emergency stop: abort the scenario, stop motion and de-energise all axes."""
        if self._runner.is_active:
            self._runner.stop()
        for axis in AXES:
            self._service.stop_axis(axis, _EMERGENCY_DECEL)
        for axis in AXES:
            self._service.set_power(axis, False)
        self.log("АВАРИЙНЫЙ СТОП: остановка и снятие питания со всех осей (X, Y, Z).")

    def _save_settings(self) -> None:
        """Read every field on the settings tab and persist to app_config.json."""
        s = self._settings
        s.plc_ip = self._cfg_ip.text().strip()
        s.plc_port = self._cfg_port.value()
        s.poll_interval_ms = self._cfg_poll.value()
        s.scpi_ip = self._cfg_scpi_ip.text().strip()
        s.scpi_port = self._cfg_scpi_port.value()
        s.auto_connect = self._cfg_auto_connect.isChecked()
        s.active_axes = [axis for axis, cb in self._cfg_axes.items()
                         if cb.isChecked()] or list(AXES)
        lim = s.limits
        lim.x_min = self._cfg_limits["x_min"].value()
        lim.x_max = self._cfg_limits["x_max"].value()
        lim.y_min = self._cfg_limits["y_min"].value()
        lim.y_max = self._cfg_limits["y_max"].value()
        lim.z_min = self._cfg_limits["z_min"].value()
        lim.z_max = self._cfg_limits["z_max"].value()
        self._xy_plot.refresh()
        for panel in self._axis_panels.values():
            panel.refresh_range()
        s.save()
        self.log("Настройки сохранены в app_config.json.")

    def _apply_grid(self) -> None:
        lim = self._settings.limits
        lim.x_min = self._cfg_limits["x_min"].value()
        lim.x_max = self._cfg_limits["x_max"].value()
        lim.y_min = self._cfg_limits["y_min"].value()
        lim.y_max = self._cfg_limits["y_max"].value()
        lim.z_min = self._cfg_limits["z_min"].value()
        lim.z_max = self._cfg_limits["z_max"].value()
        self._xy_plot.refresh()
        for panel in self._axis_panels.values():
            panel.refresh_range()
        self._settings.save()  # persist to app_config.json
        self.log(f"Границы сетки обновлены: X[{lim.x_min}..{lim.x_max}], "
                 f"Y[{lim.y_min}..{lim.y_max}], Z[{lim.z_min}..{lim.z_max}]")

    # ------------------------------------------------------------------ #
    # Scenario editing                                                   #
    # ------------------------------------------------------------------ #
    def _sync_step_editor(self) -> None:
        step_type = self._step_type.currentData()
        page = {
            StepType.MOVE: 0, StepType.MOVE_PULSE: 0, StepType.DELAY: 1,
            StepType.SCPI: 2, StepType.PULSE: 3,
        }[step_type]
        self._step_editor.setCurrentIndex(page)

    def _add_step(self) -> None:
        step_type = self._step_type.currentData()
        step = ScanStep(type=step_type)
        if step.is_move:
            step.axis = self._q_axis.currentData()
            step.pos = self._q_coord.value()
            step.spd = self._q_speed.value()
        elif step_type is StepType.DELAY:
            step.ms = self._q_delay.value()
        elif step_type is StepType.SCPI:
            step.cmd = self._q_scpi.text()
        self._queue.add_step(step)
        self.log(f"Добавлен шаг: {step_type.value}")

    def _start_sequence(self) -> None:
        self._pause_btn.setText("ПАУЗА")
        self._runner.start(self._queue.steps(), self._queue.start_index())

    def _stop_sequence(self) -> None:
        self._runner.stop()
        self._pause_btn.setText("ПАУЗА")

    def _toggle_pause(self) -> None:
        if not self._runner.is_active:
            return
        if self._runner.is_paused:
            self._runner.resume()
            self._pause_btn.setText("ПАУЗА")
        else:
            self._runner.pause()
            self._pause_btn.setText("ПРОДОЛЖИТЬ")

    def _on_runner_finished(self) -> None:
        self._pause_btn.setText("ПАУЗА")

    def _on_connected_resume(self, _message: str) -> None:
        """Continue an interrupted scan once the link is (re)established."""
        if self._pending_resume is None or self._runner.is_active:
            return
        idx = self._pending_resume
        self._pending_resume = None
        self.log(f"Продолжение прерванного сканирования с шага №{idx + 1}.")
        self._pause_btn.setText("ПАУЗА")
        self._runner.start(self._queue.steps(), idx)

    def _clear_sequence(self) -> None:
        self._queue.clear()
        self.log("Сценарий полностью очищен.")

    def _export_sequence(self) -> None:
        steps = self._queue.steps()
        if not steps:
            self.log("Очередь пуста.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт сценария", "scan_sequence.json", "Сценарий JSON (*.json)")
        if not path:
            self.log("Экспорт отменен")
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump([s.to_dict() for s in steps], handle,
                          ensure_ascii=False, indent=2)
            self.log("Сценарий успешно экспортирован")
        except OSError as exc:
            self.log(f"Ошибка сохранения JSON: {exc}")

    def _import_sequence(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Импорт сценария", "", "Сценарий JSON (*.json)")
        if not path:
            self.log("Импорт отменен")
            return
        self._load_sequence_from_path(path)

    def _load_sequence_from_path(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            steps = [s for s in (ScanStep.from_dict(d) for d in raw) if s]
            self._queue.set_steps(steps)
            self.log(f"Загружен сценарий: {len(steps)} шагов ({path}).")
        except (OSError, ValueError) as exc:
            self.log(f"Ошибка импорта: {exc}")

    # ------------------------------------------------------------------ #
    # Drag-and-drop of *.json sequence files onto the window             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _dropped_json_path(event) -> str:
        if not event.mimeData().hasUrls():
            return ""
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".json"):
                return path
        return ""

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._dropped_json_path(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt override
        path = self._dropped_json_path(event)
        if path:
            self._load_sequence_from_path(path)
            event.acceptProposedAction()

    # ------------------------------------------------------------------ #
    # Journal                                                            #
    # ------------------------------------------------------------------ #
    def log(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        self._log_dock.append(line)
        self._file_logger.append(line)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._service.shutdown()
        super().closeEvent(event)
